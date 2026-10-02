#!/usr/bin/env bash
# 考古地层关系核验服务 —— 正式修订关系变化追踪端到端示例
# 前提：服务已启动（docker compose up --build），监听 http://localhost:8000
#
# 覆盖：
#   正式批次产生变化 / 关系不变但最优路径改变 / 撤回致变为未定（路径为空）/
#   失败批次不产生记录 / 推演内编辑不产生正式记录、并入作为正式修订记录 /
#   游标分页（同一轮固定修订上界，并发提交不进本轮）。
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pp() { python3 -m json.tool 2>/dev/null || cat; }

# 从标准输入的 JSON 中取字段，$1 为 python 下标表达式，如 "['revision']"
jget() { python3 -c "import sys,json; print(json.load(sys.stdin)$1)"; }

echo "== 0. 健康检查 =="
curl -s "$BASE/health" | pp

echo; echo "== 1. 登记地层 =="
for s in T1 T2 T3 T4; do
  curl -s -X POST "$BASE/strata" -H 'Content-Type: application/json' \
    -d "{\"stratum_id\": \"$s\", \"site\": \"变化追踪示例遗址\"}" | pp
done

echo; echo "== 2. 修订 N：T1~T2 同一期、T2 早于 T3（注意成功响应含 relation_changes 计数） =="
REV=$(curl -s "$BASE/revision" | jget "['revision']")
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": $REV,
  \"operations\": [
    {\"op\": \"assert\", \"evidence_id\": \"CH-001\", \"stratum_a\": \"T1\", \"stratum_b\": \"T2\", \"relation\": \"same_period\"},
    {\"op\": \"assert\", \"evidence_id\": \"CH-002\", \"stratum_a\": \"T2\", \"stratum_b\": \"T3\", \"relation\": \"earlier_than\"}
  ]}" | pp

echo; echo "== 3. 修订 N+1：新增直达证据 CH-010（T1 早于 T3），关系不变但最优路径变短 =="
REV=$(curl -s "$BASE/revision" | jget "['revision']")
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": $REV,
  \"operations\": [
    {\"op\": \"assert\", \"evidence_id\": \"CH-010\", \"stratum_a\": \"T1\", \"stratum_b\": \"T3\", \"relation\": \"earlier_than\"}
  ]}" | pp

echo; echo "== 4. 失败批次（T3 早于 T1 形成闭环，整批拒绝）：不产生修订与变化记录 =="
REV=$(curl -s "$BASE/revision" | jget "['revision']")
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": $REV,
  \"operations\": [
    {\"op\": \"assert\", \"evidence_id\": \"CH-099\", \"stratum_a\": \"T3\", \"stratum_b\": \"T1\", \"relation\": \"earlier_than\"}
  ]}" | pp

echo; echo "== 5. 修订 N+2：撤回 CH-010 与 CH-002，T1/T3、T2/T3 变为未定（新路径为空数组） =="
REV=$(curl -s "$BASE/revision" | jget "['revision']")
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": $REV,
  \"operations\": [
    {\"op\": \"retract\", \"evidence_id\": \"CH-010\"},
    {\"op\": \"retract\", \"evidence_id\": \"CH-002\"}
  ]}" | pp

echo; echo "== 6. 推演内编辑不产生正式变化记录 =="
PID=$(curl -s -X POST "$BASE/projections" -H 'Content-Type: application/json' \
  -d '{"label": "变化追踪推演"}' | jget "['id']")
curl -s -X POST "$BASE/projections/$PID/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 0,
  "operations": [
    {"op": "assert", "evidence_id": "CH-P01", "stratum_a": "T3", "stratum_b": "T4", "relation": "earlier_than"}
  ]}' | pp
echo "-- 推演批次后正式修订号不变："
curl -s "$BASE/revision" | pp

echo; echo "== 7. 并入：作为一次正式修订原子提交，变化记录 source=projection_merge =="
curl -s -X POST "$BASE/projections/$PID/merge" | pp

echo; echo "== 8. 游标分页读取（每页 2 条，演示 next_cursor 原样回传） =="
CURSOR=""
PAGE=1
while :; do
  echo "-- 第 $PAGE 页 --"
  if [ -z "$CURSOR" ]; then
    BODY=$(curl -s "$BASE/revision-changes?limit=2")
  else
    BODY=$(curl -s "$BASE/revision-changes?limit=2&cursor=$CURSOR")
  fi
  echo "$BODY" | pp
  CURSOR=$(echo "$BODY" | jget "['next_cursor']" 2>/dev/null || echo "")
  [ "$CURSOR" = "None" ] && CURSOR=""
  [ -z "$CURSOR" ] && break
  PAGE=$((PAGE + 1))
done
echo "整轮读取结束（最后一页 has_more=false、next_cursor=null）。"

echo; echo "== 9. 同一轮固定修订上界：首屏后即使有新提交，续页沿用上界 =="
FIRST=$(curl -s "$BASE/revision-changes?limit=1")
BOUND0=$(echo "$FIRST" | jget "['bound_revision']")
NEXTC=$(echo "$FIRST" | jget "['next_cursor']" 2>/dev/null || echo "")
# 首屏之后再提交一个新修订
REV=$(curl -s "$BASE/revision" | jget "['revision']")
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": $REV,
  \"operations\": [
    {\"op\": \"assert\", \"evidence_id\": \"CH-020\", \"stratum_a\": \"T2\", \"stratum_b\": \"T4\", \"relation\": \"same_period\"}
  ]}" > /dev/null
if [ -n "$NEXTC" ] && [ "$NEXTC" != "None" ]; then
  NEXT_BODY=$(curl -s "$BASE/revision-changes?limit=1&cursor=$NEXTC")
  echo "首屏 bound_revision=$BOUND0；并发提交后续页："
  echo "$NEXT_BODY" | pp
  echo "-> 续页 bound_revision 仍为 $BOUND0，本轮不含新提交；省略 cursor 重新开轮才会包含。"
else
  echo "（首屏已读完全部窗口，可自行加大数据量后重试本步）"
fi

echo; echo "全部示例执行完毕。"
