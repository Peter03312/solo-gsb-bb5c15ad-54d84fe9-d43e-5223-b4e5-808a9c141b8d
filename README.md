# 考古地层关系核验服务

基于 **Python (FastAPI) + PostgreSQL** 的考古地层关系核验服务。登记遗址地层与带唯一证据编号的
“同一期 / 早于”断言，自动推导地层间关系，支持批次原子核验、冲突证据路径定位、关系查询与不可变定版快照。

## 语义规则

- **同一期（same_period）**：对称且可传递，构成等价类。
- **早于（earlier_than）**：沿“同一期 + 早于”混合路径推导。
  例如 `L1 ~ L2`（同一期）、`L2 < L3`（早于），则 `L1 早于 L3`。
- **硬约束**（对整批应用后的最终关系核验）：
  1. **组内早于禁止**——同一期等价类内部不得存在早于关系；
  2. **早于闭环禁止**——等价类之间的早于关系不得成环。
- **批次语义**：每批新增/撤回须携带当前修订号；按整批应用后的最终关系核验；
  发生矛盾时**整批拒绝**并返回冲突证据路径，原状态不变；全部通过则在同一事务内落库并**原子递增修订号**。
- **路径选取**：多条证据路径时，取证据数最少者；并列时按证据编号序列字典序最小者。
- **定版（快照）**：保存当前修订号下全部关系结论的不可变快照；后续撤回不影响旧版结论。
- **专题成果包**：把某定版中的一组（≥2、不重复、已登记）地层封存为不可变、可转交
  复核包：只含按编号排序的无序两两关系，结论与最优路径同该定版查询，确定结论携带
  路径按序原始断言，途经未入选地层随证据保留但不扩展关系，未定不附路径；同一定版 +
  同一地层集合（顺序无关）只封存一份，撤回 / 新修订 / 新定版均不改变包内容。
- **修订变化追踪**：对每次成功的正式断言批次及推演并入产生的正式修订，记录提交前后
  关系结论或最优支撑路径发生变化的地层对；同一对按地层编号固定顺序只列一次；
  关系不变但最优路径改变也记录；变为未定时路径为空。记录与批次同事务原子写入，
  失败批次、推演内编辑、无净变更并入不产生记录；已有历史修订在服务启动时幂等回填。
- **推演（沙盘）**：创建时固定当前正式修订号、已登记地层与生效断言；推演内沿用正式批次的
  新增 / 撤回 / 整批核验语义（携带推演自身修订号），正式状态始终不变。提交（并入）时把推演
  **相对基线的净变更**应用于最新正式状态并重新核验：基线目标已撤回 / 编号已被正式占用 /
  关系冲突（附证据路径）时返回具体原因且双方不变；通过则作为一次正式批次原子提交并关闭推演；
  净变更为空时只关闭推演，不递增正式修订号。
- **提交前预检（只读）**：`POST /batches/preflight` 输入沿用正式批次的 `expected_revision`、
  `operations`，并指定可额外撤回的生效旧证据数上限 `extra_retraction_budget`（0～3）。在该
  修订号对应的同一正式视图中先按原批次语义处理新增与撤回，再寻找**不包含本批新增证据、也不
  重复本批撤回**的额外撤回集合：撤回数最少、并列时按证据编号序列字典序最小，返回该集合与
  应用后是否通过关系核验。预检**不生成修订、变化记录或断言**，正式批次请求与返回格式不变。
- **分期试排（只读）**：`POST /periodization/arrangement`（正式）与
  `POST /projections/{id}/periodization/arrangement`（推演冻结视图，含已关闭推演）输入
  视图对应的 `expected_revision`、总期数 `total_periods`（1～64）及可选地层期位锚点
  （`anchors[]`，期位 1～总期数）。同一期等价组须占同一期，早于关系源组期位严格小于目标
  组，未锚定组可占任一期。**可行时返回每组所含地层，以及该组在所有可行排布中的最早 / 最晚
  期位**（`earliest_period`/`latest_period`，ASAP/ALAP 界，不是某一套任意排布）；**不可行
  时 HTTP 仍为 200、`feasible=false`，不返回任何组的期位**，`reasons[]` 区分三类：
  同组锚点矛盾 `anchor_group_conflict`、先后链与锚点不相容 `anchor_precedence_conflict`、
  先后链与期数不相容 `chain_exceeds_periods`，每条附关联锚点、组链与证据路径。分析在
  `REPEATABLE READ READ ONLY` 快照事务内完成，不写库、不递增修订号；锚点地层不存在
  （400 `INVALID_STRATUM`）、锚点地层重复（400 `DUPLICATE_STRATUM`）、锚点期位越界
  （400 `INVALID_PERIOD_POSITION`）、修订号过期（409 `STALE_REVISION`）、推演不存在
  （404 `PROJECTION_NOT_FOUND`）明确拒绝，既有查询与批次语义不变。
- **证据撤回承受力分析（只读）**：`POST /resilience/retraction-analysis` 输入视图对应的
  `expected_revision`、两个**不同的已登记**地层 `stratum_a` / `stratum_b` 及可为空的
  受保护生效证据编号集合 `protected_evidence`（自动去重）。服务在该修订号对应的**同一正式
  视图**中，只从**其余生效证据**（同一期 / 早于证据均可，受保护证据不可撤回）中求让两层
  关系变为未定所需的**最少撤回数**，并列时取**证据编号序列字典序最小**的一组，返回原关系、
  原最优证据路径、建议撤回编号与模拟撤回后的关系：
  - 关系由“同一期（无向）+ 早于（有向）”混合图上的路径支撑，撤回只删边；使关系变未定 ⇔
    切断支撑该关系的**全部**路径（最少撤回数即混合证据图上的 s–t 最小割）；
  - **同一期双向连通按一条证据撤回计数**（无向边割值为 1）；
  - **替代证据路径仍成立时不判失效**：若受保护证据单独仍支撑某条路径，返回
    `status=unreachable`、`minimum_retraction_count=null` 与仍成立的（字典序最小）证据路径
    `remaining_evidence_path`；
  - 原关系未定时返回 `status=already_undetermined`、撤回数 `0`、空集合；成功找到时
    `status=retractions_found`，模拟后关系恒为 `undetermined`；
  - 修订号过期（409 `STALE_REVISION`）、地层未登记（400 `INVALID_STRATUM`）、两层相同
    （400 `DUPLICATE_STRATUM`）、受保护证据不存在或非生效
    （400 `INVALID_PROTECTED_EVIDENCE`，`details` 区分 `not_found` / `not_active`）明确拒绝；
    分析不改断言、修订或变化记录。

## 快速开始

```bash
docker compose up --build
```

- API 地址：**http://localhost:8000**
- 交互式接口文档（Swagger UI）：**http://localhost:8000/docs**
- PostgreSQL：`localhost:5432`（容器内服务名 `db`，数据持久化于命名卷 `pgdata`）

首次启动时应用会自动等待数据库就绪并完成表结构初始化，无需手工迁移。

### 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://strat:strat@db:5432/stratigraphy`（compose 内） | 应用连接串 |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `strat` / `strat` / `stratigraphy` | 数据库账号（compose） |
| `APP_PORT` | `8000` | API 对外端口 |
| `DB_PORT` | `5432` | 数据库对外端口 |

本地开发（不用 Docker）：

```bash
pip install -r requirements.txt
export DATABASE_URL=postgresql://strat:strat@localhost:5432/stratigraphy
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/strata` | 登记地层 `{stratum_id, site?}` |
| GET | `/strata` | 地层列表 |
| POST | `/batches` | 提交一批断言新增/撤回（整批核验，成功原子递增修订号） |
| POST | `/batches/preflight` | 提交前只读预检：求消除冲突所需撤回的最少生效旧证据（预算 0～3），不落库 |
| GET | `/revision` | 当前修订号 |
| GET | `/assertions?status=active` | 断言列表 |
| GET | `/revision-changes?limit=100&cursor=...` | 游标分页追踪各正式修订前后关系/最优路径变化 |
| GET | `/relationship?a=L1&b=L3` | 查询两地层当前关系及支撑证据路径 |
| POST | `/resilience/retraction-analysis` | 只读证据撤回承受力分析：求让指定两层关系变为未定的最少生效证据撤回（含受保护证据集合） |
| GET | `/periodization?expected_revision=1` | 只读分期骨架：同一期等价组 + 组间传递约简后的直接早于边（含孤立地层） |
| POST | `/periodization/arrangement` | 正式视图只读分期试排：总期数 1～64 + 期位锚点，返回每组在所有可行排布中的最早/最晚期位 |
| POST | `/projections/{id}/periodization/arrangement` | 推演冻结视图（含已关闭）只读分期试排，返回结构同正式 |
| POST | `/snapshots` | 定版：保存不可变关系快照 `{label?}` |
| GET | `/snapshots` / `/snapshots/{version}` | 版本列表 / 版本详情 |
| GET | `/snapshots/{version}/relationship?a=L1&b=L3` | 查询历史版本结论 |
| POST | `/projections` | 创建推演（固定当前修订号、地层与生效断言） `{label?}` |
| GET | `/projections` / `/projections/{id}` | 推演列表 / 推演详情（含净变更） |
| POST | `/projections/{id}/batches` | 推演内提交批次（携带推演修订号，语义同正式批次） |
| GET | `/projections/{id}/relationship?a=L1&b=L3` | 查询推演视角下的关系与证据路径 |
| POST | `/projections/{id}/merge` | 并入：净变更应用于最新正式状态并重新核验 |
| DELETE | `/projections/{id}` | 放弃推演（仅 open 可放弃） |
| POST | `/packages` | 封存专题成果包：把定版中的一组地层关系冻结为可转交复核包 `{version, strata[]}` |
| GET | `/packages` / `/packages/{id}` | 成果包列表 / 成果包详情（冻结内容与内容指纹） |

### 批次请求格式

```json
{
  "expected_revision": 0,
  "operations": [
    {"op": "assert", "evidence_id": "EV-001", "stratum_a": "L1", "stratum_b": "L2", "relation": "same_period"},
    {"op": "assert", "evidence_id": "EV-002", "stratum_a": "L2", "stratum_b": "L3", "relation": "earlier_than"},
    {"op": "retract", "evidence_id": "EV-009"}
  ]
}
```

- `relation`：`same_period`（同一期）/ `earlier_than`（`stratum_a` 早于 `stratum_b`）。
- 成功响应：`{"revision": 1, "applied": 3, "relation_changes": 3}`
  （`relation_changes` 为本修订导致关系或最优支撑路径变化的地层对数，可能为 0）。
- 冲突响应（HTTP 409，原状态不变）：

```json
{
  "error": {
    "code": "CONFLICT",
    "message": "批次与现有断言矛盾，整批拒绝，原状态不变",
    "details": {
      "conflicts": [
        {"type": "earlier_cycle", "message": "早于关系形成闭环: ...", "evidence_path": ["EV-002", "EV-003", "EV-004"]}
      ]
    }
  }
}
```

### 提交前预检：`POST /batches/preflight`（只读）

考古人员提交断言批次前，可先知道**最少放弃哪些现有证据**才能消除冲突。预检请求体与正式
批次完全相同，另加预算字段：

```json
{
  "expected_revision": 1,
  "extra_retraction_budget": 2,
  "operations": [
    {"op": "assert", "evidence_id": "EV-100", "stratum_a": "L4", "stratum_b": "L1", "relation": "earlier_than"}
  ]
}
```

- `extra_retraction_budget`：可额外撤回的**生效旧证据**数上限，取 `0`～`3`，默认 `0`。
- 处理顺序：在该修订号对应的**同一正式视图**中先按原批次语义处理本批新增与撤回（含同批
  “新增后又撤回”按最终不保留处理），再在旧证据中求额外撤回集合；
- 额外撤回集合**不包含本批新增证据**、也**不重复本批撤回目标**，只从“处理后仍生效的历史
  证据”中选取；
- 选取规则：撤回数最少（0～预算）；并列时按**证据编号序列字典序最小**（同一期边与早于边
  都可入选——撤回同一期边会拆开等价类，同样可能消除组内早于或闭环）；
- 修订号过期、证据编号占用（含已撤回编号、同批重复）、撤回目标无效（不存在 / 已撤回 /
  同批重复撤回）、引用未登记地层等**全部沿用正式批次的拒绝语义与错误码**。

预检**只读**（在 `REPEATABLE READ READ ONLY` 快照事务内完成），不生成修订、变化记录或
断言，正式批次的既有请求与返回格式均未改变。

`resolution` 取值（HTTP 恒为 200；结构性拒绝仍为 4xx）：

| resolution | 含义 | `extra_retractions` | `passed` |
| --- | --- | --- | --- |
| `already_consistent` | 原批次本就可通过，无需放弃任何旧证据 | `[]` | `true` |
| `extra_retractions_found` | 预算内找到最少撤回集合；`passed` 为应用该集合后是否通过核验 | 排序后的证据编号序列 | `true` |
| `unfixable_new_assertions` | 本批最终保留的新断言自身矛盾（仅凭新断言即构成组内早于 / 早于闭环），无法靠撤回旧证据修复 | `[]` | `false` |
| `budget_insufficient` | 预算内不存在解（预算不足） | `[]`（`extra_retraction_count` 为 `null`） | `false` |

后两种情况都在 `conflicts` 中附上**当前仍存在的冲突证据路径**（处理后视图、未做额外撤回
时的组内早于 / 早于闭环，结构与正式批次 `CONFLICT.details.conflicts` 一致）。

找到方案的响应示例：

```json
{
  "expected_revision": 1,
  "current_revision": 1,
  "extra_retraction_budget": 1,
  "batch_new_evidence": ["EV-100"],
  "batch_retracted_evidence": [],
  "resolution": "extra_retractions_found",
  "passed": true,
  "extra_retractions": ["EV-001"],
  "extra_retraction_count": 1,
  "conflicts": []
}
```

把 `extra_retractions` 作为额外的 `{"op": "retract", ...}` 并入原批次后再提交正式批次，
即可通过核验（示例见 `examples/preflight_demo.sh`）。

### 关系查询响应

```json
{
  "stratum_a": "L1",
  "stratum_b": "L3",
  "relation": "earlier_than",
  "relation_label": "早于",
  "evidence_path": ["EV-001", "EV-002"],
  "path_detail": [
    {"evidence_id": "EV-001", "stratum_a": "L1", "stratum_b": "L2", "relation": "same_period", "status": "active"},
    {"evidence_id": "EV-002", "stratum_a": "L2", "stratum_b": "L3", "relation": "earlier_than", "status": "active"}
  ],
  "revision": 1
}
```

`relation` 取值：`same_period`（同一期）/ `earlier_than`（早于）/ `later_than`（晚于）/ `undetermined`（未定）。

### 证据撤回承受力分析：`POST /resilience/retraction-analysis`（只读）

考古人员核对当前地层关系时，需要知道它对**证据撤回**的承受力：若允许在**同一正式视图**
中撤回若干生效证据，最少撤回多少条才能让指定两层的确定关系**变为未定**，以及并列时应撤回
哪一组。请求须携带预期的当前正式修订号 `expected_revision`、两个不同的已登记地层，以及
可为空的受保护生效证据编号集合 `protected_evidence`（自动去重；这些证据不可撤回）：

```json
{
  "expected_revision": 1,
  "stratum_a": "T1",
  "stratum_b": "T4",
  "protected_evidence": []
}
```

- **候选范围**：只从“其余生效证据”中选择撤回——同一期证据与早于证据均可入选，
  `protected_evidence` 中的证据不可撤回；
- **最少撤回数**：关系由“同一期（无向、可传递）+ 早于（有向）”混合图上的路径支撑，
  使关系变未定 ⇔ 切断支撑它的**全部**路径。最少撤回数即混合证据图上的 s–t **最小割**；
  并列时给出**证据编号序列字典序最小**的一组 `suggested_retractions`；
- **同一期双向连通按一条证据撤回计数**：同一期证据是无向边，割中该证据只计 1 次；
- **替代证据路径仍成立时不判失效**：若受保护证据单独仍支撑某条路径（受保护子图上关系仍
  成立），目标无法失效，`status=unreachable`，`minimum_retraction_count` 为 `null`，
  `remaining_evidence_path` 给出仍成立的（字典序最小）证据路径；
- **原关系未定时**：`status=already_undetermined`，撤回数 `0`、集合为空；
- **模拟结果**：成功找到时 `status=retractions_found`，`simulated_relation` 恒为
  `undetermined`，并附撤回后路径（空）；`original_evidence_path` 为撤回前的最优证据路径，
  `original_path_detail` / `simulated_path_detail` 逐步展开原始断言；
- 反向查询（如原关系为 `later_than`）自动规整方向，结果与正向一致。

```json
{
  "expected_revision": 1,
  "current_revision": 1,
  "revision": 1,
  "stratum_a": "T1",
  "stratum_b": "T4",
  "protected_evidence": [],
  "original_relation": "earlier_than",
  "original_relation_label": "早于",
  "original_evidence_path": ["RV-4"],
  "original_path_detail": [
    {"evidence_id": "RV-4", "stratum_a": "T1", "stratum_b": "T4", "relation": "earlier_than", "status": "active"}
  ],
  "reachable": true,
  "status": "retractions_found",
  "minimum_retraction_count": 2,
  "suggested_retractions": ["RV-1", "RV-4"],
  "simulated_relation": "undetermined",
  "simulated_relation_label": "未定",
  "simulated_evidence_path": [],
  "simulated_path_detail": []
}
```

上例中 `T1 早于 T4` 同时由直达证据 `RV-4` 与混合链 `RV-1（T1~T2）, RV-2, RV-3` 支撑，
两条路径互不共边，须各撤一条（共 2 条）；字典序最小的组合是 `[RV-1, RV-4]`。
受保护使目标不可达时：

```json
{
  "reachable": false,
  "status": "unreachable",
  "minimum_retraction_count": null,
  "suggested_retractions": [],
  "simulated_relation": "earlier_than",
  "simulated_relation_label": "早于",
  "remaining_evidence_path": ["RV-4"],
  "remaining_path_detail": [
    {"evidence_id": "RV-4", "stratum_a": "T1", "stratum_b": "T4", "relation": "earlier_than", "status": "active"}
  ]
}
```

明确拒绝：修订号过期（409 `STALE_REVISION`，`details.current_revision`）、地层未登记
（400 `INVALID_STRATUM`）、两层相同（400 `DUPLICATE_STRATUM`）、受保护证据不存在或非生效
（400 `INVALID_PROTECTED_EVIDENCE`，`details.not_found` / `details.not_active`）。
分析在 `REPEATABLE READ READ ONLY` 快照事务内完成，**不改断言、修订或变化记录**。

```bash
bash examples/resilience_demo.sh
```

### 分期骨架视图：`GET /periodization`（只读）

考古人员需要一次性查看**当前全部地层**在同一正式修订视图下的分期骨架。请求必须
携带**预期的当前正式修订号** `expected_revision`（查询参数，≥0）：

- **同一期（same_period）断言连成等价组**：对称、可传递；未被任何同一期断言连接
  的地层（含完全没有断言时）**各自成组**，全部已登记地层恰好出现一次。
  组内地层编号排序，全部组按**组内最小编号**排序，组序号 `group` 从 1 起；
- **组间早于关系图**：每条生效“早于”断言投影到其两端所在的等价组上，
  先把同一组间的**平行断言合并为一条边**，再做**传递约简**——
  当源组到目标组已存在“经其他组”（路径长度 ≥ 2）的先后关系时，该直接边可由
  传递推出，予以省略；只保留**不存在经其他组到达**的直接先后边；
- 每条保留边给出 `from_group` / `to_group`、双方组最小编号
  （`from_min_stratum` / `to_min_stratum`，方向即时间方向：源组早于目标组），
  以及该组间**全部生效的直接早于证据编号** `evidence`（按编号排序）；
- `omitted_edge_count`：**被传递关系省略的组间边数**（平行断言合并后计数，
  即“直接商图边数 − 保留边数”）；
- 返回顺序稳定：组按最小编号排序，边按 `(源组最小编号, 目标组最小编号)` 排序，
  证据按编号排序。

**修订号与一致性**：视图在 `REPEATABLE READ READ ONLY` 快照事务内读取，
修订号在事务内只读一次：`expected_revision` 与当前一致才返回，响应中的
`expected_revision`、`current_revision`、`revision` 恒相同；过期返回 **HTTP 409
`STALE_REVISION`**（`details.current_revision` 为当前修订号），**不输出任何混合
版本数据**——调用方取当前修订号后重新查询。该接口只读，不写库、不递增修订号、
不产生变化记录；批次、关系查询、定版、推演的既有请求与返回约定均未改变。

```bash
curl -s "$BASE/periodization?expected_revision=1"
```

```json
{
  "expected_revision": 1,
  "current_revision": 1,
  "revision": 1,
  "groups": [
    {"group": 1, "min_stratum": "L1", "strata": ["L1", "L2"]},
    {"group": 2, "min_stratum": "L3", "strata": ["L3"]},
    {"group": 3, "min_stratum": "L4", "strata": ["L4", "L5"]},
    {"group": 4, "min_stratum": "L6", "strata": ["L6"]}
  ],
  "edges": [
    {
      "from_group": 1,
      "to_group": 2,
      "from_min_stratum": "L1",
      "to_min_stratum": "L3",
      "relation": "earlier_than",
      "relation_label": "早于",
      "evidence": ["EV-002", "EV-020"]
    },
    {
      "from_group": 2,
      "to_group": 3,
      "from_min_stratum": "L3",
      "to_min_stratum": "L4",
      "relation": "earlier_than",
      "relation_label": "早于",
      "evidence": ["EV-003"]
    }
  ],
  "omitted_edge_count": 1
}
```

上例中 `G1 早于 G3` 的直接断言（EV-010、EV-011）因 `G1 -> G2 -> G3` 已能推出而
被省略（平行两条只记为 1 条被省略边）。无断言时 `groups` 为各地层单元素组、
`edges` 为 `[]`、`omitted_edge_count` 为 `0`。完整端到端示例（无断言、平行合并、
传递省略、孤立地层、过期拒绝、撤回后重算）：

```bash
bash examples/periodization_demo.sh
```

### 分期试排：`POST /periodization/arrangement`（正式，只读）

考古人员试排**当前正式修订**的分期时，给出总期数与可选地层期位锚点，服务在
**同一只读快照视图**内读取地层与生效断言并求解：

- **输入**（JSON）：
  - `expected_revision`（≥0）：预期的当前正式修订号，过期返回 409 `STALE_REVISION`
    （`details.current_revision` 为当前修订号），不输出混合版本；
  - `total_periods`：总期数，取 **1～64**，期位自 1 起（超出由请求模型拒绝，HTTP 422）；
  - `anchors[]`（可选）：每项 `{stratum, period}`，把该地层（及其同一期等价组）固定在
    第 `period` 期；同一地层不可出现两次（400 `DUPLICATE_STRATUM`），期位须在
    **1～总期数**内（400 `INVALID_PERIOD_POSITION`），地层须已登记
    （400 `INVALID_STRATUM`）。
- **约束**：同一期等价组占同一期；早于关系源组期位**严格小于**目标组；未锚定的组可占
  1～总期数内任一期。等价组口径与 `/periodization` 骨架一致（组按最小编号排序）。
- **可行**（HTTP 200，`feasible=true`）：返回每个组的 `strata` 与
  `earliest_period` / `latest_period`——该组在**所有可行排布**中的最早 / 最晚期位
  （二者相等即期位被唯一确定；锚定组恒相等且 `anchored=true`）。这是界，**不是某一套
  任意排布**：界由 DAG 上的最早 / 最迟调度（ASAP/ALAP）求得，且 ASAP 排布与 ALAP
  排布各自都能扩展为完整可行排布，故这两个界是紧的。
- **不可行**（HTTP **200**，`feasible=false`）：**不返回任何组的期位**（不返回部分排布），
  仅在 `reasons[]` 给出全部可定位原因，每条含组链 `group_chain`、端点 `start`/`end`
  （锚点或首/末期边界）、所需 / 可用期位数与证据路径 `evidence_path`：

  | `type` | 含义 |
  | --- | --- |
  | `anchor_group_conflict` | 同一期等价组被锚点钉到不同期位；附该组全部锚点与组内同一期证据链 |
  | `anchor_precedence_conflict` | 先后链与锚点不相容：链端至少一侧是锚点（如两组被钉反序、锚点后容不下后继链）；`involved_anchors` 给关联锚点 |
  | `chain_exceeds_periods` | 先后链与期数不相容：一条不含锚点端的严格链所需期位数超过总期数 |

  `evidence_path` 沿组链取“同一期 + 早于”混合最优证据序列（证据数最少、并列字典序最小，
  口径与关系查询一致），端点固定为锚点地层；未锚定端取链端组的最小编号地层。

```bash
curl -s -X POST "$BASE/periodization/arrangement" -H 'Content-Type: application/json' -d '{
  "expected_revision": 1,
  "total_periods": 4,
  "anchors": [{"stratum": "L3", "period": 2}]
}'
```

```json
{
  "feasible": true,
  "total_periods": 4,
  "scope": "formal",
  "expected_revision": 1,
  "current_revision": 1,
  "revision": 1,
  "groups": [
    {"group": 1, "min_stratum": "L1", "strata": ["L1", "L2"],
     "earliest_period": 1, "latest_period": 1, "anchored": false, "anchors": []},
    {"group": 2, "min_stratum": "L3", "strata": ["L3"],
     "earliest_period": 2, "latest_period": 2, "anchored": true,
     "anchors": [{"stratum": "L3", "period": 2}]},
    {"group": 3, "min_stratum": "L4", "strata": ["L4", "L5"],
     "earliest_period": 3, "latest_period": 3, "anchored": false, "anchors": []}
  ]
}
```

不可行示例（总期数 2 容不下 3 个严格递增的组）：

```json
{
  "feasible": false,
  "total_periods": 2,
  "scope": "formal",
  "expected_revision": 1,
  "current_revision": 1,
  "revision": 1,
  "reasons": [
    {
      "type": "chain_exceeds_periods",
      "message": "先后链与期数不相容：3 个等价组须严格递增，至少需占 3 期，超过总期数 2",
      "group_chain": [
        {"group": 1, "min_stratum": "L1", "strata": ["L1", "L2"], "period": null},
        {"group": 2, "min_stratum": "L3", "strata": ["L3"], "period": null},
        {"group": 3, "min_stratum": "L4", "strata": ["L4", "L5"], "period": null}
      ],
      "start": {"kind": "boundary", "period": 1, "boundary": "first"},
      "end": {"kind": "boundary", "period": 2, "boundary": "last"},
      "required_periods": 3,
      "available_periods": 2,
      "involved_anchors": [],
      "evidence_path": ["EV-001", "EV-002", "EV-003"]
    }
  ]
}
```

### 推演冻结视图试排：`POST /projections/{id}/periodization/arrangement`（只读）

语义、请求体与返回结构与正式试排一致，差别仅在视图与修订号口径：

- 读取该推演**创建时固定的地层 / 基线断言**叠加推演内净变更（新增生效 + 基线撤回）；
- `expected_revision` 是**推演自身修订号**（自 0 起，随推演批次递增），过期返回
  409 `STALE_REVISION`（`details.projection_id`、`details.current_revision`）；
- **已关闭推演（`merged` / `discarded`）仍按其冻结视图分析**，响应回带 `status`；
  推演不存在返回 404 `PROJECTION_NOT_FOUND`；
- 响应额外含 `scope="projection"`、`projection_id`、`status`、`base_revision`；
  锚点地层以基线地层为准；分析不写任何表，推演状态与正式修订均不变。

```bash
curl -s -X POST "$BASE/projections/1/periodization/arrangement" \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision": 1, "total_periods": 3, "anchors": []}'
```

完整端到端示例（最早/最晚界、锚点拉直、三类不可行与证据路径、结构拒绝、
推演冻结视图、关闭后仍可读、全程只读）：

```bash
bash examples/arrangement_demo.sh
```



| HTTP | code | 含义 |
| --- | --- | --- |
| 400 | `INVALID_STRATUM` | 引用了未登记的地层 |
| 409 | `STALE_REVISION` | 批次携带的修订号过期（响应含当前修订号） |
| 409 | `DUPLICATE_EVIDENCE` | 证据编号重复（含已撤回的编号） |
| 409 | `ASSERTION_NOT_FOUND` | 撤回不存在或已撤回的断言 |
| 409 | `CONFLICT` | 整批核验发现组内早于 / 早于闭环（附冲突证据路径） |
| 409 | `STRATUM_EXISTS` | 地层重复登记 |
| 404 | `SNAPSHOT_NOT_FOUND` | 版本不存在 |
| 404 | `PROJECTION_NOT_FOUND` | 推演不存在 |
| 409 | `PROJECTION_CLOSED` | 推演已关闭（merged/discarded），不可再操作 |
| 409 | `MERGE_TARGET_RETRACTED` | 并入失败：撤回目标证据已在最新正式状态被撤回 |
| 409 | `MERGE_EVIDENCE_TAKEN` | 并入失败：推演新增证据编号已被正式历史占用 |
| 409 | `MERGE_CONFLICT` | 并入核验发现关系冲突（附冲突证据路径），双方不变 |
| 400 | `INVALID_CURSOR` | 变化追踪分页游标无效 |
| 400 | `INVALID_PERIOD_POSITION` | 分期试排：锚点期位越界（不在 1～总期数） |
| 400 | `INVALID_PROTECTED_EVIDENCE` | 撤回承受力分析：受保护证据不存在或非生效（`details` 区分 not_found/not_active） |
| 400 | `EMPTY_STRATA` | 成果包：地层集合为空 |
| 400 | `NOT_ENOUGH_STRATA` | 成果包：少于两个地层编号 |
| 400 | `DUPLICATE_STRATUM` | 成果包：地层编号重复 / 分期试排：锚点地层重复 |
| 404 | `PACKAGE_NOT_FOUND` | 成果包不存在 |

## 正式修订关系变化追踪

考古人员需要按正式修订追踪**关系结论及支撑证据**的变化。服务对每次成功的
**正式断言批次**（`POST /batches`）与**推演并入产生的正式修订**
（`POST /projections/{id}/merge` 且实际递增修订号）记录变化明细。

**记录口径**

- 同一地层对按地层编号固定顺序只列一次：返回中恒有 `stratum_a < stratum_b`；
- 记录条件：提交前后**关系变化**（同一期 / 早于 / 晚于 / 未定之间）**或**
  关系不变但**最优支撑路径改变**（路径选取沿用现有规则：证据数最少、并列按证据
  编号序列字典序最小）；
- 变为未定时新路径为空数组 `[]`；首版从未定变为确定结论时旧路径也为 `[]`；
- 旧侧关系与路径按**该修订提交前的当时状态**推导：后来才被撤回的证据在旧侧仍按
  当时生效解释（无需回放后续撤回）；
- **失败批次不产生记录**（整批拒绝、原状态不变、修订号不增）；
  **推演内编辑不产生正式记录**（只动 `projection_*` 表）；
  **无净变更并入不产生记录**（只关闭推演、不递增正式修订号）；
- 成功修订也可能零变化（如撤回一条从未进入最优路径的平行断言）：修订号照常递增、
  审计与封口照写，但没有变化行。

**原子性与并发**

- 变化记录与断言落库、修订号递增在**同一事务**内提交，读不到“半批结果”；
  每次成功批次对计数器行加排他锁，并发提交串行化，修订号连续、不重不漏。

**游标分页** `GET /revision-changes`

- 参数：`limit`（每页 1–1000，默认 100）、`cursor`（首屏省略，后续原样回传
  上一页的 `next_cursor`，为不透明令牌）；
- **同一轮固定正式修订上界**：首屏把当前修订号固定为 `bound_revision` 并编码进
  游标；翻页期间发生的并发提交不会进入本轮窗口，因此整轮遍历不漏项、不重复；
  本轮读完（`has_more=false`、`next_cursor=null`）后重新发一个不带 `cursor` 的
  首屏请求，即可开启覆盖新修订的下一轮；
- 每页在 `REPEATABLE READ READ ONLY` 快照事务内读取。

响应示例：

```json
{
  "changes": [
    {
      "revision": 1,
      "stratum_a": "L1",
      "stratum_b": "L3",
      "old_relation": "undetermined",
      "new_relation": "earlier_than",
      "old_relation_label": "未定",
      "new_relation_label": "早于",
      "old_evidence_path": [],
      "new_evidence_path": ["EV-001", "EV-002"],
      "source": "formal_batch"
    }
  ],
  "bound_revision": 1,
  "limit": 100,
  "has_more": false,
  "next_cursor": null
}
```

`source`：`formal_batch`（正式断言批次）/ `projection_merge`（推演并入产生的
正式修订）。

**历史修订可追溯**：服务启动时幂等检查 `revisions` 审计，对部署前已存在、尚未
追踪的历史修订按当时断言集（`added_revision` / `retracted_revision` 回放）重算
旧/新关系与最优路径并回填；推演并入的历史修订 `source` 按审计记录还原为
`projection_merge`。再次启动不会重复（按修订封口）。

```bash
# 首屏
curl "$BASE/revision-changes?limit=100"
# 翻页：原样回传 next_cursor
curl "$BASE/revision-changes?limit=100&cursor=eyJiIjo1LCJyIjoxLCJzIjozLCJ2IjoxfQ"

# 完整端到端示例（含失败批次 / 推演并入 / 路径变化 / 分页上界）
bash examples/changes_demo.sh
```

## 推演（证据修订沙盘）

考古人员可在**不改动正式断言**的前提下推演证据修订：

1. **创建即固定基线**：`base_revision`（正式修订号）、`base_strata`（已登记地层）、
   `base_assertions`（生效断言快照）。推演创建之后正式侧新增的地层 / 断言对该推演不可见。
2. **推演内批次语义与正式批次一致**：
   - 请求携带的是**推演修订号**（自 0 起，每次成功批次 +1），过期返回 `STALE_REVISION`；
   - 按整批应用后的推演最终关系核验，组内早于 / 早于闭环整批拒绝，附冲突证据路径；
   - **正式历史已用证据编号不可复用**（含已撤回，查正式 `assertions`），
     **推演新增编号在本推演内不可复用**（含在推演内已撤回）；
   - 可撤回推演自己新增的断言，也可撤回基线断言；撤回同样只置状态、不可重复。
3. **推演查询**：`GET /projections/{id}/relationship` 返回推演视角下的关系与证据路径，
   `path_detail[].scope` 标注每步证据来源：`base`（基线）/ `projection`（推演新增）。
4. **并入（merge）**：把推演相对基线的**净变更**（推演内仍生效的新增 + 被撤回的基线断言；
   “新增后又撤回”不产生净变更）应用到**最新**正式状态并重新核验：
   - 基线撤回目标已在正式状态撤回 → `MERGE_TARGET_RETRACTED`；
   - 新增编号已被正式占用 → `MERGE_EVIDENCE_TAKEN`；
   - 净变更应用后出现组内早于 / 早于闭环 → `MERGE_CONFLICT`（附证据路径）；
   - 任一失败：**正式状态与推演均不变**。
   - 核验通过：作为一次正式批次原子提交（写 `assertions`、`revisions` 审计，
     批次记录带来源 `source=projection_merge`），正式修订号 +1 并关闭推演；
     该修订的关系变化同样可在 `/revision-changes` 追踪（`source=projection_merge`）；
   - 净变更为空：只关闭推演，**不递增正式修订号**。
5. 并入与放弃都会关闭推演；关闭后其冻结视图仍可查询，但不能再提交批次或并入。

### 推演请求示例

创建并查询：

```bash
curl -X POST "$BASE/projections" -H 'Content-Type: application/json' \
  -d '{"label": "二期证据修订推演"}'
# -> {"id": 1, "base_revision": 1, "projection_revision": 0, "status": "open", ...}

curl "$BASE/projections/1/relationship?a=L1&b=L3"
```

推演内批次（`expected_revision` 为推演修订号）：

```bash
curl -X POST "$BASE/projections/1/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 0,
  "operations": [
    {"op": "retract", "evidence_id": "EV-002"},
    {"op": "assert", "evidence_id": "EV-P01", "stratum_a": "L4", "stratum_b": "L5", "relation": "earlier_than"}
  ]}'
# -> {"id": 1, "projection_revision": 1, "applied": 2}（正式状态不变）
```

并入：

```bash
curl -X POST "$BASE/projections/1/merge"
# 成功 -> {"id": 1, "merged": true, "applied": 2, "revision": 2,
#          "revision_incremented": true, "relation_changes": 3,
#          "net_changes": {"added": [...], "retracted": [...], "count": 2}}
# 冲突 -> HTTP 409 MERGE_CONFLICT，details.conflicts[].evidence_path 给出证据路径
```

完整流程（含正式状态不受影响、编号复用拒绝、三类并入失败、空净变更只关闭）见
`examples/projection_demo.sh`：

```bash
bash examples/projection_demo.sh
```

## 专题成果包（定版地层关系封存转交）

考古人员可把**某个定版**中的一组地层关系封存为可转交复核的专题成果包。成果包是
**定版时刻的只读摘录**，独立于正式状态继续演化。

**封存口径**

- 请求给出定版编号 `version` 与至少两个、不重复的**已登记**地层编号 `strata`；
- 包内只列所选地层**按编号排序**后的无序两两关系（每对取编号较小者为
  `stratum_a`，`stratum_a < stratum_b`）；
- 关系结论与最优证据路径与**该定版查询**（`GET /snapshots/{version}/relationship`）
  严格一致：确定关系直接取自定版冻结结论，路径为该定版选取的最优证据序列
  （证据数最少、并列按证据编号字典序最小）；
- 每条确定结论的 `path_detail` 携带路径上**按顺序**的原始断言（证据编号、两端
  地层、关系），原始断言取自**定版冻结的断言副本**（`snapshots.assertions`），
  不读取在线断言表，因此其 `status` 恒为封存时的 `active`；
- 路径**途经但未入选**的地层只随证据保留：包顶层给出 `transit_strata`，每步断言
  用 `in_selection`（该步两端是否都在入选集合内）标注；不会因为途经而扩展出
  额外的两两关系；
- **未定关系**只返回 `relation=undetermined` 与中文标签，**不附** `evidence_path`
  和 `path_detail`；
- `evidence` 是包内所有路径引用到的去重原始断言清单（按证据编号排序），便于脱离
  本服务转交复核；`content_hash` 是包内容（定版、修订、入选集合、pairs、evidence、
  transit_strata）的 sha256 指纹，接收方可据此校验包未被篡改。

**身份与不可变性**

- 同一 `version` + **同一地层集合**无论输入顺序、是否重复输入，都只生成**一份**
  成果包（按排序去重后的集合取指纹 `strata_key`，数据库唯一约束
  `(version, strata_key)` 保证；并发封存也在行锁下串行）；
- 首次封存返回 **HTTP 201**；同集合重复封存幂等返回**同一份**（相同 `id` 与
  `content_hash`），HTTP **200**，响应带 `already_exists=true`；
- 包内容在封存事务内一次性冻结，之后**永不修改、永不删除**：正式断言撤回、后续
  修订、新定版都不会改变已有包（旧包仍展示定版当时的结论与当时生效断言）。

**拒绝语义（校验失败明确拒绝、事务回滚，不留半成品）**

| 情形 | HTTP | code |
| --- | --- | --- |
| `strata` 为空 | 400 | `EMPTY_STRATA` |
| 少于两个地层 | 400 | `NOT_ENOUGH_STRATA` |
| 地层编号重复 | 400 | `DUPLICATE_STRATUM` |
| 含空编号 / 地层未登记 | 400 | `INVALID_STRATUM` |
| 定版不存在 | 404 | `SNAPSHOT_NOT_FOUND` |
| 缺少 `version`/`strata` 字段、类型不符 | 422 | FastAPI 请求体校验 |

### 封存请求与响应

```bash
# 选 L1、L5、L6；L1 早于 L5 的证据链途经未入选的 L2、L3、L4
curl -X POST "$BASE/packages" -H 'Content-Type: application/json' \
  -d '{"version": 1, "strata": ["L1", "L5", "L6"]}'
```

```json
{
  "id": 1,
  "version": 1,
  "snapshot_revision": 1,
  "label": "二期定版",
  "strata": ["L1", "L5", "L6"],
  "pairs": [
    {
      "stratum_a": "L1",
      "stratum_b": "L5",
      "relation": "earlier_than",
      "relation_label": "早于",
      "evidence_path": ["EV-001", "EV-002", "EV-003", "EV-004"],
      "path_detail": [
        {"evidence_id": "EV-001", "stratum_a": "L1", "stratum_b": "L2", "relation": "same_period", "status": "active", "in_selection": false},
        {"evidence_id": "EV-002", "stratum_a": "L2", "stratum_b": "L3", "relation": "earlier_than", "status": "active", "in_selection": false},
        {"evidence_id": "EV-003", "stratum_a": "L3", "stratum_b": "L4", "relation": "earlier_than", "status": "active", "in_selection": false},
        {"evidence_id": "EV-004", "stratum_a": "L4", "stratum_b": "L5", "relation": "same_period", "status": "active", "in_selection": false}
      ]
    },
    {"stratum_a": "L1", "stratum_b": "L6", "relation": "earlier_than", "relation_label": "早于", "...": "..."},
    {"stratum_a": "L5", "stratum_b": "L6", "relation": "undetermined", "relation_label": "未定"}
  ],
  "evidence": [
    {"evidence_id": "EV-001", "stratum_a": "L1", "stratum_b": "L2", "relation": "same_period", "status": "active"}
  ],
  "transit_strata": ["L2", "L3", "L4"],
  "content_hash": "cfa6dd4e0236a56f9b4f18f1fbeeaed6f8913451d2329ac46f007f05d379fd15",
  "created_at": "2026-10-01T01:24:31.192399+00:00",
  "already_exists": false
}
```

- 乱序等价：`{"version":1,"strata":["L6","L1","L5"]}` 与上例是**同一个集合**，
  返回同一 `id`、同一 `content_hash`（HTTP 200，`already_exists=true`）；
- 列表 `GET /packages` 只给摘要（不含 `pairs` 明细）；
  详情 `GET /packages/{id}` 返回完整冻结内容，不存在返回 404 `PACKAGE_NOT_FOUND`。

完整端到端演示（封存、乱序幂等、未定无路径、撤回 + 新定版后内容不变、各类拒绝）：

```bash
bash examples/packages_demo.sh
```

## 调用示例

端到端 curl 演示（覆盖登记、批次、推导查询、冲突拒绝、撤回、定版与旧版不可变）：

```bash
bash examples/demo.sh
```

分期骨架视图演示（无断言各自成组、同一期等价组、平行断言合并、
传递边省略、孤立地层、过期修订号拒绝、撤回后骨架重算）：

```bash
bash examples/periodization_demo.sh
```

分期试排演示（每组最早/最晚期位界、锚点拉直、同组锚点矛盾 /
先后链与锚点 / 先后链与期数三类不可行及证据路径、结构拒绝、
推演冻结视图与关闭后可读、全程只读）：

```bash
bash examples/arrangement_demo.sh
```

提交前预检演示（原批可通过 / 建议最少撤回 / 预算不足附冲突路径 /
新断言自成矛盾不可修复 / 拒绝语义沿用正式批次 / 全程只读）：

```bash
bash examples/preflight_demo.sh
```

证据撤回承受力分析演示（最小割最少撤回 / 字典序最小序列 / 同一期按一条计数 /
替代路径仍成立不可达 / 原关系未定返回零和空集 / 修订、地层、受保护证据无效明确拒绝）：

```bash
bash examples/resilience_demo.sh
```

推演（沙盘）端到端演示（创建固定基线、推演批次、关系查询、并入成功与三类并入失败、
净变更为空只关闭、放弃推演）：

```bash
bash examples/projection_demo.sh
```

正式修订关系变化追踪演示（正式批次 / 路径变化 / 失败批次不产生记录 /
推演内编辑与并入 / 游标分页与同一轮上界）：

```bash
bash examples/changes_demo.sh
```

专题成果包封存演示（定版选层封存、路径途经未入选地层、乱序幂等一份、
未定不附路径、撤回与新定版后不可变、空 / 重复 / 未登记 / 定版不存在明确拒绝）：

```bash
bash examples/packages_demo.sh
```

Python 客户端示例：

```bash
pip install requests
python3 examples/client_demo.py
```

## 项目结构

```
app/
  main.py         # FastAPI 路由与错误处理
  service.py      # 正式批次核验、关系查询、定版快照
  preflight.py    # 提交前只读预检：求消除本批冲突所需撤回的最少生效旧证据（预算 0～3）
  resilience.py   # 证据撤回承受力（只读）：混合证据图最小割求让两层关系变未定的最少撤回（含受保护集合）
  packages.py     # 专题成果包：定版地层关系封存（冻结、集合幂等、不可变、可转交复核）
  projections.py  # 推演（沙盘）：创建固定基线、推演批次、关系查询、并入核验
  periodization.py # 分期骨架视图（只读）：同一期等价组、组间传递约简直接早于边
  arrangement.py   # 分期试排（只读）：总期数+锚点约束下每组在所有可行排布中的最早/最晚期位
  changes.py      # 正式修订变化追踪：前后差异计算、游标分页、启动历史回填
  derivation.py   # 推导引擎：并查集等价类、DAG 校验、最优证据路径、分期骨架、分期试排
  db.py           # 连接池与模式初始化（启动时自动建表、幂等回填）
  models.py       # 请求模型
examples/
  demo.sh           # curl 端到端示例（批次/查询/冲突/撤回/定版）
  periodization_demo.sh # 分期骨架视图示例（等价组/平行合并/传递省略/过期拒绝/撤回重算）
  arrangement_demo.sh # 分期试排示例（最早/最晚界、锚点、三类不可行证据链、推演冻结视图）
  preflight_demo.sh # 提交前只读预检示例（最少撤回/预算不足/新断言自相矛盾/拒绝/只读校验）
  resilience_demo.sh # 撤回承受力分析示例（最小割/字典序最小/受保护不可达/原关系未定/拒绝）
  projection_demo.sh # 推演端到端示例（沙盘批次、并入成功与三类失败、空净变更）
  changes_demo.sh  # 修订变化追踪示例（变化/路径改变/失败批次/并入/游标分页）
  packages_demo.sh # 专题成果包示例（封存/幂等/不可变/拒绝分支）
  client_demo.py    # Python 客户端示例
Dockerfile
docker-compose.yml
requirements.txt
```
