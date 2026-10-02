#!/usr/bin/env bash
# 考古地层关系核验服务 —— 推演（证据修订沙盘）端到端调用示例
# 前提：服务已启动（docker compose up --build），监听 http://localhost:8000
# 说明：本脚本自洽运行（自行登记地层并提交正式批次），不依赖 demo.sh 的数据。
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pp() { python3 -m json.tool 2>/dev/null || cat; }
req() { curl -s -o /tmp/resp.json -w '%{http_code}' "$@"; }
show() { echo "   [HTTP $(req "$@")]"; pp < /tmp/resp.json; }

echo "== 0. 健康检查 =="
curl -s "$BASE/health" | pp

echo; echo "== 1. 登记地层 =="
for s in L1 L2 L3 L4 L5; do
  curl -s -X POST "$BASE/strata" -H 'Content-Type: application/json' \
    -d "{\"stratum_id\": \"$s\", \"site\": \"甲遗址\"}" >/dev/null
done
curl -s "$BASE/strata" | pp

echo; echo "== 2. 正式批次（修订号 0 -> 1）：L1~L2 同一期，L2 早于 L3，L3 早于 L4 =="
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 0,
  "operations": [
    {"op": "assert", "evidence_id": "EV-001", "stratum_a": "L1", "stratum_b": "L2", "relation": "same_period"},
    {"op": "assert", "evidence_id": "EV-002", "stratum_a": "L2", "stratum_b": "L3", "relation": "earlier_than"},
    {"op": "assert", "evidence_id": "EV-003", "stratum_a": "L3", "stratum_b": "L4", "relation": "earlier_than"}
  ]}' | pp

echo; echo "== 3. 创建推演（固定当前修订号 1、已登记地层、生效断言），返回推演 id =="
PID=$(curl -s -X POST "$BASE/projections" -H 'Content-Type: application/json' \
  -d '{"label": "二期证据修订推演"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')
echo "   projection id = $PID"
curl -s "$BASE/projections/$PID" | pp

echo; echo "== 4. 推演内查询 L1 与 L3（与正式一致：早于；path_detail 标注 scope=base） =="
curl -s "$BASE/projections/$PID/relationship?a=L1&b=L3" | pp

echo; echo "== 5. 推演批次（推演修订号 0 -> 1）：撤回 EV-002，新增 EV-P01：L4 早于 L5 =="
curl -s -X POST "$BASE/projections/$PID/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 0,
  "operations": [
    {"op": "retract", "evidence_id": "EV-002"},
    {"op": "assert", "evidence_id": "EV-P01", "stratum_a": "L4", "stratum_b": "L5", "relation": "earlier_than"}
  ]}' | pp

echo; echo "== 6. 推演内关系变化：L1 与 L3 变未定；L3 与 L5 早于（路径 EV-003(base)+EV-P01(projection)） =="
curl -s "$BASE/projections/$PID/relationship?a=L1&b=L3" | pp
curl -s "$BASE/projections/$PID/relationship?a=L3&b=L5" | pp

echo; echo "== 7. 正式状态不受影响：L1 与 L3 仍为早于，正式修订号仍为 1 =="
curl -s "$BASE/relationship?a=L1&b=L3" | pp
curl -s "$BASE/revision" | pp

echo; echo "== 8. 推演详情：新增/撤回记录与相对基线的净变更 =="
curl -s "$BASE/projections/$PID" | pp

echo; echo "== 9. 错误示例：推演新增复用正式历史编号 EV-001（HTTP 409 DUPLICATE_EVIDENCE，整批拒绝） =="
show -X POST "$BASE/projections/$PID/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 1,
  "operations": [
    {"op": "assert", "evidence_id": "EV-001", "stratum_a": "L1", "stratum_b": "L5", "relation": "same_period"}
  ]}'

echo; echo "== 10. 错误示例：推演内编号复用 EV-P01（含已撤回语义，HTTP 409） =="
show -X POST "$BASE/projections/$PID/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 1,
  "operations": [
    {"op": "assert", "evidence_id": "EV-P01", "stratum_a": "L2", "stratum_b": "L5", "relation": "earlier_than"}
  ]}'

echo; echo "== 11. 错误示例：撤回基线中不存在的证据（HTTP 409 ASSERTION_NOT_FOUND） =="
show -X POST "$BASE/projections/$PID/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 1,
  "operations": [{"op": "retract", "evidence_id": "EV-999"}]
}'

echo; echo "== 12. 错误示例：推演修订号过期（HTTP 409 STALE_REVISION） =="
show -X POST "$BASE/projections/$PID/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 0,
  "operations": [
    {"op": "assert", "evidence_id": "EV-P02", "stratum_a": "L2", "stratum_b": "L5", "relation": "earlier_than"}
  ]}'

echo; echo "== 13. 错误示例：推演内形成早于闭环（L5 早于 L3，沿 EV-003/EV-P01 成环，整批拒绝附证据路径） =="
show -X POST "$BASE/projections/$PID/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 1,
  "operations": [
    {"op": "assert", "evidence_id": "EV-P03", "stratum_a": "L5", "stratum_b": "L3", "relation": "earlier_than"}
  ]}'

echo; echo "== 14. 错误示例：引用基线之后才登记的地层（HTTP 400 INVALID_STRATUM） =="
curl -s -X POST "$BASE/strata" -H 'Content-Type: application/json' -d '{"stratum_id": "L6"}' >/dev/null
show -X POST "$BASE/projections/$PID/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 1,
  "operations": [
    {"op": "assert", "evidence_id": "EV-P04", "stratum_a": "L1", "stratum_b": "L6", "relation": "same_period"}
  ]}'

echo; echo "== 15. 并入推演：净变更（撤回 EV-002、新增 EV-P01）应用于最新正式状态并重新核验，修订号 1 -> 2 =="
curl -s -X POST "$BASE/projections/$PID/merge" | pp

echo; echo "== 16. 并入后：推演已关闭；再提交批次 / 再并入均被拒绝（HTTP 409 PROJECTION_CLOSED） =="
show -X POST "$BASE/projections/$PID/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 1,
  "operations": [{"op": "retract", "evidence_id": "EV-003"}]}'
show -X POST "$BASE/projections/$PID/merge"

echo; echo "== 17. 正式状态已更新：L1 与 L3 变未定、L3 与 L5 早于；正式修订号为 2 =="
curl -s "$BASE/relationship?a=L1&b=L3" | pp
curl -s "$BASE/relationship?a=L3&b=L5" | pp
curl -s "$BASE/revision" | pp

echo; echo "== 18. 关闭后的推演仍可查询其冻结视图（L1 与 L3 未定） =="
curl -s "$BASE/projections/$PID/relationship?a=L1&b=L3" | pp

echo; echo "== 19. 并入拒绝场景 A：撤回目标已在正式状态撤回（MERGE_TARGET_RETRACTED，双方不变） =="
P2=$(curl -s -X POST "$BASE/projections" -H 'Content-Type: application/json' \
  -d '{"label": "撤回 EV-003 的推演"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')
echo "   projection id = $P2（基线修订号 2）"
curl -s -X POST "$BASE/projections/$P2/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 0,
  "operations": [{"op": "retract", "evidence_id": "EV-003"}]}' | pp
echo "   -- 正式侧也撤回 EV-003（修订号 2 -> 3）--"
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 2,
  "operations": [{"op": "retract", "evidence_id": "EV-003"}]}' | pp
echo "   -- 并入推演 $P2：应被拒绝 --"
show -X POST "$BASE/projections/$P2/merge"
echo "   -- 正式修订号仍为 3，推演仍为 open --"
curl -s "$BASE/revision" | pp
curl -s "$BASE/projections/$P2" | python3 -m json.tool | grep -E '"status"|"projection_revision"'

echo; echo "== 20. 并入拒绝场景 B：新增编号已被正式占用（MERGE_EVIDENCE_TAKEN，双方不变） =="
P3=$(curl -s -X POST "$BASE/projections" -H 'Content-Type: application/json' \
  -d '{"label": "新增 EV-P02 的推演"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')
curl -s -X POST "$BASE/projections/$P3/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 0,
  "operations": [
    {"op": "assert", "evidence_id": "EV-P02", "stratum_a": "L5", "stratum_b": "L3", "relation": "earlier_than"}
  ]}' | pp
echo "   -- 正式侧先占用编号 EV-P02：L2 早于 L4（修订号 3 -> 4）--"
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 3,
  "operations": [
    {"op": "assert", "evidence_id": "EV-P02", "stratum_a": "L2", "stratum_b": "L4", "relation": "earlier_than"}
  ]}' | pp
echo "   -- 并入推演 $P3：应被拒绝 --"
show -X POST "$BASE/projections/$P3/merge"

echo; echo "== 21. 并入拒绝场景 C：与最新正式状态形成关系冲突（MERGE_CONFLICT，附证据路径，双方不变） =="
P4=$(curl -s -X POST "$BASE/projections" -H 'Content-Type: application/json' \
  -d '{"label": "推演内无环、但并入正式后成环"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')
echo "   projection id = $P4（基线修订号 4；链 L2<L4<L5）"
echo "   -- 推演批次：先撤回基线 EV-P02(L2<L4)，再新增 EV-P10(L4<L2)：推演内无环 --"
curl -s -X POST "$BASE/projections/$P4/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 0,
  "operations": [
    {"op": "retract", "evidence_id": "EV-P02"},
    {"op": "assert", "evidence_id": "EV-P10", "stratum_a": "L4", "stratum_b": "L2", "relation": "earlier_than"}
  ]}' | pp
echo "   -- 正式侧新增 EV-005：L2 早于 L4（修订号 4 -> 5）；推演通过时该断言尚不存在 --"
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 4,
  "operations": [
    {"op": "assert", "evidence_id": "EV-005", "stratum_a": "L2", "stratum_b": "L4", "relation": "earlier_than"}
  ]}' | pp
echo "   -- 并入推演 $P4：净变更为撤回 EV-P02 + 新增 EV-P10(L4<L2)；EV-P10 与正式链 EV-005(L2<L4) 成环，应拒绝并附证据路径 --"
show -X POST "$BASE/projections/$P4/merge"
echo "   -- 正式修订号仍为 5，推演仍为 open --"
curl -s "$BASE/revision" | pp
curl -s "$BASE/projections/$P4" | python3 -m json.tool | grep -E '"status"|"projection_revision"'

echo; echo "== 22. 放弃被拒绝的推演 $P2 / $P3 / $P4（DELETE，仅 open 可放弃） =="
for p in "$P2" "$P3" "$P4"; do
  show -X DELETE "$BASE/projections/$p"
done
echo "   -- 重复放弃已关闭推演：HTTP 409 PROJECTION_CLOSED --"
show -X DELETE "$BASE/projections/$P2"

echo; echo "== 23. 净变更为空的并入：推演内新增后又撤回，并入只关闭推演、不递增修订号（仍为 5） =="
P5=$(curl -s -X POST "$BASE/projections" -H 'Content-Type: application/json' \
  -d '{"label": "空净变更推演"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])')
curl -s -X POST "$BASE/projections/$P5/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 0,
  "operations": [
    {"op": "assert", "evidence_id": "EV-P20", "stratum_a": "L2", "stratum_b": "L3", "relation": "earlier_than"}
  ]}' | pp
curl -s -X POST "$BASE/projections/$P5/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 1,
  "operations": [{"op": "retract", "evidence_id": "EV-P20"}]}' | pp
curl -s "$BASE/projections/$P5" | python3 -m json.tool | grep -A4 '"net_changes"'
curl -s -X POST "$BASE/projections/$P5/merge" | pp
curl -s "$BASE/revision" | pp

echo; echo "== 24. 推演列表 =="
curl -s "$BASE/projections" | pp

echo; echo "全部推演示例执行完毕。"
