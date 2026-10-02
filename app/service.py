"""业务逻辑：批次应用、关系查询、定版快照。"""
from typing import Any, Dict, List, Optional

from psycopg2.extras import Json

from .db import db_conn
from .changes import insert_revision_changes, relation_diffs
from .derivation import Assertion, Derivation, RELATION_LABELS
from .errors import ApiError
from .models import AssertOp, BatchIn, RetractOp


# ------------------------------------------------------------------ 工具
def _current_revision(cur) -> int:
    cur.execute("SELECT revision FROM revision_counter WHERE id = TRUE")
    return cur.fetchone()[0]


def _load_active(cur) -> List[Assertion]:
    cur.execute(
        "SELECT evidence_id, stratum_a, stratum_b, relation "
        "FROM assertions WHERE status = 'active' ORDER BY evidence_id"
    )
    return [Assertion(evidence_id=r[0], a=r[1], b=r[2], relation=r[3]) for r in cur.fetchall()]


def _load_strata(cur) -> List[str]:
    cur.execute("SELECT stratum_id FROM strata ORDER BY stratum_id")
    return [r[0] for r in cur.fetchall()]


def _ensure_strata(cur, *stratum_ids: str) -> None:
    for s in stratum_ids:
        cur.execute("SELECT 1 FROM strata WHERE stratum_id = %s", (s,))
        if cur.fetchone() is None:
            raise ApiError(400, "INVALID_STRATUM", f"地层未登记: {s}", {"stratum_id": s})


def _path_details(cur, path: Optional[List[str]]) -> List[Dict[str, Any]]:
    """把证据编号序列展开为可读的逐步推导链。"""
    if not path:
        return []
    cur.execute(
        "SELECT evidence_id, stratum_a, stratum_b, relation, status "
        "FROM assertions WHERE evidence_id = ANY(%s)",
        (list(path),),
    )
    rows = {r[0]: r for r in cur.fetchall()}
    return [
        {
            "evidence_id": eid,
            "stratum_a": rows[eid][1],
            "stratum_b": rows[eid][2],
            "relation": rows[eid][3],
            "status": rows[eid][4],
        }
        for eid in path
        if eid in rows
    ]


def _relation_payload(relation: str, path: Optional[List[str]], cur) -> Dict[str, Any]:
    return {
        "relation": relation,
        "relation_label": RELATION_LABELS[relation],
        "evidence_path": path,
        "path_detail": _path_details(cur, path),
    }


# ------------------------------------------------------------------ 批次
def apply_batch(batch: BatchIn) -> Dict[str, Any]:
    """应用一批断言变更。

    - 携带修订号与当前不一致 -> STALE_REVISION，整批拒绝；
    - 任一操作非法（无效地层/重复证据编号/撤回不存在）-> 整批拒绝；
    - 对整批应用后的最终关系核验，发现组内早于或早于闭环 -> 整批拒绝并给出冲突证据路径；
    - 全部通过 -> 同一事务内落库并原子递增修订号。
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT revision FROM revision_counter WHERE id = TRUE FOR UPDATE")
            current = cur.fetchone()[0]
            if batch.expected_revision != current:
                raise ApiError(
                    409,
                    "STALE_REVISION",
                    f"修订号过期：请求携带 {batch.expected_revision}，当前为 {current}",
                    {"current_revision": current},
                )
            new_revision = current + 1

            # 变化追踪旧侧：本批次应用前的当时生效断言集（后来撤回的证据在旧侧仍生效）
            before_assertions = _load_active(cur)

            for op in batch.operations:
                if isinstance(op, AssertOp):
                    _ensure_strata(cur, op.stratum_a, op.stratum_b)
                    cur.execute("SELECT 1 FROM assertions WHERE evidence_id = %s", (op.evidence_id,))
                    if cur.fetchone() is not None:
                        raise ApiError(
                            409,
                            "DUPLICATE_EVIDENCE",
                            f"证据编号已存在（含已撤回）: {op.evidence_id}",
                            {"evidence_id": op.evidence_id},
                        )
                    cur.execute(
                        "INSERT INTO assertions (evidence_id, stratum_a, stratum_b, relation, status, added_revision) "
                        "VALUES (%s, %s, %s, %s, 'active', %s)",
                        (op.evidence_id, op.stratum_a, op.stratum_b, op.relation, new_revision),
                    )
                elif isinstance(op, RetractOp):
                    cur.execute(
                        "SELECT status FROM assertions WHERE evidence_id = %s FOR UPDATE",
                        (op.evidence_id,),
                    )
                    row = cur.fetchone()
                    if row is None:
                        raise ApiError(
                            409,
                            "ASSERTION_NOT_FOUND",
                            f"撤回失败：证据编号不存在: {op.evidence_id}",
                            {"evidence_id": op.evidence_id},
                        )
                    if row[0] != "active":
                        raise ApiError(
                            409,
                            "ASSERTION_NOT_FOUND",
                            f"撤回失败：证据 {op.evidence_id} 已处于撤回状态",
                            {"evidence_id": op.evidence_id},
                        )
                    cur.execute(
                        "UPDATE assertions SET status = 'retracted', retracted_revision = %s, retracted_at = now() "
                        "WHERE evidence_id = %s",
                        (new_revision, op.evidence_id),
                    )

            # 对整批应用后的最终关系核验
            after_assertions = _load_active(cur)
            derivation = Derivation(after_assertions)
            conflicts = derivation.validate()
            if conflicts:
                raise ApiError(
                    409,
                    "CONFLICT",
                    "批次与现有断言矛盾，整批拒绝，原状态不变",
                    {"conflicts": conflicts},
                )

            # 关系变化追踪：按提交前/后的当时状态逐对比较，与修订号同事务原子落库
            diffs = relation_diffs(_load_strata(cur), before_assertions, after_assertions)

            cur.execute("UPDATE revision_counter SET revision = %s WHERE id = TRUE", (new_revision,))
            cur.execute(
                "INSERT INTO revisions (revision, batch) VALUES (%s, %s)",
                (new_revision, Json(batch.model_dump())),
            )
            insert_revision_changes(cur, new_revision, diffs, "formal_batch")
            return {"revision": new_revision, "applied": len(batch.operations), "relation_changes": len(diffs)}


# ------------------------------------------------------------------ 查询
def query_relationship(a: str, b: str) -> Dict[str, Any]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            _ensure_strata(cur, a, b)
            revision = _current_revision(cur)
            derivation = Derivation(_load_active(cur))
            relation, path = derivation.relation(a, b)
            payload = _relation_payload(relation, path, cur)
            payload.update({"stratum_a": a, "stratum_b": b, "revision": revision})
            return payload


# ------------------------------------------------------------------ 定版
def create_snapshot(label: Optional[str]) -> Dict[str, Any]:
    """定版：把当前修订号下全部可确定的关系结论固化为不可变快照。"""
    with db_conn() as conn:
        with conn.cursor() as cur:
            # 锁定修订计数器，保证快照与某一修订严格对应
            cur.execute("SELECT revision FROM revision_counter WHERE id = TRUE FOR UPDATE")
            revision = cur.fetchone()[0]
            cur.execute("SELECT stratum_id FROM strata ORDER BY stratum_id")
            strata = [r[0] for r in cur.fetchall()]
            active = _load_active(cur)
            derivation = Derivation(active)

            cur.execute(
                "INSERT INTO snapshots (revision, label, assertions) VALUES (%s, %s, %s) RETURNING version",
                (
                    revision,
                    label,
                    Json(
                        [
                            {"evidence_id": x.evidence_id, "stratum_a": x.a, "stratum_b": x.b, "relation": x.relation}
                            for x in active
                        ]
                    ),
                ),
            )
            version = cur.fetchone()[0]

            determined = 0
            for x in strata:
                for y in strata:
                    if x == y:
                        continue
                    relation, path = derivation.relation(x, y)
                    if relation == "undetermined":
                        continue
                    cur.execute(
                        "INSERT INTO snapshot_conclusions (version, stratum_a, stratum_b, relation, path) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (version, x, y, relation, Json(path)),
                    )
                    determined += 1
            return {"version": version, "revision": revision, "conclusions": determined}


def get_snapshot(version: int) -> Dict[str, Any]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT version, revision, label, assertions, created_at FROM snapshots WHERE version = %s",
                (version,),
            )
            row = cur.fetchone()
            if row is None:
                raise ApiError(404, "SNAPSHOT_NOT_FOUND", f"版本不存在: {version}", {"version": version})
            return {
                "version": row[0],
                "revision": row[1],
                "label": row[2],
                "assertions": row[3],
                "created_at": row[4].isoformat(),
            }


def query_snapshot_relationship(version: int, a: str, b: str) -> Dict[str, Any]:
    """查询历史版本结论。快照不可变，后续撤回不影响旧版结论。"""
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT revision FROM snapshots WHERE version = %s", (version,))
            row = cur.fetchone()
            if row is None:
                raise ApiError(404, "SNAPSHOT_NOT_FOUND", f"版本不存在: {version}", {"version": version})
            snapshot_revision = row[0]
            _ensure_strata(cur, a, b)

            if a == b:
                relation, path = "same_period", []
            else:
                cur.execute(
                    "SELECT relation, path FROM snapshot_conclusions "
                    "WHERE version = %s AND stratum_a = %s AND stratum_b = %s",
                    (version, a, b),
                )
                row = cur.fetchone()
                if row is None:
                    relation, path = "undetermined", None
                else:
                    relation, path = row[0], list(row[1])

            payload = _relation_payload(relation, path, cur)
            payload.update(
                {
                    "stratum_a": a,
                    "stratum_b": b,
                    "version": version,
                    "snapshot_revision": snapshot_revision,
                }
            )
            return payload
