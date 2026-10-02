"""证据撤回承受力分析（只读）。

考古人员核对当前地层关系时，需要知道：若允许在**同一正式视图**中撤回若干
生效证据（同一期 / 早于证据均可撤回），最少撤回多少条才能让指定两地层的
**确定关系变为未定**，以及并列时应撤回哪一组。

口径：

- 只从“其余生效证据”中选择撤回：`protected_evidence` 为可为空的受保护生效
  证据编号集合，受保护证据不可撤回；
- 关系由“同一期（无向、可传递）+ 早于（有向）”混合图上的路径支撑。撤回只删
  边、不加边，因此关系变未定 ⇔ 混合图上两个方向的所有路径都被切断（对
  same_period 即两端同一期连通被切断；对 earlier_than/later_than 即该方向的
  全部混合路径被切断）；
- **同一期双向连通按一条证据撤回计数**：混合图中的同一期边是无向边，网络上
  建成正反两条容量 1 的弧，同一期证据的最小割恰为 1；
- 最少撤回数即该混合证据图上的 s–t 最小割（Dinic 单位容量）；并列时按证据
  编号序列字典序最小，通过对证据按编号排序后的逐条“强制保留 / 强制删去”
  可行性测试贪心求得；
- 替代证据路径仍成立时不判失效：最小割必须切断全部路径，若受保护证据单独
  仍支撑某条路径（受保护子图仍连通），目标不可达，明确返回 `unreachable`
  并给出仍成立的（字典序最小）证据路径；
- 原关系已为未定时撤回数为 0、建议集合为空。

全程只读：`REPEATABLE READ READ ONLY` 快照事务内读取修订号、地层与生效断言，
不写库、不递增修订号、不产生变化记录；修订号过期（409 STALE_REVISION）、
地层无效（400 INVALID_STRATUM）、两地层相同（400 DUPLICATE_STRATUM）、
受保护证据不存在或非生效（400 INVALID_PROTECTED_EVIDENCE）明确拒绝。
"""
from __future__ import annotations

from collections import deque
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Tuple

from .db import db_conn
from .derivation import Assertion, Derivation, EARLIER, RELATION_LABELS, SAME, best_path, _build_adj
from .errors import ApiError
from .models import ResilienceIn

KINDS_MIXED = frozenset({SAME, EARLIER})
KINDS_SAME = frozenset({SAME})

INF = 10**9


# ------------------------------------------------------------------ 最小割（Dinic）
class _Edge:
    __slots__ = ("to", "rev", "cap")

    def __init__(self, to: int, rev: int, cap: int) -> None:
        self.to = to
        self.rev = rev
        self.cap = cap


class _Dinic:
    """有向单位容量网络最大流。

    同一期证据在混合图中是无向边：按惯例加入 (u,v) 与 (v,u) 两条容量 1 的
    独立弧。其 s–t 割值恰为 1（割中该证据至多计一次——反向弧不可能同时穿过
    同一割），即“同一期双向连通按一条证据撤回计数”。
    """

    def __init__(self, n: int) -> None:
        self.g: List[List[_Edge]] = [[] for _ in range(n)]

    def add_arc(self, u: int, v: int, cap: int = 1) -> None:
        self.g[u].append(_Edge(v, len(self.g[v]), cap))
        self.g[v].append(_Edge(u, len(self.g[u]) - 1, 0))

    def _bfs(self, s: int, t: int, level: List[int]) -> bool:
        for i in range(len(level)):
            level[i] = -1
        level[s] = 0
        q = deque([s])
        while q:
            v = q.popleft()
            for e in self.g[v]:
                if e.cap > 0 and level[e.to] < 0:
                    level[e.to] = level[v] + 1
                    q.append(e.to)
        return level[t] >= 0

    def _dfs(self, v: int, t: int, f: int, level: List[int], it: List[int]) -> int:
        if v == t:
            return f
        while it[v] < len(self.g[v]):
            e = self.g[v][it[v]]
            if e.cap > 0 and level[e.to] == level[v] + 1:
                d = self._dfs(e.to, t, min(f, e.cap), level, it)
                if d > 0:
                    e.cap -= d
                    self.g[e.to][e.rev].cap += d
                    return d
            it[v] += 1
        return 0

    def max_flow(self, s: int, t: int, limit: Optional[int] = None) -> int:
        flow = 0
        level = [-1] * len(self.g)
        cap_limit = limit if limit is not None else INF
        while flow < cap_limit and self._bfs(s, t, level):
            it = [0] * len(self.g)
            while flow < cap_limit:
                f = self._dfs(s, t, cap_limit - flow, level, it)
                if f == 0:
                    break
                flow += f
        return flow


def _build_network(
    assertions: Sequence[Assertion],
    source: str,
    target: str,
    forced_keep: FrozenSet[str] = frozenset(),
    forced_cut: FrozenSet[str] = frozenset(),
) -> Tuple[_Dinic, Dict[str, int]]:
    """由生效断言构造混合证据网络。

    - 同一期：无向边，正反两条独立容量 1 弧（同一期双向连通按一条证据计数）；
    - 早于：有向容量 1 弧；
    - forced_keep（受保护 / 本步强制保留）：容量 INF，割不能经过它；
    - forced_cut（本步强制删去）：不进入网络。
    """
    nodes = sorted({source, target} | {x.a for x in assertions} | {x.b for x in assertions})
    idx = {s: i for i, s in enumerate(nodes)}
    net = _Dinic(len(nodes))
    for a in sorted(assertions, key=lambda x: x.evidence_id):
        if a.evidence_id in forced_cut:
            continue
        cap = INF if a.evidence_id in forced_keep else 1
        u, v = idx[a.a], idx[a.b]
        if a.relation == SAME:
            # 同一期无向双向连通：正反两条独立容量弧，撤回一条证据即切断
            net.add_arc(u, v, cap)
            net.add_arc(v, u, cap)
        else:  # EARLIER：a 早于 b，有向容量弧
            net.add_arc(u, v, cap)
    return net, idx


def _min_cut_value(
    assertions: Sequence[Assertion],
    source: str,
    target: str,
    forced_keep: FrozenSet[str] = frozenset(),
    forced_cut: FrozenSet[str] = frozenset(),
) -> int:
    """约束下的最小割值（INF 表示只能切断受保护 / 强制保留证据，目标不可达）。"""
    net, idx = _build_network(assertions, source, target, forced_keep=forced_keep, forced_cut=forced_cut)
    s, t = idx[source], idx[target]
    # 可切断边总数 +1 作为“不可达”判定阈值：流量超过可切断边数必走保留边
    cuttable = sum(1 for a in assertions if a.evidence_id not in forced_keep and a.evidence_id not in forced_cut)
    flow = net.max_flow(s, t, limit=cuttable + 1)
    return INF if flow > cuttable else flow


def _lexicographically_smallest_min_cut(
    assertions: Sequence[Assertion],
    source: str,
    target: str,
    protected: FrozenSet[str],
    cut_size: int,
) -> List[str]:
    """在所有规模为 cut_size 的最小撤回集合中，取证据编号序列字典序最小者。

    对可撤回证据按编号升序逐条贪心：尝试把当前编号**纳入**撤回集合（最小元素
    越小，排序后的序列字典序越小），若在“已选删去 + 当前编号”基础上仍能把割
    扩展到 cut_size 条（约束下的最小割值不超过剩余预算），则选它；否则它必须
    保留。全局最小性保证可行性测试中的最小割不会小于剩余预算。
    """
    kept: set = set(protected)
    cut: set = set()

    for eid in sorted(a.evidence_id for a in assertions if a.evidence_id not in protected):
        val = _min_cut_value(
            assertions,
            source,
            target,
            forced_keep=frozenset(kept),
            forced_cut=frozenset(cut | {eid}),
        )
        if val <= cut_size - len(cut) - 1:
            cut.add(eid)
        else:
            kept.add(eid)
    return sorted(cut)


# ------------------------------------------------------------------ 承受力分析
def _path_details(rows_by_id: Dict[str, Tuple[str, str, str, str]], path: Optional[Sequence[str]]) -> List[Dict[str, Any]]:
    if not path:
        return []
    return [
        {
            "evidence_id": eid,
            "stratum_a": rows_by_id[eid][0],
            "stratum_b": rows_by_id[eid][1],
            "relation": rows_by_id[eid][2],
            "status": rows_by_id[eid][3],
        }
        for eid in path
        if eid in rows_by_id
    ]


def analyze_retraction_resilience(body: ResilienceIn) -> Dict[str, Any]:
    """只读分析：求让两地层关系变为未定所需的最少生效证据撤回。"""
    stratum_a, stratum_b = body.stratum_a, body.stratum_b
    if stratum_a == stratum_b:
        raise ApiError(
            400,
            "DUPLICATE_STRATUM",
            f"两个地层编号相同: {stratum_a}（须为两个不同的已登记地层）",
            {"stratum_id": stratum_a},
        )

    protected_input = sorted(set(body.protected_evidence))

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY ISOLATION LEVEL REPEATABLE READ")

            cur.execute("SELECT revision FROM revision_counter WHERE id = TRUE")
            current = cur.fetchone()[0]
            if body.expected_revision != current:
                raise ApiError(
                    409,
                    "STALE_REVISION",
                    f"修订号过期：请求携带 {body.expected_revision}，当前为 {current}",
                    {"current_revision": current},
                )

            cur.execute("SELECT stratum_id FROM strata WHERE stratum_id = ANY(%s)", ([stratum_a, stratum_b],))
            known = {r[0] for r in cur.fetchall()}
            for s in (stratum_a, stratum_b):
                if s not in known:
                    raise ApiError(400, "INVALID_STRATUM", f"地层未登记: {s}", {"stratum_id": s})

            cur.execute(
                "SELECT evidence_id, stratum_a, stratum_b, relation, status "
                "FROM assertions ORDER BY evidence_id"
            )
            all_rows = cur.fetchall()

    rows_by_id: Dict[str, Tuple[str, str, str, str]] = {r[0]: (r[1], r[2], r[3], r[4]) for r in all_rows}

    not_found = [eid for eid in protected_input if eid not in rows_by_id]
    not_active = [eid for eid in protected_input if eid in rows_by_id and rows_by_id[eid][3] != "active"]
    if not_found or not_active:
        raise ApiError(
            400,
            "INVALID_PROTECTED_EVIDENCE",
            "受保护证据无效：须为当前视图中生效的证据编号",
            {"not_found": not_found, "not_active": not_active},
        )

    protected = frozenset(protected_input)
    assertions = [
        Assertion(evidence_id=r[0], a=r[1], b=r[2], relation=r[3])
        for r in all_rows
        if r[4] == "active"
    ]
    derivation = Derivation(assertions)
    relation, original_path = derivation.relation(stratum_a, stratum_b)

    common: Dict[str, Any] = {
        "expected_revision": body.expected_revision,
        "current_revision": current,
        "revision": current,
        "stratum_a": stratum_a,
        "stratum_b": stratum_b,
        "protected_evidence": protected_input,
        "original_relation": relation,
        "original_relation_label": RELATION_LABELS[relation],
        "original_evidence_path": original_path or [],
        "original_path_detail": _path_details(rows_by_id, original_path),
    }

    if relation == "undetermined":
        common.update(
            {
                "reachable": True,
                "status": "already_undetermined",
                "minimum_retraction_count": 0,
                "suggested_retractions": [],
                "simulated_relation": "undetermined",
                "simulated_relation_label": RELATION_LABELS["undetermined"],
                "remaining_evidence_path": None,
            }
        )
        return common

    # 关系方向规整到混合图：later_than 时交换两端，使原证据路径方向与网络一致
    if relation == "later_than":
        source, target = stratum_b, stratum_a
    else:
        source, target = stratum_a, stratum_b

    # 受保护子图是否仍单独支撑该关系（可达 ⇒ 目标无法失效）
    kept = [a for a in assertions if a.evidence_id in protected]
    kinds = KINDS_SAME if relation == "same_period" else KINDS_MIXED
    surviving = best_path(_build_adj(kept, kinds), source, target)

    if surviving is not None:
        common.update(
            {
                "reachable": False,
                "status": "unreachable",
                "minimum_retraction_count": None,
                "suggested_retractions": [],
                "simulated_relation": relation,
                "simulated_relation_label": RELATION_LABELS[relation],
                "remaining_evidence_path": list(surviving),
                "remaining_path_detail": _path_details(rows_by_id, surviving),
            }
        )
        return common

    # 无受保护约束的全局最小割（force 全部受保护证据保留）
    cut_size = _min_cut_value(assertions, source, target, forced_keep=protected)
    if cut_size >= INF:
        # 理论不可达：受保护可达已在上面处理；防御性返回不可达
        common.update(
            {
                "reachable": False,
                "status": "unreachable",
                "minimum_retraction_count": None,
                "suggested_retractions": [],
                "simulated_relation": relation,
                "simulated_relation_label": RELATION_LABELS[relation],
                "remaining_evidence_path": None,
            }
        )
        return common

    chosen = _lexicographically_smallest_min_cut(assertions, source, target, protected, cut_size)

    # 模拟撤回：替代路径仍成立则关系不失效；最小割保证两个方向均被切断
    remaining = [a for a in assertions if a.evidence_id not in frozenset(chosen)]
    sim_relation, sim_path = Derivation(remaining).relation(stratum_a, stratum_b)

    common.update(
        {
            "reachable": True,
            "status": "retractions_found",
            "minimum_retraction_count": cut_size,
            "suggested_retractions": chosen,
            "simulated_relation": sim_relation,
            "simulated_relation_label": RELATION_LABELS[sim_relation],
            "simulated_evidence_path": sim_path or [],
            "simulated_path_detail": _path_details(rows_by_id, sim_path),
        }
    )
    return common
