#!/usr/bin/env bash
# 考古地层关系核验服务 —— 分期骨架视图端到端示例
# 前提：服务已启动（docker compose up --build），监听 http://localhost:8000
# 覆盖：无断言各自成组、同一期等价组、平行断言合并、传递边省略、
#       孤立地层、过期修订号拒绝、撤回后骨架重算。
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pp() { python3 -m json.tool 2>/dev/null || cat; }

echo "== 0. 健康检查 =="
curl -s "$BASE/health" | pp

echo; echo "== 1. 登记地层 L1～L6 =="
for s in L1 L2 L3 L4 L5 L6; do
  curl -s -X POST "$BASE/strata" -H 'Content-Type: application/json' \
    -d "{\"stratum_id\": \"$s\", \"site\": \"甲遗址\"}" >/dev/null
done
echo "已登记 L1～L6"

echo; echo "== 2. 无断言视图（修订号 0）：各地层各自成组、边为空 =="
curl -s "$BASE/periodization?expected_revision=0" | pp

echo; echo "== 3. 批次 1（修订号 0 -> 1） =="
echo "  同一期：L1~L2（EV-001）、L4~L5（EV-005）"
echo "  早于： L2<L3（EV-002）、L1<L3（EV-020，与 EV-002 平行）"
echo "         L3<L4（EV-003）、L2<L4（EV-010）、L1<L5（EV-011，后两者同属 G1->G3）"
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 0,
  "operations": [
    {"op": "assert", "evidence_id": "EV-001", "stratum_a": "L1", "stratum_b": "L2", "relation": "same_period"},
    {"op": "assert", "evidence_id": "EV-005", "stratum_a": "L4", "stratum_b": "L5", "relation": "same_period"},
    {"op": "assert", "evidence_id": "EV-002", "stratum_a": "L2", "stratum_b": "L3", "relation": "earlier_than"},
    {"op": "assert", "evidence_id": "EV-020", "stratum_a": "L1", "stratum_b": "L3", "relation": "earlier_than"},
    {"op": "assert", "evidence_id": "EV-003", "stratum_a": "L3", "stratum_b": "L4", "relation": "earlier_than"},
    {"op": "assert", "evidence_id": "EV-010", "stratum_a": "L2", "stratum_b": "L4", "relation": "earlier_than"},
    {"op": "assert", "evidence_id": "EV-011", "stratum_a": "L1", "stratum_b": "L5", "relation": "earlier_than"}
  ]}' | pp

echo; echo "== 4. 分期骨架（修订号 1） =="
echo "  组：{L1,L2}、{L3}、{L4,L5}、{L6 孤立}"
echo "  保留边：G1->G2（EV-002/EV-020 平行合并）、G2->G3（EV-003）"
echo "  G1->G3 可经 G2 到达 -> 被传递省略（EV-010/EV-011 只算一条边），omitted_edge_count=1"
curl -s "$BASE/periodization?expected_revision=1" | pp

echo; echo "== 5. 过期修订号（携带 0，当前为 1）：HTTP 409 STALE_REVISION，仅回当前修订号 =="
curl -s -i "$BASE/periodization?expected_revision=0" | head -n 20

echo; echo "== 6. 批次 2（修订号 1 -> 2）：撤回 EV-003（L3 早于 L4） =="
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 1,
  "operations": [
    {"op": "retract", "evidence_id": "EV-003"}
  ]}' | pp

echo; echo "== 7. 撤回后骨架（修订号 2）：G2->G3 消失；G1->G3 不再可经 G2 到达，"
echo "    转为保留直接边（EV-010/EV-011）；omitted_edge_count=0"
curl -s "$BASE/periodization?expected_revision=2" | pp

echo; echo "分期骨架示例执行完毕。"
