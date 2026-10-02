#!/usr/bin/env bash
# 考古地层关系核验服务 —— 分期试排（只读）端到端示例
# 前提：服务已启动（docker compose up --build），监听 http://localhost:8000
# 覆盖：正式可行（最早/最晚界）、地层期位锚点、三类不可行分类与证据路径、
#       结构拒绝（锚点重复/期位越界/地层不存在/修订号过期）、
#       推演冻结视图试排（含已关闭推演）、全程只读不写修订。
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pp() { python3 -m json.tool 2>/dev/null || cat; }

echo "== 0. 健康检查 =="
curl -s "$BASE/health" | pp

echo; echo "== 1. 登记地层 L1～L5 并提交断言批次（修订号 0 -> 1） =="
echo "  同一期：L1~L2（EV-001）、L4~L5（EV-005）"
echo "  早于： L2<L3（EV-002）、L3<L4（EV-003）  =>  {L1,L2} < {L3} < {L4,L5}"
for s in L1 L2 L3 L4 L5; do
  curl -s -X POST "$BASE/strata" -H 'Content-Type: application/json' \
    -d "{\"stratum_id\": \"$s\", \"site\": \"乙遗址\"}" >/dev/null
done
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 0,
  "operations": [
    {"op": "assert", "evidence_id": "EV-001", "stratum_a": "L1", "stratum_b": "L2", "relation": "same_period"},
    {"op": "assert", "evidence_id": "EV-005", "stratum_a": "L4", "stratum_b": "L5", "relation": "same_period"},
    {"op": "assert", "evidence_id": "EV-002", "stratum_a": "L2", "stratum_b": "L3", "relation": "earlier_than"},
    {"op": "assert", "evidence_id": "EV-003", "stratum_a": "L3", "stratum_b": "L4", "relation": "earlier_than"}
  ]}' | pp

echo; echo "== 2. 正式试排（修订号 1，总期数 4，无锚点，只读） =="
echo "  返回每组在【所有可行排布】中的最早/最晚期位，不是某一套任意排布："
echo "  {L1,L2}∈[1,2]、{L3}∈[2,3]、{L4,L5}∈[3,4]"
curl -s -X POST "$BASE/periodization/arrangement" -H 'Content-Type: application/json' -d '{
  "expected_revision": 1,
  "total_periods": 4
}' | pp

echo; echo "== 3. 带锚点试排：L3 固定第 2 期（总期数 4） =="
echo "  链被拉直：{L1,L2}=1、{L3}=2、{L4,L5}=3，另有一期空隙"
curl -s -X POST "$BASE/periodization/arrangement" -H 'Content-Type: application/json' -d '{
  "expected_revision": 1,
  "total_periods": 4,
  "anchors": [{"stratum": "L3", "period": 2}]
}' | pp

echo; echo "== 4. 不可行①：先后链与期数不相容（3 个严格递增组 > 总期数 2） =="
echo "  feasible=false，reasons[].type=chain_exceeds_periods，附组链与证据路径，不返回任何期位"
curl -s -X POST "$BASE/periodization/arrangement" -H 'Content-Type: application/json' -d '{
  "expected_revision": 1,
  "total_periods": 2
}' | pp

echo; echo "== 5. 不可行②：同组锚点矛盾（L1 锚第 1 期、L2 锚第 3 期，但二者同一期） =="
echo "  reasons[].type=anchor_group_conflict，evidence_path 给出同一期证据链 EV-001"
curl -s -X POST "$BASE/periodization/arrangement" -H 'Content-Type: application/json' -d '{
  "expected_revision": 1,
  "total_periods": 6,
  "anchors": [{"stratum": "L1", "period": 1}, {"stratum": "L2", "period": 3}]
}' | pp

echo; echo "== 6. 不可行③：先后链与锚点不相容（L1 锚第 4 期，后面还压着两组，总期数 4） =="
echo "  reasons[].type=anchor_precedence_conflict，start 为锚点、end 为最后一期边界"
curl -s -X POST "$BASE/periodization/arrangement" -H 'Content-Type: application/json' -d '{
  "expected_revision": 1,
  "total_periods": 4,
  "anchors": [{"stratum": "L1", "period": 4}]
}' | pp

echo; echo "== 7. 结构性拒绝 =="
echo "  7a. 锚点地层重复（HTTP 400 DUPLICATE_STRATUM）"
curl -s -X POST "$BASE/periodization/arrangement" -H 'Content-Type: application/json' -d '{
  "expected_revision": 1, "total_periods": 4,
  "anchors": [{"stratum": "L1", "period": 1}, {"stratum": "L1", "period": 2}]}' | pp
echo; echo "  7b. 锚点期位越界（HTTP 400 INVALID_PERIOD_POSITION，第 5 期 > 总期数 4）"
curl -s -X POST "$BASE/periodization/arrangement" -H 'Content-Type: application/json' -d '{
  "expected_revision": 1, "total_periods": 4,
  "anchors": [{"stratum": "L1", "period": 5}]}' | pp
echo; echo "  7c. 锚点地层不存在（HTTP 400 INVALID_STRATUM）"
curl -s -X POST "$BASE/periodization/arrangement" -H 'Content-Type: application/json' -d '{
  "expected_revision": 1, "total_periods": 4,
  "anchors": [{"stratum": "L99", "period": 1}]}' | pp
echo; echo "  7d. 修订号过期（HTTP 409 STALE_REVISION，回当前修订号）"
curl -s -i -X POST "$BASE/periodization/arrangement" -H 'Content-Type: application/json' -d '{
  "expected_revision": 0, "total_periods": 4}' | head -n 20

echo; echo "== 8. 全程只读：修订号仍为 1 =="
curl -s "$BASE/revision" | pp

echo; echo "== 9. 推演冻结视图试排 =="
echo "  9a. 创建推演（固定基线：修订号 1 的地层与生效断言）"
PID=$(curl -s -X POST "$BASE/projections" -H 'Content-Type: application/json' \
  -d '{"label": "四期试排推演"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')
echo "  推演 id=$PID"
echo "  9b. 推演内撤回 EV-003（L3 早于 L4），链断为 {L1,L2}<{L3} 与 {L4,L5}；推演修订号 0 -> 1"
curl -s -X POST "$BASE/projections/$PID/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 0,
  "operations": [
    {"op": "retract", "evidence_id": "EV-003"}
  ]}' | pp
echo "  9c. 推演冻结视图试排（expected_revision 为【推演修订号 1】，总期数 3）"
echo "      {L1,L2}∈[1,2]、{L3}∈[2,3]、{L4,L5}∈[1,3]（与正式视图结果不同，证明读的是冻结视图）"
curl -s -X POST "$BASE/projections/$PID/periodization/arrangement" -H 'Content-Type: application/json' -d '{
  "expected_revision": 1,
  "total_periods": 3
}' | pp
echo "  9d. 放弃推演后仍可按冻结视图分析（status=discarded），结果不变"
curl -s -X DELETE "$BASE/projections/$PID" | pp
curl -s -X POST "$BASE/projections/$PID/periodization/arrangement" -H 'Content-Type: application/json' -d '{
  "expected_revision": 1,
  "total_periods": 3
}' | pp
echo; echo "  9e. 推演视图不存在（HTTP 404 PROJECTION_NOT_FOUND）"
curl -s -i -X POST "$BASE/projections/999/periodization/arrangement" -H 'Content-Type: application/json' -d '{
  "expected_revision": 0, "total_periods": 3}' | head -n 12
echo; echo "  9f. 正式修订号仍为 1（推演分析不写正式状态）"
curl -s "$BASE/revision" | pp

echo; echo "分期试排示例执行完毕。"
