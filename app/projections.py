"""推演（证据修订沙盘）业务逻辑。

创建推演时固定三项基线：
- 正式修订号（base_revision）；
- 已登记地层（base_strata）；
- 生效断言（base_assertions）。

推演内沿用正式批次的新增 / 撤回 / 整批核验语义，但一切变更只落在
projection_* 表，正式状态不变。提交（merge）时把推演相对基线的**净变更**
应用到最新正式状态并重新核验：

- 新增：推演内仍生效、且基线中不存在的证据；
- 撤回：基线中生效、且被推演撤回的证据（推演内“新增后又撤回”不产生净变更）。

净变更为空：只关闭推演，不递增正式修订号。
"""
from typing import Any, Dict, List, Optional

from psycopg2.extras import Json

from .db import db_conn
from .changes import insert_revision_changes, relation_diffs
from .derivation import Assertion, Derivation, RELATION_LABELS
from .errors import ApiError
from .models import AssertOp, ProjectionBatchIn, RetractOp


# ------------------------------------------------------------------ 工具
def _formal_revision(cur, for_update: bool = False) -> int:
    sql = "SELECT revision FROM revision_counter WHERE id = TRUE"
    if for_update:
        sql += " FOR UPDATE"
    cur.execute(sql)
    return cur.fetchone()[0]


def _formal_active(cur) -> List[Assertion]:
    cur.execute(
        "SELECT evidence_id, stratum_a, stratum_b, relation "
        "FROM assertions WHERE status = 'active' ORDER BY evidence_id"
    )
    return [Assertion(evidence_id=r[0], a=r[1], b=r[2], relation=r[3]) for r in cur.fetchall()]


def _get_projection(cur, projection_id: int, for_update: bool = False) -> Dict[str, Any]:
    sql = (
        "SELECT id, label, base_revision, base_strata, base_assertions, "
        "projection_revision, status, merged_into_revision "
        "FROM projections WHERE id = %s"
    )
    if for_update:
        sql += " FOR UPDATE"
    cur.execute(sql, (projection_id,))
    row = cur.fetchone()
    if row is None:
        raise ApiError(404, "PROJECTION_NOT_FOUND", f"推演不存在: {projection_id}", {"projection_id": projection_id})
    return {
        "id": row[0],
        "label": row[1],
        "base_revision": row[2],
        "base_strata": list(row[3]),
        "base_assertions": list(row[4]),
        "projection_revision": row[5],
        "status": row[6],
        "merged_into_revision": row[7],
    }


def _require_open(proj: Dict[str, Any]) -> None:
    if proj["status"] != "open":
        raise ApiError(
            409,
            "PROJECTION_CLOSED",
            f"推演 {proj['id']} 已关闭（{proj['status']}），不可再操作",
            {"projection_id": proj["id"], "status": proj["status"]},
        )


def _projection_added(cur, projection_id: int) -> List[Dict[str, Any]]:
    """推演内新增的全部断言（含在推演内又撤回的）。"""
    cur.execute(
        "SELECT evidence_id, stratum_a, stratum_b, relation, status, added_revision, retracted_revision "
        "FROM projection_assertions WHERE projection_id = %s ORDER BY evidence_id",
        (projection_id,),
    )
    return [
        {
            "evidence_id": r[0],
            "stratum_a": r[1],
            "stratum_b": r[2],
            "relation": r[3],
            "status": r[4],
            "added_revision": r[5],
            "retracted_revision": r[6],
        }
        for r in cur.fetchall()
    ]


def _projection_retractions(cur, projection_id: int) -> List[Dict[str, Any]]:
    cur.execute(
        "SELECT evidence_id, retracted_revision FROM projection_retractions "
        "WHERE projection_id = %s ORDER BY evidence_id",
        (projection_id,),
    )
    return [{"evidence_id": r[0], "retracted_revision": r[1]} for r in cur.fetchall()]


def effective_from(base_assertions, retracted_ids, added) -> List[Assertion]:
    """纯函数：由基线断言快照、撤回编号集合、推演新增断言（含状态）推导生效断言。"""
    result: List[Assertion] = []
    for a in base_assertions:
        if a["evidence_id"] in retracted_ids:
            continue
        result.append(Assertion(evidence_id=a["evidence_id"], a=a["stratum_a"], b=a["stratum_b"], relation=a["relation"]))
    for a in added:
        if a["status"] != "active":
            continue
        result.append(Assertion(evidence_id=a["evidence_id"], a=a["stratum_a"], b=a["stratum_b"], relation=a["relation"]))
    return result


def _load_view(cur, projection_id: int, proj: Optional[Dict[str, Any]] = None):
    """读取推演当前视图：(投影行, 生效断言列表, 被撤回基线编号集合)。"""
    proj = proj or _get_projection(cur, projection_id)
    added = _projection_added(cur, projection_id)
    retracted = {r["evidence_id"] for r in _projection_retractions(cur, projection_id)}
    effective = effective_from(proj["base_assertions"], retracted, added)
    return proj, effective, retracted


def _projection_path_details(
    cur,
    projection_id: int,
    path: Optional[List[str]],
    retracted_ids: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """把证据编号序列展开为逐步推导链；标注证据来源（base 基线 / projection 推演新增）。

    status 反映**推演视角**：基线证据若在推演中被撤回则不可能出现在生效路径上，
    因此路径中的基线证据在该冻结视图中恒为 active（即使其并入后在正式侧又被撤回）。
    """
    if not path:
        return []
    cur.execute(
        "SELECT evidence_id, stratum_a, stratum_b, relation, status "
        "FROM projection_assertions WHERE projection_id = %s AND evidence_id = ANY(%s)",
        (projection_id, list(path)),
    )
    proj_rows = {r[0]: r for r in cur.fetchall()}
    remaining = [e for e in path if e not in proj_rows]
    base_rows: Dict[str, Any] = {}
    if remaining:
        # 基线证据可能已在正式状态被撤回；按编号取历史行（含已撤回）以还原 strata/relation。
        cur.execute(
            "SELECT evidence_id, stratum_a, stratum_b, relation, status "
            "FROM assertions WHERE evidence_id = ANY(%s)",
            (remaining,),
        )
        base_rows = {r[0]: r for r in cur.fetchall()}
    out: List[Dict[str, Any]] = []
    for eid in path:
        pr = proj_rows.get(eid)
        if pr is not None:
            out.append(
                {
                    "evidence_id": eid,
                    "stratum_a": pr[1],
                    "stratum_b": pr[2],
                    "relation": pr[3],
                    "status": pr[4],
                    "scope": "projection",
                }
            )
            continue
        br = base_rows.get(eid)
        if br is None:
            continue
        out.append(
            {
                "evidence_id": eid,
                "stratum_a": br[1],
                "stratum_b": br[2],
                "relation": br[3],
                "status": "retracted" if (retracted_ids and eid in retracted_ids) else "active",
                "scope": "base",
            }
        )
    return out


# ------------------------------------------------------------------ 创建 / 查询
def create_projection(label: Optional[str]) -> Dict[str, Any]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT revision FROM revision_counter WHERE id = TRUE FOR UPDATE")
            base_revision = cur.fetchone()[0]
            cur.execute("SELECT stratum_id FROM strata ORDER BY stratum_id")
            base_strata = [r[0] for r in cur.fetchall()]
            base = _formal_active(cur)
            cur.execute(
                "INSERT INTO projections (label, base_revision, base_strata, base_assertions) "
                "VALUES (%s, %s, %s, %s) RETURNING id, created_at",
                (
                    label,
                    base_revision,
                    Json(base_strata),
                    Json(
                        [
                            {"evidence_id": a.evidence_id, "stratum_a": a.a, "stratum_b": a.b, "relation": a.relation}
                            for a in base
                        ]
                    ),
                ),
            )
            pid, created_at = cur.fetchone()
            return {
                "id": pid,
                "label": label,
                "base_revision": base_revision,
                "projection_revision": 0,
                "status": "open",
                "base_strata": base_strata,
                "base_assertions_count": len(base),
                "created_at": created_at.isoformat(),
            }


def list_projections() -> List[Dict[str, Any]]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, label, base_revision, projection_revision, status, merged_into_revision, created_at, closed_at "
                "FROM projections ORDER BY id"
            )
            return [
                {
                    "id": r[0],
                    "label": r[1],
                    "base_revision": r[2],
                    "projection_revision": r[3],
                    "status": r[4],
                    "merged_into_revision": r[5],
                    "created_at": r[6].isoformat(),
                    "closed_at": r[7].isoformat() if r[7] else None,
                }
                for r in cur.fetchall()
            ]


def _summary(cur, proj: Dict[str, Any]) -> Dict[str, Any]:
    added = _projection_added(cur, proj["id"])
    retracted = _projection_retractions(cur, proj["id"])
    net_added = [a for a in added if a["status"] == "active"]
    base_ids = {a["evidence_id"] for a in proj["base_assertions"]}
    net_retracted = [r for r in retracted if r["evidence_id"] in base_ids]
    return {
        "id": proj["id"],
        "label": proj["label"],
        "base_revision": proj["base_revision"],
        "projection_revision": proj["projection_revision"],
        "status": proj["status"],
        "merged_into_revision": proj["merged_into_revision"],
        "base_strata": proj["base_strata"],
        "base_assertions_count": len(proj["base_assertions"]),
        "added": added,
        "retractions": retracted,
        "net_changes": {
            "added": [
                {
                    "evidence_id": a["evidence_id"],
                    "stratum_a": a["stratum_a"],
                    "stratum_b": a["stratum_b"],
                    "relation": a["relation"],
                    "added_revision": a["added_revision"],
                }
                for a in net_added
            ],
            "retracted": net_retracted,
            "count": len(net_added) + len(net_retracted),
        },
    }


def get_projection(projection_id: int) -> Dict[str, Any]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            return _summary(cur, _get_projection(cur, projection_id))


def discard_projection(projection_id: int) -> Dict[str, Any]:
    """放弃推演：仅 open 可放弃；放弃后不可再操作、不可并入。"""
    with db_conn() as conn:
        with conn.cursor() as cur:
            proj = _get_projection(cur, projection_id, for_update=True)
            _require_open(proj)
            cur.execute(
                "UPDATE projections SET status = 'discarded', closed_at = now() WHERE id = %s",
                (projection_id,),
            )
    return {"id": projection_id, "status": "discarded"}


# ------------------------------------------------------------------ 推演内批次
def apply_projection_batch(projection_id: int, batch: ProjectionBatchIn) -> Dict[str, Any]:
    """在推演内应用一批变更，语义与正式批次一致：
    - 携带推演修订号，不一致 -> STALE_REVISION；
    - 未登记地层（以基线地层为准）/ 正式历史已用编号 / 推演内编号复用 /
      撤回不存在或已撤回的断言 -> 整批拒绝；
    - 对整批应用后的推演最终关系核验，冲突附证据路径；
    - 全部通过则原子递增推演修订号。正式状态始终不变。
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            proj = _get_projection(cur, projection_id, for_update=True)
            _require_open(proj)
            if batch.expected_revision != proj["projection_revision"]:
                raise ApiError(
                    409,
                    "STALE_REVISION",
                    f"推演修订号过期：请求携带 {batch.expected_revision}，当前为 {proj['projection_revision']}",
                    {"projection_id": projection_id, "current_revision": proj["projection_revision"]},
                )
            new_revision = proj["projection_revision"] + 1
            base_strata = set(proj["base_strata"])
            base_ids = {a["evidence_id"] for a in proj["base_assertions"]}

            # 推演内既有状态（本批次之前）
            cur.execute(
                "SELECT evidence_id, status FROM projection_assertions WHERE projection_id = %s",
                (projection_id,),
            )
            added_map: Dict[str, str] = {r[0]: r[1] for r in cur.fetchall()}
            cur.execute(
                "SELECT evidence_id FROM projection_retractions WHERE projection_id = %s",
                (projection_id,),
            )
            retracted_set = {r[0] for r in cur.fetchall()}

            # 本批次内的编号占用与撤回目标（同一批次内也不允许复用/重复撤回）
            batch_assert_ids: set = set()
            batch_retract_ids: set = set()

            for op in batch.operations:
                if isinstance(op, AssertOp):
                    if op.stratum_a not in base_strata or op.stratum_b not in base_strata:
                        bad = op.stratum_a if op.stratum_a not in base_strata else op.stratum_b
                        raise ApiError(
                            400,
                            "INVALID_STRATUM",
                            f"地层未登记（推演基线内不存在）: {bad}",
                            {"projection_id": projection_id, "stratum_id": bad},
                        )
                    eid = op.evidence_id
                    # 正式历史编号（含已撤回）永久不可复用
                    cur.execute("SELECT 1 FROM assertions WHERE evidence_id = %s", (eid,))
                    if cur.fetchone() is not None:
                        raise ApiError(
                            409,
                            "DUPLICATE_EVIDENCE",
                            f"证据编号已被正式历史占用（含已撤回），推演不可复用: {eid}",
                            {"projection_id": projection_id, "evidence_id": eid, "scope": "formal"},
                        )
                    # 推演内既有编号（含推演内已撤回）不可复用
                    if eid in added_map:
                        raise ApiError(
                            409,
                            "DUPLICATE_EVIDENCE",
                            f"证据编号在本推演内已存在（含已撤回）: {eid}",
                            {"projection_id": projection_id, "evidence_id": eid, "scope": "projection"},
                        )
                    if eid in batch_assert_ids:
                        raise ApiError(
                            409,
                            "DUPLICATE_EVIDENCE",
                            f"证据编号在同一批内重复: {eid}",
                            {"projection_id": projection_id, "evidence_id": eid, "scope": "batch"},
                        )
                    batch_assert_ids.add(eid)
                    cur.execute(
                        "INSERT INTO projection_assertions "
                        "(projection_id, evidence_id, stratum_a, stratum_b, relation, status, added_revision) "
                        "VALUES (%s, %s, %s, %s, %s, 'active', %s)",
                        (projection_id, eid, op.stratum_a, op.stratum_b, op.relation, new_revision),
                    )
                    added_map[eid] = "active"
                elif isinstance(op, RetractOp):
                    eid = op.evidence_id
                    if eid in added_map:
                        # 撤回推演内新增断言
                        if added_map[eid] != "active":
                            raise ApiError(
                                409,
                                "ASSERTION_NOT_FOUND",
                                f"撤回失败：推演证据 {eid} 已处于撤回状态",
                                {"projection_id": projection_id, "evidence_id": eid, "scope": "projection"},
                            )
                        if eid in batch_retract_ids:
                            raise ApiError(
                                409,
                                "ASSERTION_NOT_FOUND",
                                f"撤回失败：证据 {eid} 在同一批内被重复撤回",
                                {"projection_id": projection_id, "evidence_id": eid, "scope": "batch"},
                            )
                        batch_retract_ids.add(eid)
                        cur.execute(
                            "UPDATE projection_assertions SET status = 'retracted', "
                            "retracted_revision = %s, retracted_at = now() "
                            "WHERE projection_id = %s AND evidence_id = %s",
                            (new_revision, projection_id, eid),
                        )
                        added_map[eid] = "retracted"
                    else:
                        # 撤回基线断言
                        if eid not in base_ids:
                            raise ApiError(
                                409,
                                "ASSERTION_NOT_FOUND",
                                f"撤回失败：证据编号在推演基线中不存在: {eid}",
                                {"projection_id": projection_id, "evidence_id": eid, "scope": "projection"},
                            )
                        if eid in retracted_set:
                            raise ApiError(
                                409,
                                "ASSERTION_NOT_FOUND",
                                f"撤回失败：基线证据 {eid} 已在本推演中撤回",
                                {"projection_id": projection_id, "evidence_id": eid, "scope": "projection"},
                            )
                        if eid in batch_retract_ids:
                            raise ApiError(
                                409,
                                "ASSERTION_NOT_FOUND",
                                f"撤回失败：证据 {eid} 在同一批内被重复撤回",
                                {"projection_id": projection_id, "evidence_id": eid, "scope": "batch"},
                            )
                        batch_retract_ids.add(eid)
                        cur.execute(
                            "INSERT INTO projection_retractions (projection_id, evidence_id, retracted_revision) "
                            "VALUES (%s, %s, %s)",
                            (projection_id, eid, new_revision),
                        )
                        retracted_set.add(eid)

            # 对整批应用后的推演最终关系核验
            final_added = _projection_added(cur, projection_id)
            effective = effective_from(proj["base_assertions"], retracted_set, final_added)
            conflicts = Derivation(effective).validate()
            if conflicts:
                raise ApiError(
                    409,
                    "CONFLICT",
                    "推演批次与当前推演断言矛盾，整批拒绝，推演状态不变",
                    {"projection_id": projection_id, "conflicts": conflicts},
                )

            cur.execute(
                "UPDATE projections SET projection_revision = %s WHERE id = %s",
                (new_revision, projection_id),
            )
            cur.execute(
                "INSERT INTO projection_revisions (projection_id, revision, batch) VALUES (%s, %s, %s)",
                (projection_id, new_revision, Json(batch.model_dump())),
            )
            return {
                "id": projection_id,
                "projection_revision": new_revision,
                "applied": len(batch.operations),
            }


# ------------------------------------------------------------------ 推演内关系查询
def query_projection_relationship(projection_id: int, a: str, b: str) -> Dict[str, Any]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            proj = _get_projection(cur, projection_id)
            if a not in proj["base_strata"]:
                raise ApiError(400, "INVALID_STRATUM", f"地层未登记（推演基线内不存在）: {a}", {"stratum_id": a})
            if b not in proj["base_strata"]:
                raise ApiError(400, "INVALID_STRATUM", f"地层未登记（推演基线内不存在）: {b}", {"stratum_id": b})
            _, effective, view_retracted = _load_view(cur, projection_id, proj)
            derivation = Derivation(effective)
            relation, path = derivation.relation(a, b)

            # path_detail（含 scope 标注；status 取推演视角）
            path_detail = _projection_path_details(cur, projection_id, path, retracted_ids=view_retracted)
            return {
                "projection_id": projection_id,
                "stratum_a": a,
                "stratum_b": b,
                "relation": relation,
                "relation_label": RELATION_LABELS[relation],
                "evidence_path": path,
                "path_detail": path_detail,
                "base_revision": proj["base_revision"],
                "projection_revision": proj["projection_revision"],
                "status": proj["status"],
            }


# ------------------------------------------------------------------ 并入正式状态
def merge_projection(projection_id: int) -> Dict[str, Any]:
    """把推演相对基线的净变更应用到最新正式状态并重新核验。

    结构性失败（基线目标已在正式状态撤回 / 编号已被正式占用）给出具体原因；
    关系冲突附证据路径。任一失败：正式状态与推演均不变。
    核验通过：作为一次正式批次原子提交并关闭推演。
    净变更为空：只关闭推演，不递增正式修订号。
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            proj = _get_projection(cur, projection_id, for_update=True)
            _require_open(proj)

            # 锁定正式计数器：并入与正式批次互斥，保证“最新正式状态”一致
            current = _formal_revision(cur, for_update=True)

            added = _projection_added(cur, projection_id)
            retracted = _projection_retractions(cur, projection_id)
            base_by_id = {a["evidence_id"]: a for a in proj["base_assertions"]}
            base_ids = set(base_by_id)

            # ---- 净变更 ----
            net_added = [a for a in added if a["status"] == "active"]
            net_retracted = [r for r in retracted if r["evidence_id"] in base_ids]

            # ---- 针对最新正式状态的结构核验（fail-fast，给出具体原因）----
            for r in net_retracted:
                eid = r["evidence_id"]
                cur.execute("SELECT status FROM assertions WHERE evidence_id = %s", (eid,))
                row = cur.fetchone()
                # 正式证据只增不删：基线快照中的编号在正式表必然存在；非 active 即已被正式撤回。
                if row is None or row[0] != "active":
                    raise ApiError(
                        409,
                        "MERGE_TARGET_RETRACTED",
                        f"并入失败：撤回目标证据 {eid} 已在最新正式状态（修订号 {current}）被撤回或不存在",
                        {
                            "projection_id": projection_id,
                            "evidence_id": eid,
                            "reason": "not_active_in_formal",
                            "current_revision": current,
                        },
                    )

            for a in net_added:
                eid = a["evidence_id"]
                cur.execute("SELECT 1 FROM assertions WHERE evidence_id = %s", (eid,))
                if cur.fetchone() is not None:
                    raise ApiError(
                        409,
                        "MERGE_EVIDENCE_TAKEN",
                        f"并入失败：推演新增证据编号 {eid} 已被正式历史占用",
                        {
                            "projection_id": projection_id,
                            "evidence_id": eid,
                            "current_revision": current,
                        },
                    )

            # ---- 对净变更应用后的最新正式状态做关系核验 ----
            formal = _formal_active(cur)
            retracted_ids = {r["evidence_id"] for r in net_retracted}
            merged_assertions = [
                a for a in formal if a.evidence_id not in retracted_ids
            ]
            for a in net_added:
                merged_assertions.append(
                    Assertion(evidence_id=a["evidence_id"], a=a["stratum_a"], b=a["stratum_b"], relation=a["relation"])
                )
            conflicts = Derivation(merged_assertions).validate()
            if conflicts:
                raise ApiError(
                    409,
                    "MERGE_CONFLICT",
                    "并入核验发现关系冲突，正式状态与推演均不变",
                    {
                        "projection_id": projection_id,
                        "base_revision": proj["base_revision"],
                        "current_revision": current,
                        "conflicts": conflicts,
                    },
                )

            change_count = len(net_added) + len(net_retracted)
            if change_count == 0:
                # 净变更为空：只关闭推演，不递增正式修订号
                cur.execute(
                    "UPDATE projections SET status = 'merged', closed_at = now(), merged_into_revision = %s "
                    "WHERE id = %s",
                    (current, projection_id),
                )
                return {
                    "id": projection_id,
                    "merged": True,
                    "applied": 0,
                    "revision": current,
                    "revision_incremented": False,
                    "net_changes": {"added": [], "retracted": [], "count": 0},
                }

            # 关系变化追踪：并入前 formal=旧侧当时状态，merged_assertions=新侧状态
            cur.execute("SELECT stratum_id FROM strata ORDER BY stratum_id")
            formal_strata = [r[0] for r in cur.fetchall()]
            diffs = relation_diffs(formal_strata, formal, merged_assertions)

            # ---- 作为一次正式批次原子提交 ----
            new_revision = current + 1
            operations: List[Dict[str, Any]] = []
            for a in net_added:
                cur.execute(
                    "INSERT INTO assertions (evidence_id, stratum_a, stratum_b, relation, status, added_revision) "
                    "VALUES (%s, %s, %s, %s, 'active', %s)",
                    (a["evidence_id"], a["stratum_a"], a["stratum_b"], a["relation"], new_revision),
                )
                operations.append(
                    {
                        "op": "assert",
                        "evidence_id": a["evidence_id"],
                        "stratum_a": a["stratum_a"],
                        "stratum_b": a["stratum_b"],
                        "relation": a["relation"],
                    }
                )
            for r in net_retracted:
                cur.execute(
                    "UPDATE assertions SET status = 'retracted', retracted_revision = %s, retracted_at = now() "
                    "WHERE evidence_id = %s",
                    (new_revision, r["evidence_id"]),
                )
                operations.append({"op": "retract", "evidence_id": r["evidence_id"]})

            cur.execute("UPDATE revision_counter SET revision = %s WHERE id = TRUE", (new_revision,))
            cur.execute(
                "INSERT INTO revisions (revision, batch) VALUES (%s, %s)",
                (
                    new_revision,
                    Json(
                        {
                            "source": "projection_merge",
                            "projection_id": projection_id,
                            "base_revision": proj["base_revision"],
                            "operations": operations,
                        }
                    ),
                ),
            )
            # 变化记录与并入提交同事务原子写入
            insert_revision_changes(cur, new_revision, diffs, "projection_merge")
            cur.execute(
                "UPDATE projections SET status = 'merged', closed_at = now(), "
                "merged_into_revision = %s WHERE id = %s",
                (new_revision, projection_id),
            )
            return {
                "id": projection_id,
                "merged": True,
                "applied": change_count,
                "revision": new_revision,
                "revision_incremented": True,
                "relation_changes": len(diffs),
                "net_changes": {
                    "added": [
                        {
                            "evidence_id": a["evidence_id"],
                            "stratum_a": a["stratum_a"],
                            "stratum_b": a["stratum_b"],
                            "relation": a["relation"],
                        }
                        for a in net_added
                    ],
                    "retracted": [{"evidence_id": r["evidence_id"]} for r in net_retracted],
                    "count": change_count,
                },
            }
