"""专题成果包封存。

把某个**不可变定版**（snapshot）中的一组已登记地层封存为可转交复核的专题成果包：

- 只列所选地层按编号排序后的**无序两两关系**（与定版查询同一来源
  `snapshot_conclusions`，结论与最优证据路径严格一致）；
- 每条确定结论携带其最优路径上**按顺序**的原始断言（取自定版冻结的断言副本
  `snapshots.assertions`，不读在线断言表）；
- 路径途经但**未入选**的地层仅随证据保留（`transit_strata` + 逐步 `in_selection`
  标记），不扩展出额外的两两关系；
- 未定关系只给关系结论，不附支撑路径与断言；
- 同一定版与同一地层集合（排序去重后的集合身份）无论输入顺序只生成**一份**
  不可变成果包，重复请求幂等返回已封存包；
- 包内容在封存时刻一次性冻结：正式断言撤回、后续修订、新定版都不会改变包内容。

校验失败（输入空 / 不足两个 / 重复 / 含空编号 / 定版不存在 / 地层未登记）显式
拒绝，事务回滚，不留半成品。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from psycopg2.extras import Json

from .db import db_conn
from .derivation import RELATION_LABELS
from .errors import ApiError
from .models import PackageIn


# ------------------------------------------------------------------ 工具
def _canonical(payload: Any) -> str:
    """稳定序列化：排序键、紧凑分隔、不转义中文，供集合指纹与内容指纹使用。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _strata_key(version: int, strata: Sequence[str]) -> str:
    """入选集合的稳定指纹：与输入顺序无关（strata 已排序去重）。"""
    return hashlib.sha256(_canonical({"version": version, "strata": list(strata)}).encode("utf-8")).hexdigest()


def _validate_strata(raw: Sequence[str]) -> List[str]:
    """输入校验：空、不足两个、含空编号、重复一律明确拒绝。返回排序去重后的列表。"""
    if not raw:
        raise ApiError(400, "EMPTY_STRATA", "地层集合为空：至少需要两个不重复的已登记地层编号", {"strata": []})

    blank = [s for s in raw if not isinstance(s, str) or not s.strip()]
    if blank:
        raise ApiError(
            400,
            "INVALID_STRATUM",
            "地层编号不能为空",
            {"stratum_id": ""},
        )

    if len(raw) < 2:
        raise ApiError(
            400,
            "NOT_ENOUGH_STRATA",
            f"至少需要两个不重复的地层编号，实际收到 {len(raw)} 个",
            {"strata": list(raw), "received": len(raw)},
        )

    seen: Set[str] = set()
    duplicates: List[str] = []
    for s in raw:
        if s in seen and s not in duplicates:
            duplicates.append(s)
        seen.add(s)
    if duplicates:
        raise ApiError(
            400,
            "DUPLICATE_STRATUM",
            f"地层编号重复: {', '.join(sorted(duplicates))}",
            {"stratum_ids": sorted(duplicates)},
        )

    return sorted(seen)


def _load_snapshot(cur, version: int) -> Tuple[int, Optional[str], Dict[str, Dict[str, Any]]]:
    """读取定版及其冻结断言副本。定版不存在明确拒绝。

    返回 (定版修订号, 定版备注, 以证据编号为键的冻结原始断言)。
    冻结断言固定带 status=active（定版保存的就是当时生效断言，后续撤回不改变包）。
    """
    cur.execute("SELECT revision, label, assertions FROM snapshots WHERE version = %s", (version,))
    row = cur.fetchone()
    if row is None:
        raise ApiError(404, "SNAPSHOT_NOT_FOUND", f"定版不存在: {version}", {"version": version})
    revision, label, raw_assertions = row[0], row[1], list(row[2])
    by_id: Dict[str, Dict[str, Any]] = {}
    for a in raw_assertions:
        by_id[a["evidence_id"]] = {
            "evidence_id": a["evidence_id"],
            "stratum_a": a["stratum_a"],
            "stratum_b": a["stratum_b"],
            "relation": a["relation"],
            "status": "active",
        }
    return revision, label, by_id


def _ensure_registered(cur, strata: Sequence[str]) -> None:
    cur.execute("SELECT stratum_id FROM strata WHERE stratum_id = ANY(%s)", (list(strata),))
    registered = {r[0] for r in cur.fetchall()}
    missing = [s for s in strata if s not in registered]
    if missing:
        raise ApiError(
            400,
            "INVALID_STRATUM",
            f"地层未登记: {missing[0]}",
            {"stratum_id": missing[0], "unregistered": missing},
        )


def _build_pairs(
    cur,
    version: int,
    selected: Sequence[str],
    frozen_assertions: Dict[str, Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    """构造入选地层按编号排序的无序两两关系。

    结论与最优路径直接读定版冻结结论表（与 GET /snapshots/{v}/relationship 同源）：
    有结论行 -> 确定关系 + 按序路径断言；无结论行 -> 未定（不附路径与断言）。
    返回 (pairs, 去重冻结证据清单, 途经未入选地层)。
    """
    selected_set = set(selected)
    pairs: List[Dict[str, Any]] = []
    transit: Set[str] = set()
    evidence_index: Dict[str, Dict[str, Any]] = {}

    for i, a in enumerate(selected):
        for b in selected[i + 1 :]:
            cur.execute(
                "SELECT relation, path FROM snapshot_conclusions "
                "WHERE version = %s AND stratum_a = %s AND stratum_b = %s",
                (version, a, b),
            )
            row = cur.fetchone()
            pair: Dict[str, Any] = {
                "stratum_a": a,
                "stratum_b": b,
            }
            if row is None:
                # 未定关系：不附支撑路径和断言
                pair["relation"] = "undetermined"
                pair["relation_label"] = RELATION_LABELS["undetermined"]
            else:
                relation, path = row[0], list(row[1])
                path_detail: List[Dict[str, Any]] = []
                for eid in path:
                    frozen = frozen_assertions.get(eid)
                    if frozen is None:
                        # 理论上不会发生：定版结论路径必由其冻结断言构成
                        continue
                    endpoints_in = frozen["stratum_a"] in selected_set and frozen["stratum_b"] in selected_set
                    step = dict(frozen)
                    # 标记该步断言两端是否均为入选地层；未入选端点仅随证据保留
                    step["in_selection"] = endpoints_in
                    path_detail.append(step)
                    evidence_index.setdefault(eid, frozen)
                    if not endpoints_in:
                        for node in (frozen["stratum_a"], frozen["stratum_b"]):
                            if node not in selected_set:
                                transit.add(node)
                pair["relation"] = relation
                pair["relation_label"] = RELATION_LABELS[relation]
                pair["evidence_path"] = path
                pair["path_detail"] = path_detail
            pairs.append(pair)

    evidence = [evidence_index[eid] for eid in sorted(evidence_index)]
    return pairs, evidence, sorted(transit)


# ------------------------------------------------------------------ 封存
def create_package(body: PackageIn) -> Tuple[Dict[str, Any], bool]:
    """封存专题成果包。返回 (成果包内容, 是否首次创建)。

    同一定版 + 同一地层集合（顺序无关）只封存一份：唯一索引
    (version, strata_key) 上用 ON CONFLICT DO NOTHING 让并发首封在索引层串行——
    仅一个事务写入，其余事务取不到 RETURNING 行，转读已提交的同一份包，返回 200
    幂等结果（不存在 SELECT FOR UPDATE 锁不住“尚不存在的行”的竞态）。全部校验
    通过后才插入，任何失败回滚不留半成品。
    """
    # 纯输入校验先做（空 / 不足两个 / 重复 / 空编号），不触碰数据库
    selected = _validate_strata(body.strata)

    with db_conn() as conn:
        with conn.cursor() as cur:
            revision, label, frozen_assertions = _load_snapshot(cur, body.version)
            _ensure_registered(cur, selected)

            strata_key = _strata_key(body.version, selected)
            pairs, evidence, transit = _build_pairs(cur, body.version, selected, frozen_assertions)
            content_hash = hashlib.sha256(
                _canonical(
                    {
                        "version": body.version,
                        "snapshot_revision": revision,
                        "strata": selected,
                        "pairs": pairs,
                        "evidence": evidence,
                        "transit_strata": transit,
                    }
                ).encode("utf-8")
            ).hexdigest()

            # 唯一索引层串行化：首封写入并取回 id；并发/重复同集合取不到行，
            # 随后读取已提交的同一份包。
            cur.execute(
                "INSERT INTO thematic_packages "
                "(version, revision, label, strata, strata_key, pairs, evidence, transit_strata, content_hash) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (version, strata_key) DO NOTHING "
                "RETURNING id, created_at",
                (
                    body.version,
                    revision,
                    label,
                    Json(selected),
                    strata_key,
                    Json(pairs),
                    Json(evidence),
                    Json(transit),
                    content_hash,
                ),
            )
            inserted = cur.fetchone()

            if inserted is None:
                # 同集合已被（可能在并发事务中）封存：读取那一份冻结内容
                cur.execute(
                    f"SELECT {_SELECT_COLUMNS} FROM thematic_packages "
                    "WHERE version = %s AND strata_key = %s",
                    (body.version, strata_key),
                )
                return _serialize(cur.fetchone()), False

    return (
        _serialize(
            (
                inserted[0],
                body.version,
                revision,
                label,
                selected,
                pairs,
                evidence,
                transit,
                content_hash,
                inserted[1],
            )
        ),
        True,
    )


# ------------------------------------------------------------------ 查询
_SELECT_COLUMNS = (
    "id, version, revision, label, strata, pairs, evidence, transit_strata, content_hash, created_at"
)


def _serialize(row: tuple) -> Dict[str, Any]:
    return {
        "id": row[0],
        "version": row[1],
        "snapshot_revision": row[2],
        "label": row[3],
        "strata": list(row[4]),
        "pairs": row[5],
        "evidence": row[6],
        "transit_strata": list(row[7]),
        "content_hash": row[8],
        "created_at": row[9].isoformat(),
    }


def get_package(package_id: int) -> Dict[str, Any]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_SELECT_COLUMNS} FROM thematic_packages WHERE id = %s", (package_id,))
            row = cur.fetchone()
    if row is None:
        raise ApiError(404, "PACKAGE_NOT_FOUND", f"成果包不存在: {package_id}", {"package_id": package_id})
    return _serialize(row)


def list_packages() -> List[Dict[str, Any]]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, version, revision, label, strata, transit_strata, content_hash, created_at "
                "FROM thematic_packages ORDER BY id"
            )
            rows = cur.fetchall()
    return [
        {
            "id": r[0],
            "version": r[1],
            "snapshot_revision": r[2],
            "label": r[3],
            "strata": list(r[4]),
            "transit_strata": list(r[5]),
            "content_hash": r[6],
            "created_at": r[7].isoformat(),
        }
        for r in rows
    ]
