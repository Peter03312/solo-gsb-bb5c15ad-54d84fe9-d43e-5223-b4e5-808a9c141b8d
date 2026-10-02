"""分期试排（只读）。

考古人员在**不写入任何修订**的前提下，试排当前正式修订或指定推演修订（含已关闭
推演的冻结视图）的分期：同一期等价组须占同一期，早于关系的源组期位严格小于目标
组，未锚定的组可占 1～总期数内的任一期。

- 可行：返回每组所含地层，以及它在**所有可行排布**中的最早/最晚期位
  （ASAP/ALAP 界，而非某一套任意排布）；
- 不可行：HTTP 200、`feasible=false`，区分同组锚点矛盾（anchor_group_conflict）、
  先后链与锚点不相容（anchor_precedence_conflict）、先后链与期数不相容
  （chain_exceeds_periods），给出关联锚点与证据路径，**不返回任何组的期位**。

视图口径：正式与推演各自在 `REPEATABLE READ READ ONLY` 快照事务内读取地层与
生效断言，修订号在事务内只读一次；过期返回 409 STALE_REVISION，不输出混合版本。
结构性拒绝：视图不存在（404 PROJECTION_NOT_FOUND）、锚点地层不存在
（400 INVALID_STRATUM）、锚点地层重复（400 DUPLICATE_STRATUM）、锚点期位越界
（400 INVALID_PERIOD_POSITION）。既有查询与批次语义不变。
"""
from __future__ import annotations

from typing import Any, Dict, List, Sequence

from .db import db_conn
from .derivation import (
    ArrangementFeasible,
    ArrangementInfeasible,
    Assertion,
    ArrangementAnchor,
    trial_arrangement,
)
from .errors import ApiError
from .models import ArrangementIn, PeriodAnchorIn
from .projections import _get_projection, _projection_added, _projection_retractions, effective_from


# ------------------------------------------------------------------ 请求校验
def validate_anchors(
    anchors: Sequence[PeriodAnchorIn], total_periods: int, known_strata
) -> List[ArrangementAnchor]:
    """锚点结构校验：期位越界 / 地层重复先查，再查地层是否存在于视图。"""
    for anc in anchors:
        if anc.period < 1 or anc.period > total_periods:
            raise ApiError(
                400,
                "INVALID_PERIOD_POSITION",
                f"锚点 {anc.stratum} 的期位 {anc.period} 越界：须在 1～{total_periods}（总期数 {total_periods}）",
                {"stratum": anc.stratum, "period": anc.period, "total_periods": total_periods},
            )
    seen: set = set()
    for anc in anchors:
        if anc.stratum in seen:
            raise ApiError(
                400,
                "DUPLICATE_STRATUM",
                f"锚点地层重复: {anc.stratum}（同一地层只能给出一个期位锚点）",
                {"stratum": anc.stratum},
            )
        seen.add(anc.stratum)
    for anc in anchors:
        if anc.stratum not in known_strata:
            raise ApiError(
                400,
                "INVALID_STRATUM",
                f"锚点地层未登记（视图内不存在）: {anc.stratum}",
                {"stratum_id": anc.stratum},
            )
    return [ArrangementAnchor(stratum=anc.stratum, period=anc.period) for anc in anchors]


# ------------------------------------------------------------------ 结果序列化
def _result_payload(result, scope_meta: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "feasible": result.feasible,
        "total_periods": result.total_periods,
        **scope_meta,
    }
    if isinstance(result, ArrangementFeasible):
        payload["groups"] = [
            {
                "group": g.group,
                "min_stratum": g.min_stratum,
                "strata": list(g.strata),
                "earliest_period": g.earliest_period,
                "latest_period": g.latest_period,
                "anchored": g.anchored,
                "anchors": [{"stratum": a.stratum, "period": a.period} for a in g.anchors],
            }
            for g in result.groups
        ]
    elif isinstance(result, ArrangementInfeasible):
        # 不可行：不返回任何组的期位（不返回部分排布），仅给分类原因与证据路径
        payload["reasons"] = list(result.reasons)
    return payload


# ------------------------------------------------------------------ 正式视图
def arrange_formal(body: ArrangementIn) -> Dict[str, Any]:
    """在当前正式修订的只读快照视图中试排分期；不写库、不递增修订号。"""
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY ISOLATION LEVEL REPEATABLE READ")

            cur.execute("SELECT revision FROM revision_counter WHERE id = TRUE")
            current = cur.fetchone()[0]
            if body.expected_revision != current:
                raise ApiError(
                    409,
                    "STALE_REVISION",
                    f"修订号过期：请求携带 {body.expected_revision}，当前正式修订号为 {current}",
                    {"current_revision": current, "scope": "formal"},
                )

            cur.execute("SELECT stratum_id FROM strata ORDER BY stratum_id")
            strata = [r[0] for r in cur.fetchall()]
            cur.execute(
                "SELECT evidence_id, stratum_a, stratum_b, relation "
                "FROM assertions WHERE status = 'active' ORDER BY evidence_id"
            )
            assertions = [
                Assertion(evidence_id=r[0], a=r[1], b=r[2], relation=r[3]) for r in cur.fetchall()
            ]

    anchors = validate_anchors(body.anchors, body.total_periods, set(strata))
    result = trial_arrangement(strata, assertions, body.total_periods, anchors)
    return _result_payload(
        result,
        {
            "scope": "formal",
            "expected_revision": body.expected_revision,
            "current_revision": current,
            "revision": current,
        },
    )


# ------------------------------------------------------------------ 推演视图
def arrange_projection(projection_id: int, body: ArrangementIn) -> Dict[str, Any]:
    """在指定推演的冻结视图（open/merged/discarded 均可）中试排分期；不写任何表。"""
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY ISOLATION LEVEL REPEATABLE READ")

            proj = _get_projection(cur, projection_id)
            if body.expected_revision != proj["projection_revision"]:
                raise ApiError(
                    409,
                    "STALE_REVISION",
                    (
                        f"推演修订号过期：请求携带 {body.expected_revision}，"
                        f"推演 {projection_id} 当前为 {proj['projection_revision']}"
                    ),
                    {
                        "projection_id": projection_id,
                        "current_revision": proj["projection_revision"],
                        "scope": "projection",
                    },
                )

            strata = list(proj["base_strata"])
            added = _projection_added(cur, projection_id)
            retracted = {r["evidence_id"] for r in _projection_retractions(cur, projection_id)}
            effective = effective_from(proj["base_assertions"], retracted, added)
            status = proj["status"]
            base_revision = proj["base_revision"]
            view_revision = proj["projection_revision"]

    anchors = validate_anchors(body.anchors, body.total_periods, set(strata))
    result = trial_arrangement(strata, effective, body.total_periods, anchors)
    return _result_payload(
        result,
        {
            "scope": "projection",
            "projection_id": projection_id,
            "status": status,
            "base_revision": base_revision,
            "expected_revision": body.expected_revision,
            "current_revision": view_revision,
            "revision": view_revision,
        },
    )
