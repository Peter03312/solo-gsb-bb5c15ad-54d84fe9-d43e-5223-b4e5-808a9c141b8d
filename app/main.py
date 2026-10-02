"""考古地层关系核验服务 HTTP API。"""
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from . import arrangement as arrangement_svc, changes, db, field as field_svc, packages as packages_svc, periodization as periodization_svc, preflight as preflight_svc, projections, resilience, service
from .derivation import RELATION_LABELS
from .errors import ApiError
from .models import ArrangementIn, BatchIn, FieldPackageIn, PackageIn, PreflightIn, ProjectionBatchIn, ProjectionIn, ResilienceIn, SnapshotIn, StratumIn


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_pool()
    yield


app = FastAPI(
    title="考古地层关系核验服务",
    version="1.8.0",
    description=(
        "登记遗址地层与带唯一证据编号的“同一期 / 早于”断言；"
        "同一期可传递，早于沿同一期与早于路径推导；"
        "批次携带修订号、按整批最终关系核验，矛盾整批拒绝；"
        "支持关系查询（含支撑证据路径）与不可变定版快照；"
        "支持野外终端离线短号记录的整包汇入（/field-packages）："
        "短号对照随包建立地层与跨包不可改绑的终端绑定，证据编号由终端/包号/序号稳定确定，"
        "整包最终关系核验、原子递增一次正式修订，同终端同包号内容重试幂等返回原结果；"
        "支持只读分期骨架视图（/periodization）：同一期等价组与组间传递约简后的直接早于边；"
        "支持只读分期试排（/periodization/arrangement 与 /projections/{id}/periodization/arrangement）："
        "在总期数（1～64）与地层期位锚点约束下，返回每个同一期等价组在所有可行排布中的最早/最晚期位，"
        "不可行时区分同组锚点矛盾、先后链与锚点不相容、先后链与期数不相容并附证据路径；"
        "支持只读证据撤回承受力分析（/resilience/retraction-analysis）："
        "在同一正式视图中求让指定两层关系变为未定所需撤回的最少生效证据"
        "（受保护证据不可撤回；替代路径仍成立不判失效；同一期双向连通按一条证据计数）；"
        "支持推演（沙盘）：固定基线修订号与断言，推演内独立批次核验，"
        "并入时把相对基线的净变更应用于最新正式状态并重新核验；"
        "支持按正式修订游标分页追踪关系结论及最优支撑路径的变化（/revision-changes）；"
        "支持把定版中的一组地层封存为不可变、可转交复核的专题成果包（/packages）。"
    ),
    lifespan=lifespan,
)


@app.exception_handler(ApiError)
async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
    )


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


@app.get("/", tags=["meta"])
def index():
    return {
        "service": "考古地层关系核验服务",
        "docs": "/docs",
        "relations": RELATION_LABELS,
    }


# ------------------------------------------------------------------ 地层
@app.post("/strata", status_code=201, tags=["strata"])
def create_stratum(body: StratumIn):
    """登记遗址地层。"""
    with db.db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM strata WHERE stratum_id = %s", (body.stratum_id,))
            if cur.fetchone() is not None:
                raise ApiError(409, "STRATUM_EXISTS", f"地层已登记: {body.stratum_id}", {"stratum_id": body.stratum_id})
            cur.execute(
                "INSERT INTO strata (stratum_id, site) VALUES (%s, %s)",
                (body.stratum_id, body.site),
            )
    return {"stratum_id": body.stratum_id, "site": body.site}


@app.get("/strata", tags=["strata"])
def list_strata():
    with db.db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT stratum_id, site, created_at FROM strata ORDER BY stratum_id")
            return [
                {"stratum_id": r[0], "site": r[1], "created_at": r[2].isoformat()}
                for r in cur.fetchall()
            ]


# ------------------------------------------------------------------ 断言批次
@app.post("/batches", tags=["batches"])
def post_batch(batch: BatchIn):
    """提交一批断言新增/撤回。整批核验，成功则原子递增修订号。"""
    return service.apply_batch(batch)


@app.post("/batches/preflight", tags=["batches"])
def post_batch_preflight(batch: PreflightIn):
    """提交前只读预检（不生成修订、变化记录或断言，原状态不变）。

    沿用正式批次的 expected_revision / operations 语义先处理新增与撤回，
    再在 extra_retraction_budget（0～3）限定的生效旧证据范围内，寻找不含本批
    新增证据、也不重复本批撤回的最少额外撤回集合（并列按证据编号序列字典序最小）；
    修订号过期、证据编号占用、撤回目标无效等沿用正式批次的拒绝语义。

    返回 resolution：
    - `already_consistent`：原批次本可通过，额外撤回集合为空，passed=true；
    - `extra_retractions_found`：给出最少额外撤回集合及应用后是否通过核验；
    - `unfixable_new_assertions`：本批保留的新断言自身矛盾，无法靠撤回旧证据修复；
    - `budget_insufficient`：预算内无解；后两种均附当前仍存在的冲突证据路径。
    """
    return preflight_svc.preflight_batch(batch)


@app.get("/revision", tags=["batches"])
def get_revision():
    with db.db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT revision FROM revision_counter WHERE id = TRUE")
            return {"revision": cur.fetchone()[0]}


@app.get("/assertions", tags=["batches"])
def list_assertions(status: Optional[str] = Query(default=None, pattern="^(active|retracted)$")):
    with db.db_conn() as conn:
        with conn.cursor() as cur:
            sql = (
                "SELECT evidence_id, stratum_a, stratum_b, relation, status, added_revision, retracted_revision "
                "FROM assertions"
            )
            params = ()
            if status:
                sql += " WHERE status = %s"
                params = (status,)
            sql += " ORDER BY evidence_id"
            cur.execute(sql, params)
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


# ------------------------------------------------------------------ 野外终端离线整包汇入
@app.post("/field-packages", tags=["field-packages"])
def post_field_package(body: FieldPackageIn):
    """野外终端离线记录整包汇入正式库（整批核验、原子提交）。

    - 请求带采集站点、终端编号、包号、预期正式修订号、短号→全库地层编号对照
      及带包内唯一序号的同一期 / 早于断言；断言端点只能引用本包短号
      （400 UNKNOWN_SHORT_CODE / DUPLICATE_SHORT_CODE / DUPLICATE_SEQUENCE）；
    - 对照目标已登记时站点须一致（409 SITE_CONFLICT），未登记随包建立；
      同一终端同一短号跨包不可改绑（409 SHORT_CODE_REBOUND）；
    - 证据编号由 (终端, 包号, 序号) 稳定确定且不得占用已有编号
      （409 FIELD_EVIDENCE_TAKEN）；
    - 整包沿用正式批次的最终关系核验，冲突 409 FIELD_CONFLICT 附证据路径；
      任一错误都不落地层、绑定或断言；
    - 成功只递增一次正式修订，并与变化记录同事务提交，返回短号映射、
      证据编号与修订号；
    - 相同终端 + 包号 + 内容重试返回原结果（replayed=true），即使正式修订
      已前进；异内容复用 409 FIELD_PACKAGE_CONTENT_MISMATCH；
      首次汇入的过期修订 409 STALE_REVISION。
    """
    return field_svc.ingest_field_package(body)


@app.get("/field-packages", tags=["field-packages"])
def list_field_packages(
    terminal_id: Optional[str] = Query(default=None, description="终端编号（与 package_no 同时给出时返回该包详情）"),
    package_no: Optional[str] = Query(default=None, description="包号（与 terminal_id 同时给出时返回该包详情）"),
):
    """已成功汇入的野外整包台账。

    - 不带参数：台账摘要列表；
    - 同时给 terminal_id 与 package_no：返回该包详情（终端 / 包号含 '/' 等
      不便放入路径段的字符时用此形式），不存在 404 FIELD_PACKAGE_NOT_FOUND。
    """
    if terminal_id is not None and package_no is not None:
        return field_svc.get_field_package(terminal_id, package_no)
    if terminal_id is not None or package_no is not None:
        raise ApiError(
            400,
            "INVALID_QUERY",
            "terminal_id 与 package_no 须同时提供",
            {"terminal_id": terminal_id, "package_no": package_no},
        )
    return field_svc.list_field_packages()


@app.get("/field-packages/{terminal_id}/{package_no}", tags=["field-packages"])
def get_field_package(terminal_id: str, package_no: str):
    """野外整包汇入详情：短号映射、稳定证据编号、首次成功响应（不存在 404）。

    终端编号 / 包号含 '/' 等路径保留字符时改用
    `GET /field-packages?terminal_id=...&package_no=...`。
    """
    return field_svc.get_field_package(terminal_id, package_no)


# ------------------------------------------------------------------ 关系查询
@app.get("/relationship", tags=["query"])
def get_relationship(a: str = Query(description="地层编号"), b: str = Query(description="地层编号")):
    """查询两地层当前关系：同一期 / 早于 / 晚于 / 未定，附支撑证据路径。"""
    return service.query_relationship(a, b)


@app.post("/resilience/retraction-analysis", tags=["query"])
def post_retraction_analysis(body: ResilienceIn):
    """证据撤回承受力分析（只读，不写库、不递增修订号、不产生变化记录）。

    在 expected_revision 的同一正式视图中，求让两个不同已登记地层的确定关系
    变为未定所需撤回的最少生效证据数（受保护证据 protected_evidence 不可撤回，
    同一期双向连通按一条证据撤回计数），并列取证据编号序列字典序最小的一组，
    返回原关系、原最优证据路径、建议撤回编号与模拟撤回后的关系。

    - 原关系未定：撤回数 0、建议集合为空（status=already_undetermined）；
    - 受保护证据单独仍支撑某条路径（替代证据路径仍成立）：status=unreachable，
      minimum_retraction_count 为 null，附仍成立的证据路径 remaining_evidence_path；
    - 否则 status=retractions_found，给出最少撤回集合，模拟后关系恒为未定。
    - 修订号过期 409 STALE_REVISION、地层未登记 400 INVALID_STRATUM、
      两层相同 400 DUPLICATE_STRATUM、受保护证据不存在或非生效
      400 INVALID_PROTECTED_EVIDENCE。
    """
    return resilience.analyze_retraction_resilience(body)


@app.get("/periodization", tags=["query"])
def get_periodization(
    expected_revision: int = Query(
        ge=0, description="预期的当前正式修订号；与当前不一致返回 409 STALE_REVISION（含当前修订号）"
    ),
):
    """当前全部地层的分期骨架（只读）。

    - 同一期断言连成等价组（含孤立地层，各自成组）：组内编号排序，组按最小编号排序；
    - 组间早于关系图只保留传递约简后的直接边（不存在经其他组到达的路径）；
    - 每条保留边列出该组间全部生效的直接早于证据编号（排序）；平行断言合并为一条边；
    - 返回所用修订号、组、边与被传递关系省略的组间边数，顺序稳定；
    - 修订号过期返回 409 STALE_REVISION（details.current_revision），不输出混合版本；
      无断言时各地层各自成组、边为空。
    """
    return periodization_svc.get_periodization(expected_revision)


@app.post("/periodization/arrangement", tags=["query"])
def post_periodization_arrangement(body: ArrangementIn):
    """当前正式修订视图下的分期试排（只读，不写库、不递增修订号）。

    - 同一期等价组须占同一期，早于关系源组期位严格小于目标组；未锚定组可占任一期；
    - 可行时返回每组所含地层及其在**全部可行排布**中的最早 / 最晚期位
      （`earliest_period` / `latest_period`），而非某一套任意排布；
    - 不可行时 HTTP 200、`feasible=false`，`reasons[]` 区分：
      `anchor_group_conflict`（同组锚点矛盾）、
      `anchor_precedence_conflict`（先后链与锚点不相容）、
      `chain_exceeds_periods`（先后链与期数不相容）；
      每条原因附关联锚点、组链与 `evidence_path`，不返回任何组的期位；
    - 总期数取 1～64；锚点期位越界 400 INVALID_PERIOD_POSITION、锚点地层重复
      400 DUPLICATE_STRATUM、锚点地层不存在 400 INVALID_STRATUM；
    - 修订号过期 409 STALE_REVISION（details.current_revision），全程只读快照。
    """
    return arrangement_svc.arrange_formal(body)


# ------------------------------------------------------------------ 修订变化追踪
@app.get("/revision-changes", tags=["changes"])
def get_revision_changes(
    limit: int = Query(default=100, ge=1, le=1000, description="每页条数（1-1000，默认 100）"),
    cursor: Optional[str] = Query(default=None, description="分页游标；首次请求省略，后续原样回传"),
):
    """按正式修订游标分页追踪地层对关系结论与最优支撑路径的变化。

    - 仅记录成功的正式断言批次与产生修订号的推演并入；
    - 同一对按地层编号固定顺序（stratum_a < stratum_b）只列一次；
    - 关系不变但最优支撑路径改变也列出；变为未定时路径为空数组；
    - 旧侧路径按该修订提交前的当时状态解释（后来撤回的证据仍按生效处理）；
    - 同一轮分页固定正式修订上界：首屏省略 cursor 固定 bound，后续原样回传
      next_cursor，新并发提交不进入本轮窗口（不漏、不重、读不到半批）。
    """
    return changes.list_changes(limit, cursor)


# ------------------------------------------------------------------ 定版
@app.post("/snapshots", status_code=201, tags=["snapshots"])
def post_snapshot(body: SnapshotIn):
    """定版：保存当前修订号下全部关系结论的不可变快照。"""
    return service.create_snapshot(body.label)

@app.get("/snapshots", tags=["snapshots"])
def list_snapshots():
    with db.db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version, revision, label, created_at FROM snapshots ORDER BY version")
            return [
                {"version": r[0], "revision": r[1], "label": r[2], "created_at": r[3].isoformat()}
                for r in cur.fetchall()
            ]


@app.get("/snapshots/{version}", tags=["snapshots"])
def get_snapshot(version: int):
    return service.get_snapshot(version)


@app.get("/snapshots/{version}/relationship", tags=["snapshots"])
def get_snapshot_relationship(version: int, a: str = Query(...), b: str = Query(...)):
    """查询历史版本结论；后续撤回不影响旧版。"""
    return service.query_snapshot_relationship(version, a, b)


# ------------------------------------------------------------------ 专题成果包
@app.post("/packages", tags=["packages"])
def post_package(body: PackageIn):
    """把某定版中的一组地层封存为不可变专题成果包（可转交复核）。

    - 只含所选地层按编号排序的无序两两关系，结论与最优证据路径与该定版查询一致；
    - 确定结论携带路径上按序的原始断言；路径途经未入选地层随证据保留
      （`transit_strata` / 逐步 `in_selection`），不扩展额外关系；未定关系不附路径；
    - 同一定版 + 同一地层集合（顺序无关）只生成一份：重复封存返回同一份
      （`already_exists=true`，HTTP 200），首次封存 HTTP 201；
    - 包内容封存即冻结，撤回、新修订、新定版均不改变。
    """
    payload, created = packages_svc.create_package(body)
    payload["already_exists"] = not created
    return JSONResponse(status_code=201 if created else 200, content=payload)


@app.get("/packages", tags=["packages"])
def list_packages():
    """成果包列表（不含两两关系明细）。"""
    return packages_svc.list_packages()


@app.get("/packages/{package_id}", tags=["packages"])
def get_package(package_id: int):
    """成果包详情：冻结的两两关系、路径断言、途经未入选地层与内容指纹。"""
    return packages_svc.get_package(package_id)


# ------------------------------------------------------------------ 推演（证据修订沙盘）
@app.post("/projections", status_code=201, tags=["projections"])
def create_projection(body: ProjectionIn):
    """创建推演：固定当前正式修订号、已登记地层与生效断言；正式状态不变。"""
    return projections.create_projection(body.label)


@app.get("/projections", tags=["projections"])
def list_projections():
    """推演列表。"""
    return projections.list_projections()


@app.get("/projections/{projection_id}", tags=["projections"])
def get_projection(projection_id: int):
    """推演详情：基线信息、推演内新增/撤回、相对基线的净变更。"""
    return projections.get_projection(projection_id)


@app.post("/projections/{projection_id}/batches", tags=["projections"])
def post_projection_batch(projection_id: int, batch: ProjectionBatchIn):
    """在推演内提交一批断言新增/撤回。

    沿用正式批次语义：携带推演修订号（自 0 起）、整批核验；
    新增编号不得复用正式历史编号，且在本推演内不可复用；
    成功后递增推演修订号，正式状态始终不变。
    """
    return projections.apply_projection_batch(projection_id, batch)


@app.get("/projections/{projection_id}/relationship", tags=["projections"])
def get_projection_relationship(
    projection_id: int, a: str = Query(description="地层编号"), b: str = Query(description="地层编号")
):
    """查询推演视角下两地层的关系及支撑证据路径（路径标注 base/projection 来源）。"""
    return projections.query_projection_relationship(projection_id, a, b)


@app.post("/projections/{projection_id}/periodization/arrangement", tags=["projections"])
def post_projection_periodization_arrangement(projection_id: int, body: ArrangementIn):
    """指定推演冻结视图下的分期试排（只读；已关闭推演 merged/discarded 仍可分析）。

    视图为该推演创建时固定的地层 / 基线断言叠加推演内净变更；`expected_revision`
    为推演自身修订号，过期 409 STALE_REVISION；推演不存在 404 PROJECTION_NOT_FOUND；
    语义、返回结构与正式试排一致（额外回带 projection_id / status / base_revision）；
    不写任何表、不改变推演状态与正式修订。
    """
    return arrangement_svc.arrange_projection(projection_id, body)


@app.post("/projections/{projection_id}/merge", tags=["projections"])
def merge_projection(projection_id: int):
    """并入正式状态：把净变更应用于最新正式状态并重新核验。

    基线目标已撤回 / 编号已被正式占用 / 关系冲突（附证据路径）时返回具体原因，
    正式状态与推演均不变；通过则作为一次正式批次原子提交并关闭推演；
    净变更为空时只关闭推演，不递增正式修订号。
    """
    return projections.merge_projection(projection_id)


@app.delete("/projections/{projection_id}", tags=["projections"])
def discard_projection(projection_id: int):
    """放弃推演（仅 open 可放弃，放弃后不可并入）。"""
    return projections.discard_projection(projection_id)
