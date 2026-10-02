#!/usr/bin/env bash
# 考古地层关系核验服务 —— 提交前预检（POST /batches/preflight）端到端示例
#
# 预检只读：不生成修订、变化记录或断言，原状态不变。
# 输入沿用正式批次的 expected_revision / operations，
# 额外给出 extra_retraction_budget（0～3，默认可撤回的生效旧证据数上限）。
#
# 前提：服务已启动（docker compose up --build），监听 http://localhost:8000
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pp() { python3 -m json.tool 2>/dev/null || cat; }

echo "== 0. 登记本示例专用地层 =="
for s in A1 A2 A3 A4 A5 A6 A7; do
  curl -s -X POST "$BASE/strata" -H 'Content-Type: application/json' \
    -d "{\"stratum_id\": \"$s\", \"site\": \"预检演示区\"}" | pp
done

echo; echo "== 1. 建立无冲突正式视图（修订号 0 -> 1）：A1~A2 同一期，A2<A3，A3<A4；A5<A6<A7 =="
REV="$(curl -s "$BASE/revision" | python3 -c 'import sys,json;print(json.load(sys.stdin)["revision"])')"
if [ "$REV" = 0 ]; then
  curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
    "expected_revision": 0,
    "operations": [
      {"op": "assert", "evidence_id": "PV-1", "stratum_a": "A1", "stratum_b": "A2", "relation": "same_period"},
      {"op": "assert", "evidence_id": "PV-2", "stratum_a": "A2", "stratum_b": "A3", "relation": "earlier_than"},
      {"op": "assert", "evidence_id": "PV-3", "stratum_a": "A3", "stratum_b": "A4", "relation": "earlier_than"},
      {"op": "assert", "evidence_id": "PV-4", "stratum_a": "A5", "stratum_b": "A6", "relation": "earlier_than"},
      {"op": "assert", "evidence_id": "PV-5", "stratum_a": "A6", "stratum_b": "A7", "relation": "earlier_than"}
    ]}' | pp
fi
REV="$(curl -s "$BASE/revision" | python3 -c 'import sys,json;print(json.load(sys.stdin)["revision"])')"
echo "（当前修订号 $REV）"

echo; echo "== 2. 预检：原批次本可通过 -> already_consistent，额外撤回为空 =="
curl -s -X POST "$BASE/batches/preflight" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": $REV,
  \"extra_retraction_budget\": 2,
  \"operations\": [
    {\"op\": \"assert\", \"evidence_id\": \"PN-1\", \"stratum_a\": \"A4\", \"stratum_b\": \"A5\", \"relation\": \"earlier_than\"}
  ]}" | pp

echo; echo "== 3. 预检：新边 A4<A1 与链 A1..<A4 成闭环；预算 1 -> 建议撤回最少旧证据 =="
echo "   （冲突路径 PV-2,PV-3,PN-2,PV-1：拆同一期边 PV-1 或早于边 PV-2/PV-3 均可，取字典序最小 PV-1；"
echo "    PN-2 是本批新增证据，不会被建议撤回）"
curl -s -X POST "$BASE/batches/preflight" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": $REV,
  \"extra_retraction_budget\": 1,
  \"operations\": [
    {\"op\": \"assert\", \"evidence_id\": \"PN-2\", \"stratum_a\": \"A4\", \"stratum_b\": \"A1\", \"relation\": \"earlier_than\"}
  ]}" | pp

echo; echo "== 4. 同一批、预算 0 -> budget_insufficient，附当前仍存在的冲突证据路径 =="
curl -s -X POST "$BASE/batches/preflight" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": $REV,
  \"extra_retraction_budget\": 0,
  \"operations\": [
    {\"op\": \"assert\", \"evidence_id\": \"PN-2\", \"stratum_a\": \"A4\", \"stratum_b\": \"A1\", \"relation\": \"earlier_than\"}
  ]}" | pp

echo; echo "== 5. 预检：本批新断言自成矛盾（A6~A7 同一期且 A6<A7 组内早于）"
echo "   -> unfixable_new_assertions：无法靠撤回旧证据修复，附冲突路径 =="
curl -s -X POST "$BASE/batches/preflight" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": $REV,
  \"extra_retraction_budget\": 3,
  \"operations\": [
    {\"op\": \"assert\", \"evidence_id\": \"PN-3\", \"stratum_a\": \"A6\", \"stratum_b\": \"A7\", \"relation\": \"same_period\"},
    {\"op\": \"assert\", \"evidence_id\": \"PN-4\", \"stratum_a\": \"A6\", \"stratum_b\": \"A7\", \"relation\": \"earlier_than\"}
  ]}" | pp

echo; echo "== 6. 预检：两条新边同时形成两个闭环，预算 1 不足、预算 2 可解（取字典序最小组合） =="
curl -s -X POST "$BASE/batches/preflight" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": $REV,
  \"extra_retraction_budget\": 1,
  \"operations\": [
    {\"op\": \"assert\", \"evidence_id\": \"PN-5\", \"stratum_a\": \"A4\", \"stratum_b\": \"A1\", \"relation\": \"earlier_than\"},
    {\"op\": \"assert\", \"evidence_id\": \"PN-6\", \"stratum_a\": \"A7\", \"stratum_b\": \"A5\", \"relation\": \"earlier_than\"}
  ]}" | pp
curl -s -X POST "$BASE/batches/preflight" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": $REV,
  \"extra_retraction_budget\": 2,
  \"operations\": [
    {\"op\": \"assert\", \"evidence_id\": \"PN-5\", \"stratum_a\": \"A4\", \"stratum_b\": \"A1\", \"relation\": \"earlier_than\"},
    {\"op\": \"assert\", \"evidence_id\": \"PN-6\", \"stratum_a\": \"A7\", \"stratum_b\": \"A5\", \"relation\": \"earlier_than\"}
  ]}" | pp

echo; echo "== 7. 拒绝语义沿用正式批次（预检只读，原状态不变） =="
echo "-- 7a. 修订号过期：STALE_REVISION（409） --"
curl -s -X POST "$BASE/batches/preflight" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": 999999,
  \"operations\": [
    {\"op\": \"assert\", \"evidence_id\": \"PN-7\", \"stratum_a\": \"A1\", \"stratum_b\": \"A2\", \"relation\": \"same_period\"}
  ]}" | pp
echo "-- 7b. 证据编号已被占用（含已撤回）：DUPLICATE_EVIDENCE（409） --"
curl -s -X POST "$BASE/batches/preflight" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": $REV,
  \"operations\": [
    {\"op\": \"assert\", \"evidence_id\": \"PV-1\", \"stratum_a\": \"A1\", \"stratum_b\": \"A2\", \"relation\": \"same_period\"}
  ]}" | pp
echo "-- 7c. 撤回目标不存在 / 已撤回：ASSERTION_NOT_FOUND（409） --"
curl -s -X POST "$BASE/batches/preflight" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": $REV,
  \"operations\": [
    {\"op\": \"retract\", \"evidence_id\": \"PV-404\"}
  ]}" | pp
echo "-- 7d. 引用未登记地层：INVALID_STRATUM（400） --"
curl -s -X POST "$BASE/batches/preflight" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": $REV,
  \"operations\": [
    {\"op\": \"assert\", \"evidence_id\": \"PN-8\", \"stratum_a\": \"A1\", \"stratum_b\": \"AX\", \"relation\": \"same_period\"}
  ]}" | pp
echo "-- 7e. 预算超出 0～3：请求体校验失败（422） --"
curl -s -X POST "$BASE/batches/preflight" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": $REV,
  \"extra_retraction_budget\": 4,
  \"operations\": [
    {\"op\": \"assert\", \"evidence_id\": \"PN-9\", \"stratum_a\": \"A1\", \"stratum_b\": \"A2\", \"relation\": \"same_period\"}
  ]}" | pp

echo; echo "== 8. 确认预检全程只读：修订号仍为 $REV，PN-* 证据均未入库 =="
curl -s "$BASE/revision" | pp
curl -s "$BASE/assertions?status=active" | python3 -c '
import sys, json
rows = json.load(sys.stdin)
leaked = [r["evidence_id"] for r in rows if r["evidence_id"].startswith("PN-")]
print("active assertions:", [r["evidence_id"] for r in rows])
print("leaked preflight evidence:", leaked)
assert not leaked, leaked
print("预检验据无泄漏（只读成立）")'

echo; echo "全部预检示例执行完毕（正式状态未被预检修改）。"
