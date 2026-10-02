"""提交前预检（只读）：计算消除本批冲突所需放弃的最少现有证据。

输入沿用正式批次的 expected_revision / operations，并指定可额外撤回的生效旧证据
数上限（0～3）。预检全程只读：在 REPEATABLE READ READ ONLY 快照事务内按正式批次
**完全相同的顺序与语义**重放全部结构性校验（修订号、地层、编号占用、撤回目标），
但不写库、不递增修订号、不产生变化记录或任何其他副作用。

对按原批次语义处理新增与撤回后的视图做关系核验：

- 无冲突（本批次本可直接通过）：额外撤回集合为空，passed=true；
- 仍冲突：在“处理后视图中仍生效、且由历史批次引入”的旧证据中寻找额外撤回集合——
  既不包含本批新增证据，也不与本批撤回目标重复：
  - 撤回数最少（0..budget），并列时取证据编号序列字典序最小的一组；
  - 本批最终保留的新断言自身矛盾（仅凭新断言之间就构成组内早于/早于闭环）：
    无法靠撤回旧证据修复（resolution=unfixable_new_assertions）；
  - 预算内不存在解：预算不足（resolution=budget_insufficient）。

unfixable / budget_insufficient 两种情况均附“处理后视图（未做额外撤回）”中
仍存在的冲突证据路径，与正式批次的 CONFLICT.details.conflicts 同构。
"""
from __future__ import annotations

from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Tuple

from .db import db_conn
from .derivation import Assertion, Derivation
from .errors import ApiError
from .models import AssertOp, PreflightIn, RetractOp

# 预算硬上限（与模型字段约束保持一致；搜索复杂度随该值指数级增长）
MAX_BUDGET = 3


# ------------------------------------------------------------------ 最少额外撤回搜索
def _conflict_witness_ids(conflicts: Sequence[dict]) -> List[str]:
    """收集当前全部冲突证据路径上的证据编号（去重，按编号排序）。

    关键完备性：任一能消除全部冲突的撤回集合，都必须命中每条冲突所报告的证据路径
    ——对组内早于，路径是“同一期链 + 组内早于断言”，不撤其中任一证据则该组内早于
    依旧；对早于闭环，路径是“环上早于断言 + 环内同一期链”，不撤其中任一证据则该环
    依旧。因此每一层只需从当前冲突路径上的证据中分支，不会漏掉最小解。
    """
    ids: set = set()
    for c in conflicts:
        ids.update(c.get("evidence_path") or ())
    return sorted(ids)


def search_minimum_extra_retractions(
    base_assertions: Sequence[Assertion],
    candidates: FrozenSet[str],
    budget: int,
) -> Optional[Tuple[List[str], int]]:
    """在候选旧证据（编号集合）中寻找让核验通过的最少撤回集合。

    按撤回数 0..budget 迭代加深（BFS 分层）；同层状态按证据编号元组字典序处理，
    因此首个核验通过的集合既是规模最小、又是同规模中字典序最小的。

    返回 (排序后的证据编号序列, 撤回数)；预算内无解返回 None。
    """
    def derive(retracted: FrozenSet[str]) -> Derivation:
        return Derivation([a for a in base_assertions if a.evidence_id not in retracted])

    conflicts0 = derive(frozenset()).validate()
    if not conflicts0:
        return [], 0

    # BFS 分层：第 depth 层是恰好撤回 depth 个证据的状态
    states: List[FrozenSet[str]] = [frozenset()]
    seen: set = {frozenset()}
    for depth in range(budget + 1):
        # 同一层只需要在进入时核验；按字典序处理保证并列取字典序最小
        ordered = sorted(states, key=lambda s: tuple(sorted(s)))
        next_states: set = set()
        for retracted in ordered:
            conflicts = derive(retracted).validate()
            if not conflicts:
                chosen = sorted(retracted)
                return chosen, len(chosen)
            if depth == budget:
                continue
            # 只从当前冲突证据路径上、且属于可撤回旧证据的编号分支
            choices = [e for e in _conflict_witness_ids(conflicts) if e in candidates and e not in retracted]
            for eid in choices:
                nxt = retracted | {eid}
                if nxt not in seen:
                    seen.add(nxt)
                    next_states.add(nxt)
        states = list(next_states)
    return None


# ------------------------------------------------------------------ 预检
def _raise_stale(cur, expected: int) -> None:
    cur.execute("SELECT revision FROM revision_counter WHERE id = TRUE")
    current = cur.fetchone()[0]
    if expected != current:
        raise ApiError(
            409,
            "STALE_REVISION",
            f"修订号过期：请求携带 {expected}，当前为 {current}",
            {"current_revision": current},
        )


def preflight_batch(batch: PreflightIn) -> Dict[str, Any]:
    """只读预检：返回消除冲突所需的最少额外旧证据撤回集合。"""
    budget = min(batch.extra_retraction_budget, MAX_BUDGET)
    with db_conn() as conn:
        # 只读快照事务：与正式批次的写事务互不干扰，提交后连接归还池即恢复默认模式
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY ISOLATION LEVEL REPEATABLE READ")

            # 1) 修订号（只读查询，不加 FOR UPDATE：预检不得阻塞或干扰正式批次）
            _raise_stale(cur, batch.expected_revision)

            # 2) 加载该正式视图：已登记地层、全部断言行（含状态与所属修订）
            cur.execute("SELECT stratum_id FROM strata")
            strata = {r[0] for r in cur.fetchall()}
            cur.execute(
                "SELECT evidence_id, stratum_a, stratum_b, relation, status, added_revision "
                "FROM assertions"
            )
            rows = cur.fetchall()
            # status_by_id 仅含历史断言；本批新增编号在重放过程中动态加入
            status_by_id: Dict[str, str] = {r[0]: r[4] for r in rows}
            historical_by_id: Dict[str, Tuple[str, str, str, str, int]] = {
                r[0]: (r[1], r[2], r[3], r[4], r[5]) for r in rows
            }

            # 3) 按正式批次的操作顺序逐条重放结构性校验（命中即按相同错误码拒绝）。
            #    重放只在内存中维护断言状态，绝不执行 INSERT/UPDATE。
            # 本批新增（证据编号 -> 断言），含同批“新增后又撤回”
            new_assertions: Dict[str, Assertion] = {}
            batch_new_ids: set = set()           # 本批 assert 过的全部编号
            batch_retract_ids: set = set()       # 本批 retract 过的全部编号
            # 撤回后仍生效的本批新增编号集合
            retracted_new: set = set()

            for op in batch.operations:
                if isinstance(op, AssertOp):
                    if op.stratum_a not in strata:
                        raise ApiError(
                            400, "INVALID_STRATUM", f"地层未登记: {op.stratum_a}", {"stratum_id": op.stratum_a}
                        )
                    if op.stratum_b not in strata:
                        raise ApiError(
                            400, "INVALID_STRATUM", f"地层未登记: {op.stratum_b}", {"stratum_id": op.stratum_b}
                        )
                    eid = op.evidence_id
                    # 正式历史编号（含已撤回）永久占用；本批内编号也不可复用
                    if eid in historical_by_id:
                        raise ApiError(
                            409,
                            "DUPLICATE_EVIDENCE",
                            f"证据编号已存在（含已撤回）: {eid}",
                            {"evidence_id": eid},
                        )
                    if eid in batch_new_ids:
                        raise ApiError(
                            409,
                            "DUPLICATE_EVIDENCE",
                            f"证据编号在同一批内重复: {eid}",
                            {"evidence_id": eid},
                        )
                    batch_new_ids.add(eid)
                    new_assertions[eid] = Assertion(
                        evidence_id=eid, a=op.stratum_a, b=op.stratum_b, relation=op.relation
                    )
                    status_by_id[eid] = "active"
                elif isinstance(op, RetractOp):
                    eid = op.evidence_id
                    if eid in batch_new_ids:
                        # 撤回本批自己新增的断言
                        if eid in batch_retract_ids:
                            raise ApiError(
                                409,
                                "ASSERTION_NOT_FOUND",
                                f"撤回失败：证据 {eid} 在同一批内被重复撤回",
                                {"evidence_id": eid},
                            )
                        batch_retract_ids.add(eid)
                        retracted_new.add(eid)
                        status_by_id[eid] = "retracted"
                    else:
                        info = historical_by_id.get(eid)
                        if info is None:
                            raise ApiError(
                                409,
                                "ASSERTION_NOT_FOUND",
                                f"撤回失败：证据编号不存在: {eid}",
                                {"evidence_id": eid},
                            )
                        if info[3] != "active":
                            raise ApiError(
                                409,
                                "ASSERTION_NOT_FOUND",
                                f"撤回失败：证据 {eid} 已处于撤回状态",
                                {"evidence_id": eid},
                            )
                        if eid in batch_retract_ids:
                            raise ApiError(
                                409,
                                "ASSERTION_NOT_FOUND",
                                f"撤回失败：证据 {eid} 在同一批内被重复撤回",
                                {"evidence_id": eid},
                            )
                        batch_retract_ids.add(eid)
                        status_by_id[eid] = "retracted"

            # 4) 组装“按原批次语义处理新增与撤回后”的生效断言视图
            after_assertions: List[Assertion] = []
            for eid, (a, b, relation, _status, _rev) in historical_by_id.items():
                if status_by_id[eid] == "active":
                    after_assertions.append(Assertion(evidence_id=eid, a=a, b=b, relation=relation))
            for eid, assertion in new_assertions.items():
                if eid not in retracted_new:
                    after_assertions.append(assertion)

            conflicts = Derivation(after_assertions).validate()

            common: Dict[str, Any] = {
                "expected_revision": batch.expected_revision,
                "current_revision": batch.expected_revision,
                "extra_retraction_budget": budget,
                "batch_new_evidence": sorted(batch_new_ids),
                "batch_retracted_evidence": sorted(batch_retract_ids),
            }

            # 5a) 原批次本可通过：空集合
            if not conflicts:
                common.update(
                    {
                        "resolution": "already_consistent",
                        "passed": True,
                        "extra_retractions": [],
                        "extra_retraction_count": 0,
                        "conflicts": [],
                    }
                )
                return common

            # 5b) 本批最终保留的新断言自身矛盾？——仅凭新断言之间核验
            retained_new = [a for eid, a in new_assertions.items() if eid not in retracted_new]
            if Derivation(retained_new).validate():
                common.update(
                    {
                        "resolution": "unfixable_new_assertions",
                        "passed": False,
                        "extra_retractions": [],
                        "extra_retraction_count": 0,
                        "conflicts": conflicts,
                    }
                )
                return common

            # 5c) 在可额外撤回的旧证据中搜索最少集合：
            #     处理后视图仍生效的历史证据，且不在本批撤回目标中（不含本批新增证据）
            candidates = frozenset(
                a.evidence_id
                for a in after_assertions
                if a.evidence_id not in batch_new_ids and a.evidence_id not in batch_retract_ids
            )
            result = search_minimum_extra_retractions(after_assertions, candidates, budget)
            if result is not None:
                chosen, count = result
                # 搜索函数只在撤回后核验无冲突时返回该集合，故应用后必通过
                common.update(
                    {
                        "resolution": "extra_retractions_found",
                        "passed": True,
                        "extra_retractions": chosen,
                        "extra_retraction_count": count,
                        "conflicts": [],
                    }
                )
                return common

            # 5d) 预算内无解
            common.update(
                {
                    "resolution": "budget_insufficient",
                    "passed": False,
                    "extra_retractions": [],
                    "extra_retraction_count": None,
                    "conflicts": conflicts,
                }
            )
            return common
