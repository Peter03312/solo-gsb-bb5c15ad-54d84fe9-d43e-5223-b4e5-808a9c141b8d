"""请求体模型。"""
from typing import Annotated, List, Literal, Optional, Union

from pydantic import BaseModel, Field


class StratumIn(BaseModel):
    stratum_id: str = Field(min_length=1, max_length=128, description="地层编号，全库唯一")
    site: Optional[str] = Field(default=None, max_length=256, description="遗址名称")


class AssertOp(BaseModel):
    """新增断言。relation=earlier_than 表示 stratum_a 早于 stratum_b。"""

    op: Literal["assert"]
    evidence_id: str = Field(min_length=1, max_length=128, description="证据编号，全库唯一（含已撤回）")
    stratum_a: str = Field(min_length=1, max_length=128)
    stratum_b: str = Field(min_length=1, max_length=128)
    relation: Literal["same_period", "earlier_than"]


class RetractOp(BaseModel):
    """撤回断言（按证据编号，仅可撤回生效中的断言）。"""

    op: Literal["retract"]
    evidence_id: str = Field(min_length=1, max_length=128)


Operation = Annotated[Union[AssertOp, RetractOp], Field(discriminator="op")]


class BatchIn(BaseModel):
    """一批断言变更。须携带当前修订号；整批按应用后的最终关系核验，
    任一冲突则整批拒绝、原状态不变；全部通过则原子递增修订号。"""

    expected_revision: int = Field(ge=0, description="当前修订号；不一致则整批拒绝")
    operations: List[Operation] = Field(min_length=1)


class PreflightIn(BaseModel):
    """提交前预检（只读，不落库、不递增修订号）：沿用正式批次的
    expected_revision / operations 语义，并指定允许额外撤回的生效旧证据数上限。

    服务在该修订号对应的同一正式视图中先按原批次语义处理新增与撤回；
    若处理后仍冲突，寻找不含本批新增证据、也不与本批撤回重复的最少额外撤回集合。
    """

    expected_revision: int = Field(ge=0, description="当前修订号；不一致则按 STALE_REVISION 拒绝")
    operations: List[Operation] = Field(min_length=1)
    extra_retraction_budget: int = Field(
        default=0,
        ge=0,
        le=3,
        description="可额外撤回的生效旧证据数上限（0～3，默认 0）",
    )


class SnapshotIn(BaseModel):
    label: Optional[str] = Field(default=None, max_length=256, description="版本备注")


class ProjectionIn(BaseModel):
    """创建推演：固定当前正式修订号、已登记地层与生效断言快照。"""

    label: Optional[str] = Field(default=None, max_length=256, description="推演备注")


class PackageIn(BaseModel):
    """专题成果包封存请求：指定定版编号与一组已登记地层（至少两个、不重复）。

    业务校验（空 / 不足两个 / 重复 / 定版不存在 / 地层未登记）在包服务层显式完成，
    并分别返回明确错误码；校验失败不留任何半成品。
    """

    version: int = Field(ge=1, description="定版编号")
    strata: List[str] = Field(description="已登记地层编号；至少两个且不重复（顺序不影响结果，自动排序去重）")


class ProjectionBatchIn(BaseModel):
    """推演内的一批断言变更。须携带该推演当前修订号（自 0 起，批次成功后递增）；
    语义与正式批次一致：整批按应用后的最终关系核验，任一冲突则整批拒绝、推演状态不变。"""

    expected_revision: int = Field(ge=0, description="该推演当前修订号；不一致则整批拒绝")
    operations: List[Operation] = Field(min_length=1)


class PeriodAnchorIn(BaseModel):
    """分期试排的地层期位锚点：该地层（及其同一期等价组）固定占据某一期。"""

    stratum: str = Field(min_length=1, max_length=128, description="锚点地层编号（须存在于所读视图）")
    period: int = Field(ge=1, le=64, description="锚定期位（1～总期数；越界在服务层按总期数拒绝）")


class ArrangementIn(BaseModel):
    """分期试排请求（只读，不写库、不递增修订号）。

    在指定只读视图（当前正式修订或某推演的冻结视图）内读取地层与生效断言，
    求每个同一期等价组在全部可行排布中的最早/最晚期位，或分类报告不可行原因。
    expected_revision 必须与视图当前修订号一致，否则 STALE_REVISION。
    """

    expected_revision: int = Field(ge=0, description="视图当前修订号；正式为正式修订号，推演为推演修订号")
    total_periods: int = Field(ge=1, le=64, description="总期数（1～64），期位自 1 起")
    anchors: List[PeriodAnchorIn] = Field(
        default_factory=list,
        description="可选地层期位锚点；同一地层不可重复，期位须在 1～总期数内",
    )


class FieldMappingIn(BaseModel):
    """短号到全库地层编号的一条对照。目标地层已登记时站点须与请求站点一致，
    未登记时随包建立；同一终端的同一短号跨包不可改绑。"""

    short_code: str = Field(min_length=1, max_length=128, description="终端本地地层短号（断言端点只能引用本包短号）")
    stratum_id: str = Field(min_length=1, max_length=128, description="全库地层编号（已登记则站点须一致，否则随包建立）")


class FieldAssertionIn(BaseModel):
    """野外断言，端点为**本包短号**；seq 在包内唯一，
    正式证据编号由 (终端编号, 包号, seq) 稳定确定。"""

    seq: int = Field(ge=1, description="包内唯一序号（同一终端同一包号重试时保持不变）")
    stratum_a: str = Field(min_length=1, max_length=128, description="端点短号 A（须出现在本包 mappings 中）")
    stratum_b: str = Field(min_length=1, max_length=128, description="端点短号 B（须出现在本包 mappings 中）")
    relation: Literal["same_period", "earlier_than"]


class FieldPackageIn(BaseModel):
    """野外终端离线整包汇入请求。

    断言端点只能引用本包短号；目标地层已登记时站点须一致，未登记随包建立；
    同一终端同一短号跨包不可改绑。整包沿用正式批次的最终关系核验，任一错误
    都不落地层、绑定或断言；成功只递增一次正式修订。
    相同终端 + 包号 + 内容重试幂等返回原结果（即使正式修订已前进）；
    同终端同包号但内容不同拒绝。
    """

    site: str = Field(min_length=1, max_length=256, description="采集站点（与已登记目标的站点须一致）")
    terminal_id: str = Field(min_length=1, max_length=128, description="终端编号")
    package_no: str = Field(min_length=1, max_length=128, description="包号（终端内唯一）")
    expected_revision: int = Field(ge=0, description="预期正式修订号；过期按 STALE_REVISION 拒绝（幂等重试除外）")
    mappings: List[FieldMappingIn] = Field(min_length=1, description="短号到全库地层编号的对照（短号不可重复）")
    assertions: List[FieldAssertionIn] = Field(min_length=1, description="带包内唯一序号的断言（端点只能用本包短号）")


class ResilienceIn(BaseModel):
    """证据撤回承受力分析请求（只读，不写库、不递增修订号、不产生变化记录）。

    在 expected_revision 对应的同一正式视图中，求让两个不同已登记地层的确定
    关系变为未定所需撤回的**最少生效证据数**（只从其余生效证据中选择，
    protected_evidence 中的受保护证据不可撤回），并列时取证据编号序列字典序
    最小的一组；原关系未定时撤回数为 0、集合为空。
    """

    expected_revision: int = Field(ge=0, description="预期的当前正式修订号；过期按 STALE_REVISION 拒绝")
    stratum_a: str = Field(min_length=1, max_length=128, description="地层编号 A（须与 B 不同且均已登记）")
    stratum_b: str = Field(min_length=1, max_length=128, description="地层编号 B（须与 A 不同且均已登记）")
    protected_evidence: List[str] = Field(
        default_factory=list,
        description="受保护生效证据编号集合（可为空；自动去重；须为当前生效证据，否则拒绝）",
    )
