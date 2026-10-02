#!/usr/bin/env bash
# 考古地层关系核验服务 —— 端到端调用示例
# 前提：服务已启动（docker compose up --build），监听 http://localhost:8000
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pp() { python3 -m json.tool 2>/dev/null || cat; }

echo "== 0. 健康检查 =="
curl -s "$BASE/health" | pp

echo; echo "== 1. 登记地层 =="
for s in L1 L2 L3 L4 L5; do
  curl -s -X POST "$BASE/strata" -H 'Content-Type: application/json' \
    -d "{\"stratum_id\": \"$s\", \"site\": \"甲遗址\"}" | pp
done

echo; echo "== 2. 批次 1（修订号 0 -> 1）：L1 与 L2 同一期，L2 早于 L3，L3 早于 L4 =="
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 0,
  "operations": [
    {"op": "assert", "evidence_id": "EV-001", "stratum_a": "L1", "stratum_b": "L2", "relation": "same_period"},
    {"op": "assert", "evidence_id": "EV-002", "stratum_a": "L2", "stratum_b": "L3", "relation": "earlier_than"},
    {"op": "assert", "evidence_id": "EV-003", "stratum_a": "L3", "stratum_b": "L4", "relation": "earlier_than"}
  ]}' | pp

echo; echo "== 3. 查询：L1 与 L3（沿同一期+早于推导，L1 早于 L3） =="
curl -s "$BASE/relationship?a=L1&b=L3" | pp

echo; echo "== 4. 查询：L4 与 L1（反向即晚于） =="
curl -s "$BASE/relationship?a=L4&b=L1" | pp

echo; echo "== 5. 查询：L1 与 L5（无证据链，未定） =="
curl -s "$BASE/relationship?a=L1&b=L5" | pp

echo; echo "== 6. 冲突示例：断言 L4 早于 L1 将形成早于闭环，整批拒绝（HTTP 409） =="
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 1,
  "operations": [
    {"op": "assert", "evidence_id": "EV-004", "stratum_a": "L4", "stratum_b": "L1", "relation": "earlier_than"}
  ]}' | pp

echo; echo "== 7. 过期修订号示例（HTTP 409，STALE_REVISION） =="
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 0,
  "operations": [
    {"op": "assert", "evidence_id": "EV-004", "stratum_a": "L4", "stratum_b": "L5", "relation": "earlier_than"}
  ]}' | pp

echo; echo "== 8. 批次 2（修订号 1 -> 2）：L4 早于 L5 =="
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 1,
  "operations": [
    {"op": "assert", "evidence_id": "EV-004", "stratum_a": "L4", "stratum_b": "L5", "relation": "earlier_than"}
  ]}' | pp

echo; echo "== 9. 定版：保存版本快照 =="
curl -s -X POST "$BASE/snapshots" -H 'Content-Type: application/json' -d '{"label": "第一期发掘定版"}' | pp

echo; echo "== 10. 批次 3（修订号 2 -> 3）：撤回 EV-002（L2 早于 L3） =="
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 2,
  "operations": [
    {"op": "retract", "evidence_id": "EV-002"}
  ]}' | pp

echo; echo "== 11. 撤回后查询 L1 与 L3（当前已变为未定） =="
curl -s "$BASE/relationship?a=L1&b=L3" | pp

echo; echo "== 12. 查询版本 1 快照中的 L1 与 L3（旧版结论不变，仍为早于） =="
curl -s "$BASE/snapshots/1/relationship?a=L1&b=L3" | pp

echo; echo "== 13. 错误示例：撤回不存在的断言（HTTP 409） =="
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 3,
  "operations": [
    {"op": "retract", "evidence_id": "EV-999"}
  ]}' | pp

echo; echo "== 14. 错误示例：断言引用未登记地层（HTTP 400） =="
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 3,
  "operations": [
    {"op": "assert", "evidence_id": "EV-005", "stratum_a": "L1", "stratum_b": "LX", "relation": "same_period"}
  ]}' | pp

echo; echo "== 15. 错误示例：重复证据编号（HTTP 409） =="
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 3,
  "operations": [
    {"op": "assert", "evidence_id": "EV-001", "stratum_a": "L1", "stratum_b": "L5", "relation": "same_period"}
  ]}' | pp

echo; echo "全部示例执行完毕。"
