#!/usr/bin/env bash
# 考古地层关系核验服务 —— 专题成果包封存端到端示例
# 前提：服务已启动（docker compose up --build），监听 http://localhost:8000
#
# 演示：
#   1) 登记地层并提交断言，创建不可变定版；
#   2) 封存专题成果包（路径途经未入选地层，随证据保留、不扩展关系）；
#   3) 乱序重复封存幂等（同一份包）；未定关系不附路径；
#   4) 正式撤回、新定版之后，已封存成果包内容不变；
#   5) 各类输入拒绝（空 / 单个 / 重复 / 未登记 / 定版不存在），不留半成品。
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pp() { python3 -m json.tool 2>/dev/null || cat; }

echo "== 0. 健康检查 =="
curl -s "$BASE/health" | pp

echo; echo "== 1. 登记地层 =="
for s in L1 L2 L3 L4 L5 L6; do
  curl -s -X POST "$BASE/strata" -H 'Content-Type: application/json' \
    -d "{\"stratum_id\": \"$s\", \"site\": \"甲遗址\"}" >/dev/null || true
done
echo "已登记（若此前已存在则返回 STRATUM_EXISTS，不影响后续演示）"

echo; echo "== 2. 批次 1：L1~L2 同期，L2<L3，L3<L4，L4~L5 同期，L3<L6 =="
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 0,
  "operations": [
    {"op": "assert", "evidence_id": "EV-001", "stratum_a": "L1", "stratum_b": "L2", "relation": "same_period"},
    {"op": "assert", "evidence_id": "EV-002", "stratum_a": "L2", "stratum_b": "L3", "relation": "earlier_than"},
    {"op": "assert", "evidence_id": "EV-003", "stratum_a": "L3", "stratum_b": "L4", "relation": "earlier_than"},
    {"op": "assert", "evidence_id": "EV-004", "stratum_a": "L4", "stratum_b": "L5", "relation": "same_period"},
    {"op": "assert", "evidence_id": "EV-005", "stratum_a": "L3", "stratum_b": "L6", "relation": "earlier_than"}
  ]}' | pp || true

echo; echo "== 3. 定版 =="
curl -s -X POST "$BASE/snapshots" -H 'Content-Type: application/json' \
  -d '{"label": "二期定版"}' | pp

echo; echo "== 4. 封存专题成果包：定版 1，选 L1/L5/L6（路径途经未入选的 L2/L3/L4） =="
curl -s -X POST "$BASE/packages" -H 'Content-Type: application/json' \
  -d '{"version": 1, "strata": ["L1", "L5", "L6"]}' | pp

echo; echo "== 5. 乱序重复封存（L6/L1/L5）：返回同一份，HTTP 200，already_exists=true =="
curl -s -i -X POST "$BASE/packages" -H 'Content-Type: application/json' \
  -d '{"version": 1, "strata": ["L6", "L1", "L5"]}' | head -1
curl -s -X POST "$BASE/packages" -H 'Content-Type: application/json' \
  -d '{"version": 1, "strata": ["L6", "L1", "L5"]}' | pp

echo; echo "== 6. 成果包列表 / 详情 =="
curl -s "$BASE/packages" | pp
curl -s "$BASE/packages/1" | pp

echo; echo "== 7. 不可变性：撤回 EV-002/EV-003 并另建定版 2 =="
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 1,
  "operations": [
    {"op": "retract", "evidence_id": "EV-002"},
    {"op": "retract", "evidence_id": "EV-003"}
  ]}' | pp || true
curl -s -X POST "$BASE/snapshots" -H 'Content-Type: application/json' \
  -d '{"label": "撤回后定版"}' | pp || true

echo; echo "== 8. 再查成果包 1：结论 / 路径 / content_hash 与封存时完全一致 =="
curl -s "$BASE/packages/1" | pp

echo; echo "== 9. 拒绝分支（不留半成品） =="
for body in \
  '{"version":1,"strata":[]}' \
  '{"version":1,"strata":["L1"]}' \
  '{"version":1,"strata":["L1","L2","L1"]}' \
  '{"version":1,"strata":["L1","LX"]}' \
  '{"version":99,"strata":["L1","L2"]}'; do
  echo "请求: $body"
  curl -s -X POST "$BASE/packages" -H 'Content-Type: application/json' -d "$body" | pp
done

echo; echo "全部示例执行完毕。"
