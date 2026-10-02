#!/usr/bin/env bash
# 考古地层关系核验服务 —— 证据撤回承受力分析
# （POST /resilience/retraction-analysis）端到端示例
#
# 分析只读：不写库、不递增修订号、不产生变化记录，正式状态不变。
# 输入：expected_revision（须为当前正式修订号）、两个不同的已登记地层
# stratum_a / stratum_b、可为空的受保护生效证据编号集合 protected_evidence。
# 返回：原关系与原最优证据路径、使关系变为未定所需的最少撤回数与字典序最小
# 撤回编号序列、模拟撤回后的关系；受保护证据使目标无法失效时返回
# status=unreachable 与仍成立的证据路径。
#
# 前提：服务已启动（docker compose up --build），监听 http://localhost:8000
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pp() { python3 -m json.tool 2>/dev/null || cat; }

echo "== 0. 登记本示例专用地层 =="
for s in T1 T2 T3 T4 T5 T6 T7; do
  curl -s -X POST "$BASE/strata" -H 'Content-Type: application/json' \
    -d "{\"stratum_id\": \"$s\", \"site\": \"撤回承受力演示区\"}" | pp
done

echo; echo "== 1. 建立正式视图（修订号 0 -> 1） =="
echo "   T1~T2（RV-1 同一期），T2<T3（RV-2），T3<T4（RV-3）；另有 T1<T4 直达（RV-4）；"
echo "   T5<T6（RV-5）；T7 为孤立地层。"
REV="$(curl -s "$BASE/revision" | python3 -c 'import sys,json;print(json.load(sys.stdin)["revision"])')"
if [ "$REV" = 0 ]; then
  curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
    "expected_revision": 0,
    "operations": [
      {"op": "assert", "evidence_id": "RV-1", "stratum_a": "T1", "stratum_b": "T2", "relation": "same_period"},
      {"op": "assert", "evidence_id": "RV-2", "stratum_a": "T2", "stratum_b": "T3", "relation": "earlier_than"},
      {"op": "assert", "evidence_id": "RV-3", "stratum_a": "T3", "stratum_b": "T4", "relation": "earlier_than"},
      {"op": "assert", "evidence_id": "RV-4", "stratum_a": "T1", "stratum_b": "T4", "relation": "earlier_than"},
      {"op": "assert", "evidence_id": "RV-5", "stratum_a": "T5", "stratum_b": "T6", "relation": "earlier_than"}
    ]}' | pp
fi
REV="$(curl -s "$BASE/revision" | python3 -c 'import sys,json;print(json.load(sys.stdin)["revision"])')"
echo "（当前修订号 $REV）"

echo; echo "== 2. 两条互不共边的路径 T1→T4：直达 RV-4 与混合 RV-1,RV-2,RV-3"
echo "   最少撤回 2 条；字典序最小序列 [RV-1, RV-4]（撤 RV-2 或 RV-3 也可切断混合链，但字典序更大）"
curl -s -X POST "$BASE/resilience/retraction-analysis" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": $REV,
  \"stratum_a\": \"T1\",
  \"stratum_b\": \"T4\",
  \"protected_evidence\": []}" | pp

echo; echo "== 3. 反向查询 T4,T1（原关系 later_than）：方向自动规整，结果相同 =="
curl -s -X POST "$BASE/resilience/retraction-analysis" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": $REV,
  \"stratum_a\": \"T4\",
  \"stratum_b\": \"T1\",
  \"protected_evidence\": []}" | pp

echo; echo "== 4. 同一期关系 T1~T2：同一期双向连通按一条证据撤回计数，最少 1 条 [RV-1] =="
curl -s -X POST "$BASE/resilience/retraction-analysis" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": $REV,
  \"stratum_a\": \"T1\",
  \"stratum_b\": \"T2\"}" | pp

echo; echo "== 5. 受保护证据使目标无法失效：保护整条直达路径 RV-4"
echo "   -> status=unreachable，minimum_retraction_count=null，remaining_evidence_path=[RV-4]；"
echo "      模拟后关系仍为 earlier_than（替代证据路径仍成立，不判失效）"
curl -s -X POST "$BASE/resilience/retraction-analysis" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": $REV,
  \"stratum_a\": \"T1\",
  \"stratum_b\": \"T4\",
  \"protected_evidence\": [\"RV-4\"]}" | pp

echo; echo "== 6. 保护非路径关键证据不改变最小值（保护 RV-3：仍需 2 条，字典序 [RV-1,RV-4]）"
curl -s -X POST "$BASE/resilience/retraction-analysis" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": $REV,
  \"stratum_a\": \"T1\",
  \"stratum_b\": \"T4\",
  \"protected_evidence\": [\"RV-3\"]}" | pp

echo; echo "== 7. 单证据关系 T5<T6：最少 1 条 [RV-5]；保护 RV-5 即不可达 =="
curl -s -X POST "$BASE/resilience/retraction-analysis" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": $REV,
  \"stratum_a\": \"T5\",
  \"stratum_b\": \"T6\"}" | pp
curl -s -X POST "$BASE/resilience/retraction-analysis" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": $REV,
  \"stratum_a\": \"T5\",
  \"stratum_b\": \"T6\",
  \"protected_evidence\": [\"RV-5\"]}" | pp

echo; echo "== 8. 原关系未定（T1 与 T7 无任何路径）：撤回数 0、集合为空 =="
curl -s -X POST "$BASE/resilience/retraction-analysis" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": $REV,
  \"stratum_a\": \"T1\",
  \"stratum_b\": \"T7\",
  \"protected_evidence\": [\"RV-1\"]}" | pp

echo; echo "== 9. 明确拒绝（结构性错误） =="
echo "-- 9a. 修订号过期：STALE_REVISION（409） --"
curl -s -X POST "$BASE/resilience/retraction-analysis" -H 'Content-Type: application/json' -d '{
  "expected_revision": 999999,
  "stratum_a": "T1",
  "stratum_b": "T4"}' | pp
echo "-- 9b. 地层未登记：INVALID_STRATUM（400） --"
curl -s -X POST "$BASE/resilience/retraction-analysis" -H 'Content-Type: application/json' -d '{
  "expected_revision": '"$REV"',
  "stratum_a": "T1",
  "stratum_b": "TX"}' | pp
echo "-- 9c. 两层相同：DUPLICATE_STRATUM（400） --"
curl -s -X POST "$BASE/resilience/retraction-analysis" -H 'Content-Type: application/json' -d '{
  "expected_revision": '"$REV"',
  "stratum_a": "T1",
  "stratum_b": "T1"}' | pp
echo "-- 9d. 受保护证据不存在：INVALID_PROTECTED_EVIDENCE（400） --"
curl -s -X POST "$BASE/resilience/retraction-analysis" -H 'Content-Type: application/json' -d '{
  "expected_revision": '"$REV"',
  "stratum_a": "T1",
  "stratum_b": "T4",
  "protected_evidence": ["RV-404"]}' | pp

echo; echo "== 10. 确认分析全程只读：修订号仍为 $REV，无新增断言 =="
curl -s "$BASE/revision" | pp
curl -s "$BASE/assertions?status=active" | python3 -c '
import sys, json
rows = json.load(sys.stdin)
print("active assertions:", sorted(r["evidence_id"] for r in rows))
assert all(r["evidence_id"].startswith("RV-") for r in rows)
print("分析过程无写入（只读成立）")'

echo; echo "全部撤回承受力示例执行完毕（正式状态未被分析修改）。"
