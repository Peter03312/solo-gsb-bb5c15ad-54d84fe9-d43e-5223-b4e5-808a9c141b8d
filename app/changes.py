"""正式修订关系变化追踪。

对每次**成功的正式断言批次**及**推演并入产生的正式修订**，记录本修订提交前后
有变化的地层对（同一对按地层编号固定顺序只列一次）：

- 关系（含方向：same_period / earlier_than / later_than）发生变化；
- 关系不变但最优支撑证据路径发生变化（路径选取规则沿用 derivation：
  证据数最少、并列按证据编号序列字典序最小）；
- 变为未定（undetermined）时证据路径为空数组。

记录在提交事务内与断言落库、修订号递增原子写入，因此：
- 失败批次、推演内编辑、无净变更并入（不产生正式修订号）不产生变化记录；
- 并发提交时不可能读到“半批结果”，也不会漏项或重复。

历史可追溯性：旧侧关系与路径均按**该修订提交前**的当时状态推导。后来才被
撤回的证据在旧侧仍按当时生效解释（提交时刻已能拿到完整的当时断言集，无需
回放后来的撤回）。服务启动时对部署前已存在的历史修订做幂等回填，使“已有
历史修订也应可追溯”。
"""
from __future__ import annotations

import base64
import json
from typing import Any, Dict, List, Optional, Sequence

from psycopg2.extras import Json

from .derivation import Assertion, Derivation, RELATION_LABELS
from .errors import ApiError


# ------------------------------------------------------------------ 差异计算
def relation_diffs(
    strata: Sequence[str],
    before_assertions: Sequence[Assertion],
    after_assertions: Sequence[Assertion],
) -> List[Dict[str, Any]]:
    """纯函数：对固定地层集合，逐对比较提交前后关系结论与最优支撑路径。

    返回有序变化列表（按地层对固定顺序）；关系不变且路径不变的对不输出。
    未定向路径用空列表表示。
    """
    before = Derivation(before_assertions)
    after = Derivation(after_assertions)
    ordered = sorted(strata)
    out: List[Dict[str, Any]] = []
    for i, a in enumerate(ordered):
        for b in ordered[i + 1 :]:
            old_rel, old_path = before.relation(a, b)
            new_rel, new_path = after.relation(a, b)
            old_path = old_path or []
            new_path = new_path or []
            if old_rel != new_rel or old_path != new_path:
                out.append(
                    {
                        "stratum_a": a,
                        "stratum_b": b,
                        "old_relation": old_rel,
                        "new_relation": new_rel,
                        "old_path": old_path,
                        "new_path": new_path,
                    }
                )
    return out


def insert_revision_changes(cur, revision: int, diffs: List[Dict[str, Any]], source: str) -> None:
    """把某修订的变化记录写入 revision_changes（调用方负责事务）。

    与断言落库、修订号递增同事务，随后一并落封口标记：未封口的修订才可能被
    启动回填补齐，封口保证回填与在线写入不会重复。
    """
    if diffs:
        cur.executemany(
            "INSERT INTO revision_changes "
            "(revision, seq, stratum_a, stratum_b, old_relation, new_relation, old_path, new_path, source) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (
                    revision,
                    seq,
                    d["stratum_a"],
                    d["stratum_b"],
                    d["old_relation"],
                    d["new_relation"],
                    Json(d["old_path"]),
                    Json(d["new_path"]),
                    source,
                )
                for seq, d in enumerate(diffs, start=1)
            ],
        )
    cur.execute("INSERT INTO revision_changes_sealed (revision, source) VALUES (%s, %s)", (revision, source))


# ------------------------------------------------------------------ 历史回填
def _active_assertions_at(cur, revision: Optional[int]) -> List[Assertion]:
    """某修订提交时刻处于生效状态的断言。

    断言只增不删、撤回仅置状态并记录修订号：added_revision <= revision 且
    （未撤回 或 retracted_revision > revision）即当时生效；revision=None
    表示首个修订之前的空状态。
    """
    if revision is None:
        return []
    cur.execute(
        "SELECT evidence_id, stratum_a, stratum_b, relation "
        "FROM assertions "
        "WHERE added_revision <= %s AND (retracted_revision IS NULL OR retracted_revision > %s) "
        "ORDER BY evidence_id",
        (revision, revision),
    )
    return [Assertion(evidence_id=r[0], a=r[1], b=r[2], relation=r[3]) for r in cur.fetchall()]


def backfill_revision_changes(cur) -> int:
    """启动时幂等回填历史修订的变化记录，返回回填的修订数。

    以每个历史修订为界：地层取截至该修订提交时刻已登记的；
    旧侧断言集取上一修订时刻（最早修订取空集），新侧取本修订时刻。
    source 尽量按当时审计记录还原（推演并入的 batch 带 source=projection_merge），
    其余为正式批次。在线提交与启动回填都写 revision_changes_sealed 封口标记，
    故不重复。
    """
    # 锁定修订计数器，回填与在线提交互斥（启动阶段实际无并发，双保险）
    cur.execute("SELECT revision FROM revision_counter WHERE id = TRUE FOR UPDATE")
    cur.execute(
        "SELECT revision, applied_at, batch FROM revisions "
        "WHERE revision NOT IN (SELECT revision FROM revision_changes_sealed) "
        "ORDER BY revision"
    )
    pending = cur.fetchall()
    if not pending:
        return 0

    cur.execute("SELECT stratum_id, created_at FROM strata")
    strata_created = {r[0]: r[1] for r in cur.fetchall()}

    prev_revision: Optional[int] = None
    for revision, applied_at, batch in pending:
        before_assertions = _active_assertions_at(cur, prev_revision)
        after_assertions = _active_assertions_at(cur, revision)
        strata = [s for s, created in strata_created.items() if created <= applied_at]
        diffs = relation_diffs(strata, before_assertions, after_assertions)
        if isinstance(batch, dict) and batch.get("source") == "projection_merge":
            source = "projection_merge"
        elif isinstance(batch, dict) and batch.get("source") == "field_package":
            source = "field_package"
        else:
            source = "formal_batch"
        insert_revision_changes(cur, revision, diffs, source)
        prev_revision = revision
    return len(pending)


# ------------------------------------------------------------------ 游标分页
_CURSOR_VERSION = 1


def encode_cursor(revision: int, seq: int, bound: int) -> str:
    raw = json.dumps(
        {"v": _CURSOR_VERSION, "r": revision, "s": seq, "b": bound},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(token: str) -> Dict[str, int]:
    try:
        padding = "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(token + padding).decode()
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("v") != _CURSOR_VERSION:
            raise ValueError("bad version")
        revision, seq, bound = data["r"], data["s"], data["b"]
        if not all(isinstance(x, int) for x in (revision, seq, bound)):
            raise ValueError("bad types")
        if revision < 1 or seq < 1 or bound < 0:
            raise ValueError("bad range")
        return {"revision": revision, "seq": seq, "bound": bound}
    except Exception as exc:
        raise ApiError(400, "INVALID_CURSOR", f"分页游标无效: {token}", {"cursor": token}) from exc


def list_changes(limit: int, cursor_token: Optional[str]) -> Dict[str, Any]:
    """游标分页读取正式修订关系变化。

    - 首次请求（不带 cursor）：固定本轮正式修订上界 bound=当前修订号，
      从最早的修订开始读；
    - 后续请求：原样回传 next_cursor，服务端沿用上轮 bound —— 并发提交产生
      的新修订不会进入本轮窗口，因此不漏项、不重复；读完一轮后再以新的首屏
      请求获取更新的修订。
    - 每页在单条快照事务内读取，提交批次的原子写入保证不会读到半批结果。
    """
    from .db import db_conn

    if cursor_token:
        cursor = decode_cursor(cursor_token)
        bound = cursor["bound"]
    else:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT revision FROM revision_counter WHERE id = TRUE")
                bound = cur.fetchone()[0]
        cursor = None

    with db_conn() as conn:
        # REPEATABLE READ 只读快照：整页为一致性视图，与并发提交事务互不可见半批
        # 状态。SET TRANSACTION 只对当前事务生效，提交/回滚后连接（共享池）的
        # 后续事务恢复默认 READ WRITE/READ COMMITTED。
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY ISOLATION LEVEL REPEATABLE READ")
            cur.execute(
                "SELECT revision, seq, stratum_a, stratum_b, old_relation, new_relation, "
                "old_path, new_path, source "
                "FROM revision_changes "
                "WHERE revision <= %s "
                "AND (%s::int IS NULL OR revision > %s OR (revision = %s AND seq > %s)) "
                "ORDER BY revision, seq LIMIT %s",
                (
                    bound,
                    cursor["revision"] if cursor else None,
                    cursor["revision"] if cursor else None,
                    cursor["revision"] if cursor else None,
                    cursor["seq"] if cursor else None,
                    limit + 1,
                ),
            )
            rows = cur.fetchall()

    has_more = len(rows) > limit
    page = rows[:limit]
    items: List[Dict[str, Any]] = []
    last_revision, last_seq = (cursor["revision"], cursor["seq"]) if cursor else (0, 0)
    for r in page:
        revision, seq = r[0], r[1]
        items.append(
            {
                "revision": revision,
                "stratum_a": r[2],
                "stratum_b": r[3],
                "old_relation": r[4],
                "new_relation": r[5],
                "old_relation_label": RELATION_LABELS[r[4]],
                "new_relation_label": RELATION_LABELS[r[5]],
                "old_evidence_path": list(r[6]),
                "new_evidence_path": list(r[7]),
                "source": r[8],
            }
        )
        last_revision, last_seq = revision, seq

    # 仅当确实取到 lookahead（窗口内还有行）时才给下页游标；
    # 成功修订也可能零变化（如撤回一条从未进入最优路径的平行断言），
    # 此时不得产生指向同一位置的空转游标。
    next_cursor = encode_cursor(last_revision, last_seq, bound) if has_more else None
    return {
        "changes": items,
        "bound_revision": bound,
        "limit": limit,
        "has_more": next_cursor is not None,
        "next_cursor": next_cursor,
    }
