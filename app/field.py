"""野外终端离线整包汇入正式库。

野外终端在无网络条件下用**地层短号**记录“同一期 / 早于”断言，回站后整包汇入：

- 请求携带采集站点、终端编号、包号、预期正式修订号、短号→全库地层编号对照、
  以及带包内唯一序号的断言；**断言端点只能引用本包短号**。
- 对照目标已登记时站点须一致（`SITE_CONFLICT`），未登记时随包建立；
  同一终端的同一短号跨包不可改绑（`SHORT_CODE_REBOUND`）。
- 正式证据编号由 (终端编号, 包号, 序号) **稳定确定**（`evidence_id_for`），
  且不得占用已有编号（`FIELD_EVIDENCE_TAKEN`）。
- 整包沿用正式批次的最终关系核验（组内早于 / 早于闭环，冲突附证据路径）：
  任一错误都不落地层、绑定或断言；成功只递增一次正式修订，与变化记录、
  包台账同事务提交。
- 相同终端 + 包号 + 内容（内容指纹不含 expected_revision）重试原样返回
  首次结果，即使正式修订已前进（`replayed=true`）；异内容复用拒绝
  （`FIELD_PACKAGE_CONTENT_MISMATCH`）。
"""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple
from psycopg2.extras import Json

from .changes import insert_revision_changes, relation_diffs
from .db import db_conn
from .derivation import Assertion, Derivation
from .errors import ApiError
from .models import FieldPackageIn

SEQ_WIDTH = 12  # 序号补零宽度：稳定且字典序友好（上限 10**12-1，由模型 ge=1 与上层约束共同保证）


# ------------------------------------------------------------------ 稳定标识
def _b64url(text: str) -> str:
    """UTF-8 字节的 URL 安全 base64（去填充）。结果只含 [A-Za-z0-9_-]，
    与证据编号的 ':' 分隔符互不冲突，且对任意终端编号 / 包号双射可逆。"""
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def evidence_id_for(terminal_id: str, package_no: str, seq: int) -> str:
    """证据编号由终端、包号、包内序号稳定确定：F:<终端>:<包号>:<序号>。

    终端 / 包号段做无歧义的 base64url 编码；同输入永远得到同一编号，
    不同输入永不碰撞（编码双射 + 固定分隔符）。
    """
    return f"F:{_b64url(terminal_id)}:{_b64url(package_no)}:{seq:0{SEQ_WIDTH}d}"


def canonical_content(body: FieldPackageIn) -> Dict[str, Any]:
    """参与幂等指纹的规范化内容（**不含 expected_revision**）。

    mappings 按短号排序去重（重复短号在结构校验阶段拒绝），
    assertions 按序号排序；指纹只依赖语义内容，与数组输入顺序无关。
    """
    mappings = sorted(
        ({"short_code": m.short_code, "stratum_id": m.stratum_id} for m in body.mappings),
        key=lambda m: m["short_code"],
    )
    assertions = sorted(
        (
            {
                "seq": a.seq,
                "stratum_a": a.stratum_a,
                "stratum_b": a.stratum_b,
                "relation": a.relation,
            }
            for a in body.assertions
        ),
        key=lambda a: a["seq"],
    )
    return {"site": body.site, "mappings": mappings, "assertions": assertions}


def content_hash_of(content: Dict[str, Any]) -> str:
    blob = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


# ------------------------------------------------------------------ 结构校验（事务外）
def validate_structure(body: FieldPackageIn) -> Tuple[Dict[str, str], Dict[int, Dict[str, Any]]]:
    """包内自洽性校验（不访问数据库）：短号不重复、序号不重复、端点只引本包短号。

    返回 (短号→全库编号, 序号→断言) 供汇入使用。
    """
    short_to_full: Dict[str, str] = {}
    for m in body.mappings:
        if m.short_code in short_to_full:
            raise ApiError(
                400,
                "DUPLICATE_SHORT_CODE",
                f"包内短号重复: {m.short_code}",
                {"terminal_id": body.terminal_id, "package_no": body.package_no, "short_code": m.short_code},
            )
        short_to_full[m.short_code] = m.stratum_id

    by_seq: Dict[int, Dict[str, Any]] = {}
    for a in body.assertions:
        if a.seq in by_seq:
            raise ApiError(
                400,
                "DUPLICATE_SEQUENCE",
                f"包内序号重复: {a.seq}",
                {"terminal_id": body.terminal_id, "package_no": body.package_no, "seq": a.seq},
            )
        for endpoint in (a.stratum_a, a.stratum_b):
            if endpoint not in short_to_full:
                raise ApiError(
                    400,
                    "UNKNOWN_SHORT_CODE",
                    f"断言端点引用了本包未对照的短号: {endpoint}（序号 {a.seq}）",
                    {
                        "terminal_id": body.terminal_id,
                        "package_no": body.package_no,
                        "seq": a.seq,
                        "short_code": endpoint,
                    },
                )
        by_seq[a.seq] = {
            "seq": a.seq,
            "short_a": a.stratum_a,
            "short_b": a.stratum_b,
            "full_a": short_to_full[a.stratum_a],
            "full_b": short_to_full[a.stratum_b],
            "relation": a.relation,
        }
    return short_to_full, by_seq


# ------------------------------------------------------------------ 汇入
def ingest_field_package(body: FieldPackageIn) -> Dict[str, Any]:
    """整包汇入正式库（语义见模块文档）。成功响应的 replayed 字段由路由层标注。"""
    short_to_full, by_seq = validate_structure(body)
    content = canonical_content(body)
    content_hash = content_hash_of(content)
    ordered_short_codes = sorted(short_to_full)

    with db_conn() as conn:
        with conn.cursor() as cur:
            # 同一 (终端, 包号) 的请求在事务级咨询锁上串行，包台账写入无竞争。
            # hashtextextended 返回 bigint，对应单键版 pg_advisory_xact_lock(bigint)。
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 9173))",
                (f"{body.terminal_id}\x1f{body.package_no}",),
            )

            # ---- 幂等判定：相同终端 + 包号只成功一次 ----
            cur.execute(
                "SELECT content_hash, stored_response FROM field_packages "
                "WHERE terminal_id = %s AND package_no = %s",
                (body.terminal_id, body.package_no),
            )
            existing = cur.fetchone()
            if existing is not None:
                if existing[0] != content_hash:
                    raise ApiError(
                        409,
                        "FIELD_PACKAGE_CONTENT_MISMATCH",
                        f"终端 {body.terminal_id} 的包号 {body.package_no} 已以不同内容汇入，异内容复用拒绝",
                        {
                            "terminal_id": body.terminal_id,
                            "package_no": body.package_no,
                            "original_content_hash": existing[0],
                            "request_content_hash": content_hash,
                        },
                    )
                response = dict(existing[1])
                response["replayed"] = True
                return response

            # ---- 过期修订（首次汇入才检查；幂等重试即使修订已前进也返回原结果）----
            cur.execute("SELECT revision FROM revision_counter WHERE id = TRUE FOR UPDATE")
            current = cur.fetchone()[0]
            if body.expected_revision != current:
                raise ApiError(
                    409,
                    "STALE_REVISION",
                    f"修订号过期：请求携带 {body.expected_revision}，当前为 {current}",
                    {"current_revision": current, "expected_revision": body.expected_revision},
                )
            new_revision = current + 1

            before_assertions = _load_active(cur)

            # ---- 既有地层站点一致性 ----
            cur.execute("SELECT stratum_id, site FROM strata")
            registered_site: Dict[str, Optional[str]] = {r[0]: r[1] for r in cur.fetchall()}
            new_strata: List[str] = []
            for short in ordered_short_codes:
                full = short_to_full[short]
                if full in registered_site:
                    site = registered_site[full]
                    # 已登记目标的站点（含未知 NULL）与本包站点必须一致
                    if site != body.site:
                        raise ApiError(
                            409,
                            "SITE_CONFLICT",
                            f"对照目标地层 {full} 已登记站点 {site if site is not None else '（未登记站点）'}，与本包站点 {body.site} 不一致",
                            {
                                "terminal_id": body.terminal_id,
                                "package_no": body.package_no,
                                "short_code": short,
                                "stratum_id": full,
                                "registered_site": site,
                                "request_site": body.site,
                            },
                        )
                elif full not in new_strata:
                    new_strata.append(full)

            # ---- 同一终端同一短号跨包不可改绑 ----
            cur.execute(
                "SELECT short_code, stratum_id, package_no FROM field_short_bindings "
                "WHERE terminal_id = %s AND short_code = ANY(%s) FOR UPDATE",
                (body.terminal_id, ordered_short_codes),
            )
            bound: Dict[str, Tuple[str, str]] = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
            for short in ordered_short_codes:
                full = short_to_full[short]
                if short in bound and bound[short][0] != full:
                    prior_full, prior_pkg = bound[short]
                    raise ApiError(
                        409,
                        "SHORT_CODE_REBOUND",
                        f"终端 {body.terminal_id} 的短号 {short} 已在包 {prior_pkg} 绑定 {prior_full}，不可改绑为 {full}",
                        {
                            "terminal_id": body.terminal_id,
                            "short_code": short,
                            "bound_stratum_id": prior_full,
                            "bound_package_no": prior_pkg,
                            "requested_stratum_id": full,
                            "request_package_no": body.package_no,
                        },
                    )

            # ---- 稳定证据编号不得占用已有编号（含已撤回，永久唯一）----
            evidence_by_seq: Dict[int, str] = {}
            for seq in sorted(by_seq):
                eid = evidence_id_for(body.terminal_id, body.package_no, seq)
                evidence_by_seq[seq] = eid
            cur.execute(
                "SELECT evidence_id FROM assertions WHERE evidence_id = ANY(%s)",
                (list(evidence_by_seq.values()),),
            )
            occupied = [r[0] for r in cur.fetchall()]
            if occupied:
                raise ApiError(
                    409,
                    "FIELD_EVIDENCE_TAKEN",
                    f"稳定证据编号已被占用（含已撤回）: {', '.join(sorted(occupied))}",
                    {
                        "terminal_id": body.terminal_id,
                        "package_no": body.package_no,
                        "occupied_evidence_ids": sorted(occupied),
                    },
                )

            # ---- 落地层（未登记随包建立；防御并发登记后再核站点）----
            for full in new_strata:
                cur.execute(
                    "INSERT INTO strata (stratum_id, site) VALUES (%s, %s) ON CONFLICT (stratum_id) DO NOTHING",
                    (full, body.site),
                )
            if new_strata:
                cur.execute(
                    "SELECT stratum_id, site FROM strata WHERE stratum_id = ANY(%s)",
                    (new_strata,),
                )
                rechecked = {r[0]: r[1] for r in cur.fetchall()}
                for full in new_strata:
                    if rechecked.get(full) != body.site:
                        raise ApiError(
                            409,
                            "SITE_CONFLICT",
                            f"对照目标地层 {full} 已登记站点 {rechecked.get(full)}，与本包站点 {body.site} 不一致",
                            {
                                "terminal_id": body.terminal_id,
                                "package_no": body.package_no,
                                "stratum_id": full,
                                "registered_site": rechecked.get(full),
                                "request_site": body.site,
                            },
                        )

            # ---- 落绑定（首次建立的短号）----
            for short in ordered_short_codes:
                if short not in bound:
                    cur.execute(
                        "INSERT INTO field_short_bindings (terminal_id, short_code, stratum_id, site, package_no) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (body.terminal_id, short, short_to_full[short], body.site, body.package_no),
                    )

            # ---- 落断言（证据编号稳定、端点已展开为全库地层编号）----
            for seq in sorted(by_seq):
                item = by_seq[seq]
                cur.execute(
                    "INSERT INTO assertions (evidence_id, stratum_a, stratum_b, relation, status, added_revision) "
                    "VALUES (%s, %s, %s, %s, 'active', %s)",
                    (evidence_by_seq[seq], item["full_a"], item["full_b"], item["relation"], new_revision),
                )

            # ---- 整包应用后的最终关系核验（沿用正式批次口径）----
            after_assertions = _load_active(cur)
            conflicts = Derivation(after_assertions).validate()
            if conflicts:
                raise ApiError(
                    409,
                    "FIELD_CONFLICT",
                    "野外整包与现有断言矛盾，整批拒绝，地层 / 绑定 / 断言均不落地",
                    {
                        "terminal_id": body.terminal_id,
                        "package_no": body.package_no,
                        "current_revision": current,
                        "conflicts": conflicts,
                    },
                )

            # ---- 变化记录：与修订号递增同事务 ----
            cur.execute("SELECT stratum_id FROM strata ORDER BY stratum_id")
            all_strata = [r[0] for r in cur.fetchall()]
            diffs = relation_diffs(all_strata, before_assertions, after_assertions)

            cur.execute("UPDATE revision_counter SET revision = %s WHERE id = TRUE", (new_revision,))
            cur.execute(
                "INSERT INTO revisions (revision, batch) VALUES (%s, %s)",
                (
                    new_revision,
                    Json(
                        {
                            "source": "field_package",
                            "site": body.site,
                            "terminal_id": body.terminal_id,
                            "package_no": body.package_no,
                            "content_hash": content_hash,
                            "operations": [
                                {
                                    "seq": item["seq"],
                                    "evidence_id": evidence_by_seq[seq],
                                    "stratum_a": item["full_a"],
                                    "stratum_b": item["full_b"],
                                    "relation": item["relation"],
                                }
                                for seq, item in sorted(by_seq.items())
                            ],
                        }
                    ),
                ),
            )
            insert_revision_changes(cur, new_revision, diffs, "field_package")

            mappings_payload = [
                {
                    "short_code": short,
                    "stratum_id": short_to_full[short],
                    "site": body.site,
                    "already_registered": short_to_full[short] not in new_strata,
                }
                for short in ordered_short_codes
            ]
            evidence_payload = [
                {
                    "seq": seq,
                    "evidence_id": evidence_by_seq[seq],
                    "stratum_a": by_seq[seq]["full_a"],
                    "stratum_b": by_seq[seq]["full_b"],
                    "relation": by_seq[seq]["relation"],
                }
                for seq in sorted(by_seq)
            ]
            response = {
                "site": body.site,
                "terminal_id": body.terminal_id,
                "package_no": body.package_no,
                "revision": new_revision,
                "mappings": mappings_payload,
                "evidence": evidence_payload,
                "applied": len(by_seq),
                "relation_changes": len(diffs),
                "replayed": False,
            }

            # ---- 包台账（重试据此原样返回；唯一约束 + 咨询锁保证不重复）----
            cur.execute(
                "INSERT INTO field_packages "
                "(terminal_id, package_no, site, content_hash, expected_revision, applied_revision, "
                " mappings, evidence, request, stored_response) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    body.terminal_id,
                    body.package_no,
                    body.site,
                    content_hash,
                    body.expected_revision,
                    new_revision,
                    Json(mappings_payload),
                    Json(evidence_payload),
                    Json(content),
                    Json(response),
                ),
            )
            return response


# ------------------------------------------------------------------ 查询
def _load_active(cur) -> List[Assertion]:
    cur.execute(
        "SELECT evidence_id, stratum_a, stratum_b, relation "
        "FROM assertions WHERE status = 'active' ORDER BY evidence_id"
    )
    return [Assertion(evidence_id=r[0], a=r[1], b=r[2], relation=r[3]) for r in cur.fetchall()]


def list_field_packages() -> List[Dict[str, Any]]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT terminal_id, package_no, site, content_hash, expected_revision, applied_revision, created_at "
                "FROM field_packages ORDER BY terminal_id, package_no"
            )
            return [
                {
                    "terminal_id": r[0],
                    "package_no": r[1],
                    "site": r[2],
                    "content_hash": r[3],
                    "expected_revision": r[4],
                    "revision": r[5],
                    "created_at": r[6].isoformat(),
                }
                for r in cur.fetchall()
            ]


def get_field_package(terminal_id: str, package_no: str) -> Dict[str, Any]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT site, content_hash, expected_revision, applied_revision, mappings, evidence, "
                "stored_response, created_at "
                "FROM field_packages WHERE terminal_id = %s AND package_no = %s",
                (terminal_id, package_no),
            )
            row = cur.fetchone()
            if row is None:
                raise ApiError(
                    404,
                    "FIELD_PACKAGE_NOT_FOUND",
                    f"野外包不存在: 终端 {terminal_id} 包号 {package_no}",
                    {"terminal_id": terminal_id, "package_no": package_no},
                )
            return {
                "terminal_id": terminal_id,
                "package_no": package_no,
                "site": row[0],
                "content_hash": row[1],
                "expected_revision": row[2],
                "revision": row[3],
                "mappings": row[4],
                "evidence": row[5],
                "response": row[6],
                "created_at": row[7].isoformat(),
            }
