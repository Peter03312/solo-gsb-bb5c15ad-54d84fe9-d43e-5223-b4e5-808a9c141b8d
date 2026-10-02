"""野外终端整包汇入。

野外终端离线期间以**包内短号**记录地层关系，回站后把整包一次性汇入正式库：

- 请求携带采集站点、终端编号、包号、预期正式修订号、短号对照（短号 -> 全库地层
  编号）与带包内唯一序号的同一期 / 早于断言；断言端点只能引用本包对照中的短号；
- 对照目标已登记时站点须一致（登记时站点为空的，与任何包站点都不一致）；
  未登记时随包以包站点建立地层；
- 同一终端的同一短号跨包不可改绑：首次汇入建立 (终端, 短号) -> 地层 绑定，
  后续包的对照须与既有绑定一致；
- 证据编号由终端、包号、包内序号稳定确定（`FT-{终端}-{包号}-{序号}`），
  且不得占用已有证据编号（含已撤回）；
- 整包沿用正式批次的最终关系核验（组内早于 / 早于闭环），冲突返回证据路径；
- 任一错误都不落地层、绑定或断言（单事务整体回滚）；成功只递增一次正式修订号，
  并与关系变化记录同事务提交（变化来源 `field_ingest`）；
- 幂等：相同终端 + 包号 + 内容的重试返回原结果（即使正式修订已前进，不重复落库、
  不重复递增修订号）；相同终端 + 包号但内容不同的复用明确拒绝。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Tuple

from psycopg2.extras import Json

from .changes import insert_revision_changes, relation_diffs
from .db import db_conn
from .derivation import Assertion, Derivation
from .errors import ApiError
from .models import FieldPackageIn


# ------------------------------------------------------------------ 工具
def _canonical(payload: Any) -> str:
    """稳定序列化：排序键、紧凑分隔、不转义中文，供内容指纹使用。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def evidence_id_for(terminal_id: str, package_no: str, seq: int) -> str:
    """证据编号：由终端编号、包号、包内序号稳定确定。"""
    return f"FT-{terminal_id}-{package_no}-{seq}"


def _normalize(body: FieldPackageIn) -> Tuple[List[Dict[str, str]], List[Dict[str, Any]]]:
    """纯输入校验与规范化（不触碰数据库）。

    - 对照内短号重复 -> 400 DUPLICATE_SHORT_CODE；
    - 断言包内序号重复 -> 400 DUPLICATE_SEQUENCE；
    - 断言端点引用本包对照之外的短号 -> 400 UNKNOWN_SHORT_CODE。

    返回按短号 / 序号排序的对照与断言，使内容指纹与落库顺序稳定（与请求内
    条目书写顺序无关）。
    """
    binding: Dict[str, str] = {}
    for entry in body.strata_map:
        if entry.short_code in binding:
            raise ApiError(
                400,
                "DUPLICATE_SHORT_CODE",
                f"短号对照中短号重复: {entry.short_code}",
                {"short_code": entry.short_code},
            )
        binding[entry.short_code] = entry.stratum_id

    seen_seq = set()
    for a in body.assertions:
        if a.seq in seen_seq:
            raise ApiError(
                400,
                "DUPLICATE_SEQUENCE",
                f"包内断言序号重复: {a.seq}",
                {"seq": a.seq},
            )
        seen_seq.add(a.seq)

    used = set()
    for a in body.assertions:
        used.add(a.short_a)
        used.add(a.short_b)
    unknown = sorted(used - binding.keys())
    if unknown:
        raise ApiError(
            400,
            "UNKNOWN_SHORT_CODE",
            f"断言引用了本包对照之外的短号: {unknown[0]}",
            {"unknown_short_codes": unknown},
        )

    strata_map = [{"short_code": sc, "stratum_id": binding[sc]} for sc in sorted(binding)]
    assertions = [
        {"seq": a.seq, "short_a": a.short_a, "short_b": a.short_b, "relation": a.relation}
        for a in sorted(body.assertions, key=lambda x: x.seq)
    ]
    return strata_map, assertions


def _content_hash(body: FieldPackageIn, strata_map: List[Dict[str, str]], assertions: List[Dict[str, Any]]) -> str:
    """包内容指纹：规范化后的完整请求内容（含预期修订号），与条目书写顺序无关。"""
    return hashlib.sha256(
        _canonical(
            {
                "site": body.site,
                "terminal_id": body.terminal_id,
                "package_no": body.package_no,
                "expected_revision": body.expected_revision,
                "strata_map": strata_map,
                "assertions": assertions,
            }
        ).encode("utf-8")
    ).hexdigest()


def _load_active(cur) -> List[Assertion]:
    cur.execute(
        "SELECT evidence_id, stratum_a, stratum_b, relation "
        "FROM assertions WHERE status = 'active' ORDER BY evidence_id"
    )
    return [Assertion(evidence_id=r[0], a=r[1], b=r[2], relation=r[3]) for r in cur.fetchall()]


# ------------------------------------------------------------------ 整包汇入
def ingest_field_package(body: FieldPackageIn) -> Tuple[Dict[str, Any], bool]:
    """整包汇入。返回 (响应内容, 是否首次落库)。

    单事务内依次完成：幂等判定 -> 修订号校验 -> 短号对照（站点一致 / 随包建立 /
    终端绑定）-> 证据编号推导与占用检查 -> 断言落库 -> 整包最终关系核验 ->
    变化记录 -> 修订号递增与审计 -> 包登记。任一步失败整体回滚，
    不落地层、绑定或断言。
    """
    strata_map, assertions = _normalize(body)
    content_hash = _content_hash(body, strata_map, assertions)
    binding = {e["short_code"]: e["stratum_id"] for e in strata_map}

    with db_conn() as conn:
        with conn.cursor() as cur:
            # 锁定修订计数器：与正式批次 / 推演并入互斥，整包汇入串行化
            cur.execute("SELECT revision FROM revision_counter WHERE id = TRUE FOR UPDATE")
            current = cur.fetchone()[0]

            # 幂等：同终端同包号已落库 -> 同内容返回原结果（即使正式修订已前进）；
            # 异内容复用拒绝。只有成功汇入才登记包，失败重试不受包号占用影响。
            cur.execute(
                "SELECT content_hash, result FROM field_ingest_packages "
                "WHERE terminal_id = %s AND package_no = %s",
                (body.terminal_id, body.package_no),
            )
            row = cur.fetchone()
            if row is not None:
                if row[0] == content_hash:
                    result = dict(row[1])
                    result["replayed"] = True
                    return result, False
                raise ApiError(
                    409,
                    "PACKAGE_CONTENT_MISMATCH",
                    f"终端 {body.terminal_id} 的包号 {body.package_no} 已汇入过不同内容，异内容复用拒绝",
                    {"terminal_id": body.terminal_id, "package_no": body.package_no},
                )

            if body.expected_revision != current:
                raise ApiError(
                    409,
                    "STALE_REVISION",
                    f"修订号过期：请求携带 {body.expected_revision}，当前为 {current}",
                    {"current_revision": current},
                )
            new_revision = current + 1

            # 变化追踪旧侧：本包应用前的当时生效断言集
            before_assertions = _load_active(cur)

            # ---- 短号对照：站点一致性 / 随包建立 / 终端绑定 ----
            map_results: List[Dict[str, Any]] = []
            for entry in strata_map:
                short_code, stratum_id = entry["short_code"], entry["stratum_id"]
                cur.execute("SELECT site FROM strata WHERE stratum_id = %s", (stratum_id,))
                srow = cur.fetchone()
                stratum_created = False
                if srow is not None:
                    if srow[0] != body.site:
                        raise ApiError(
                            409,
                            "SITE_CONFLICT",
                            f"对照目标 {stratum_id} 已登记，站点不一致："
                            f"登记站点 {srow[0]!r}，本包站点 {body.site!r}",
                            {
                                "short_code": short_code,
                                "stratum_id": stratum_id,
                                "registered_site": srow[0],
                                "package_site": body.site,
                            },
                        )
                else:
                    # 未登记：随包以包站点建立
                    cur.execute(
                        "INSERT INTO strata (stratum_id, site) VALUES (%s, %s)",
                        (stratum_id, body.site),
                    )
                    stratum_created = True

                cur.execute(
                    "SELECT stratum_id FROM terminal_short_codes "
                    "WHERE terminal_id = %s AND short_code = %s",
                    (body.terminal_id, short_code),
                )
                brow = cur.fetchone()
                binding_created = False
                if brow is not None:
                    if brow[0] != stratum_id:
                        raise ApiError(
                            409,
                            "SHORT_CODE_REBIND",
                            f"终端 {body.terminal_id} 的短号 {short_code} 已绑定地层 {brow[0]}，"
                            f"跨包不可改绑为 {stratum_id}",
                            {
                                "terminal_id": body.terminal_id,
                                "short_code": short_code,
                                "bound_stratum_id": brow[0],
                                "requested_stratum_id": stratum_id,
                            },
                        )
                else:
                    cur.execute(
                        "INSERT INTO terminal_short_codes (terminal_id, short_code, stratum_id) "
                        "VALUES (%s, %s, %s)",
                        (body.terminal_id, short_code, stratum_id),
                    )
                    binding_created = True
                map_results.append(
                    {
                        "short_code": short_code,
                        "stratum_id": stratum_id,
                        "stratum_created": stratum_created,
                        "binding_created": binding_created,
                    }
                )

            # ---- 证据编号：稳定推导，不得占用已有编号（含已撤回）----
            evidence: List[Dict[str, Any]] = []
            for a in assertions:
                eid = evidence_id_for(body.terminal_id, body.package_no, a["seq"])
                cur.execute("SELECT 1 FROM assertions WHERE evidence_id = %s", (eid,))
                if cur.fetchone() is not None:
                    raise ApiError(
                        409,
                        "DUPLICATE_EVIDENCE",
                        f"证据编号已存在（含已撤回）: {eid}",
                        {"evidence_id": eid, "seq": a["seq"]},
                    )
                stratum_a, stratum_b = binding[a["short_a"]], binding[a["short_b"]]
                cur.execute(
                    "INSERT INTO assertions (evidence_id, stratum_a, stratum_b, relation, status, added_revision) "
                    "VALUES (%s, %s, %s, %s, 'active', %s)",
                    (eid, stratum_a, stratum_b, a["relation"], new_revision),
                )
                evidence.append(
                    {
                        "seq": a["seq"],
                        "evidence_id": eid,
                        "stratum_a": stratum_a,
                        "stratum_b": stratum_b,
                        "relation": a["relation"],
                    }
                )

            # ---- 整包应用后的最终关系核验（沿用正式批次口径）----
            after_assertions = _load_active(cur)
            conflicts = Derivation(after_assertions).validate()
            if conflicts:
                raise ApiError(
                    409,
                    "CONFLICT",
                    "整包与现有断言矛盾，整包拒绝，原状态不变",
                    {"conflicts": conflicts},
                )

            # 关系变化追踪：地层清单取随包建立后的全集，与断言落库、修订号递增同事务
            cur.execute("SELECT stratum_id FROM strata ORDER BY stratum_id")
            strata = [r[0] for r in cur.fetchall()]
            diffs = relation_diffs(strata, before_assertions, after_assertions)

            cur.execute("UPDATE revision_counter SET revision = %s WHERE id = TRUE", (new_revision,))
            cur.execute(
                "INSERT INTO revisions (revision, batch) VALUES (%s, %s)",
                (
                    new_revision,
                    Json(
                        {
                            "source": "field_ingest",
                            "terminal_id": body.terminal_id,
                            "package_no": body.package_no,
                            "site": body.site,
                            "operations": [
                                {
                                    "op": "assert",
                                    "evidence_id": e["evidence_id"],
                                    "stratum_a": e["stratum_a"],
                                    "stratum_b": e["stratum_b"],
                                    "relation": e["relation"],
                                }
                                for e in evidence
                            ],
                        }
                    ),
                ),
            )
            insert_revision_changes(cur, new_revision, diffs, "field_ingest")

            result: Dict[str, Any] = {
                "terminal_id": body.terminal_id,
                "package_no": body.package_no,
                "site": body.site,
                "revision": new_revision,
                "applied": len(evidence),
                "relation_changes": len(diffs),
                "strata_map": map_results,
                "evidence": evidence,
            }
            # 包登记：同终端同包号的后续同内容请求幂等返回本结果
            cur.execute(
                "INSERT INTO field_ingest_packages "
                "(terminal_id, package_no, site, content_hash, request, revision, result) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    body.terminal_id,
                    body.package_no,
                    body.site,
                    content_hash,
                    Json(body.model_dump()),
                    new_revision,
                    Json(result),
                ),
            )
            out = dict(result)
            out["replayed"] = False
            return out, True


# ------------------------------------------------------------------ 查询
def list_field_packages() -> List[Dict[str, Any]]:
    """已汇入包列表（不含请求 / 结果明细）。"""
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, terminal_id, package_no, site, revision, created_at "
                "FROM field_ingest_packages ORDER BY id"
            )
            return [
                {
                    "id": r[0],
                    "terminal_id": r[1],
                    "package_no": r[2],
                    "site": r[3],
                    "revision": r[4],
                    "created_at": r[5].isoformat(),
                }
                for r in cur.fetchall()
            ]


def get_field_package(terminal_id: str, package_no: str) -> Dict[str, Any]:
    """已汇入包详情：原始请求、落库结果与内容指纹。"""
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, terminal_id, package_no, site, content_hash, request, revision, result, created_at "
                "FROM field_ingest_packages WHERE terminal_id = %s AND package_no = %s",
                (terminal_id, package_no),
            )
            row = cur.fetchone()
    if row is None:
        raise ApiError(
            404,
            "FIELD_PACKAGE_NOT_FOUND",
            f"汇入包不存在: 终端 {terminal_id} 包号 {package_no}",
            {"terminal_id": terminal_id, "package_no": package_no},
        )
    return {
        "id": row[0],
        "terminal_id": row[1],
        "package_no": row[2],
        "site": row[3],
        "content_hash": row[4],
        "request": row[5],
        "revision": row[6],
        "result": row[7],
        "created_at": row[8].isoformat(),
    }


def list_short_code_bindings(terminal_id: str) -> List[Dict[str, Any]]:
    """某终端的短号绑定清单（跨包稳定，不可改绑）。"""
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT short_code, stratum_id, created_at FROM terminal_short_codes "
                "WHERE terminal_id = %s ORDER BY short_code",
                (terminal_id,),
            )
            return [
                {"terminal_id": terminal_id, "short_code": r[0], "stratum_id": r[1], "created_at": r[2].isoformat()}
                for r in cur.fetchall()
            ]
