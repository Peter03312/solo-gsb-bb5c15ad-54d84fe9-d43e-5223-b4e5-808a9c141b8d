#!/usr/bin/env bash
# 考古地层关系核验服务 —— 野外终端离线整包汇入端到端示例
# 前提：服务已启动（docker compose up --build），监听 http://localhost:8000
#
# 演示：
#   1) 终端 T-07 用短号离线记录，回站后整包汇入（未登记地层随包建立）；
#   2) 成功返回短号映射、(终端,包号,序号) 稳定证据编号与新修订号；
#   3) 相同终端 + 包号 + 内容重试幂等返回原结果（即使正式修订已前进、预期号过期）；
#   4) 同终端同包号异内容复用拒绝；
#   5) 未知短号 / 重复序号 / 站点冲突 / 跨包改绑 / 过期修订明确拒绝；
#   6) 整包关系冲突（早于闭环）整批拒绝、附证据路径，地层 / 绑定 / 断言均不落地；
#   7) 第二个包沿用既有短号绑定，正式关系与变化追踪可查。
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pp() { python3 -m json.tool 2>/dev/null || cat; }
T="FP-DEMO-T07"

cur_rev() { curl -s "$BASE/revision" | python3 -c 'import sys,json;print(json.load(sys.stdin)["revision"])'; }

echo "== 0. 健康检查 / 当前修订号 =="
curl -s "$BASE/health"; echo
REV="$(cur_rev)"
echo "当前正式修订号：$REV"

echo; echo "== 1. 整包汇入：站点=西山坡，终端 $T，包号 2026-09-01-03 =="
echo "   短号 s1~s2 同一期，s2 早于 s3，s3 早于 s4。"
# 首次运行 REV 为当前修订号；重复运行本脚本时该包已存在，会幂等返回原结果（replayed=true）。
curl -s -X POST "$BASE/field-packages" -H 'Content-Type: application/json' -d "{
  \"site\": \"西山坡\",
  \"terminal_id\": \"$T\",
  \"package_no\": \"2026-09-01-03\",
  \"expected_revision\": $REV,
  \"mappings\": [
    {\"short_code\": \"s1\", \"stratum_id\": \"XSP-01\"},
    {\"short_code\": \"s2\", \"stratum_id\": \"XSP-02\"},
    {\"short_code\": \"s3\", \"stratum_id\": \"XSP-03\"},
    {\"short_code\": \"s4\", \"stratum_id\": \"XSP-04\"}
  ],
  \"assertions\": [
    {\"seq\": 1, \"stratum_a\": \"s1\", \"stratum_b\": \"s2\", \"relation\": \"same_period\"},
    {\"seq\": 2, \"stratum_a\": \"s2\", \"stratum_b\": \"s3\", \"relation\": \"earlier_than\"},
    {\"seq\": 3, \"stratum_a\": \"s3\", \"stratum_b\": \"s4\", \"relation\": \"earlier_than\"}
  ]}" | pp

echo; echo "== 2. 汇入结果已进入正式库：查询 XSP-01 与 XSP-04 的关系（应：早于，3 条稳定证据路径） =="
curl -s "$BASE/relationship?a=XSP-01&b=XSP-04" | pp

echo; echo "== 3. 幂等重试：同终端 / 同包号 / 同内容，expected_revision 故意写成 0（已过期） =="
echo "   返回首次结果且 replayed=true，不重复递增修订号。"
curl -s -X POST "$BASE/field-packages" -H 'Content-Type: application/json' -d "{
  \"site\": \"西山坡\",
  \"terminal_id\": \"$T\",
  \"package_no\": \"2026-09-01-03\",
  \"expected_revision\": 0,
  \"mappings\": [
    {\"short_code\": \"s1\", \"stratum_id\": \"XSP-01\"},
    {\"short_code\": \"s2\", \"stratum_id\": \"XSP-02\"},
    {\"short_code\": \"s3\", \"stratum_id\": \"XSP-03\"},
    {\"short_code\": \"s4\", \"stratum_id\": \"XSP-04\"}
  ],
  \"assertions\": [
    {\"seq\": 1, \"stratum_a\": \"s1\", \"stratum_b\": \"s2\", \"relation\": \"same_period\"},
    {\"seq\": 2, \"stratum_a\": \"s2\", \"stratum_b\": \"s3\", \"relation\": \"earlier_than\"},
    {\"seq\": 3, \"stratum_a\": \"s3\", \"stratum_b\": \"s4\", \"relation\": \"earlier_than\"}
  ]}" | pp

echo; echo "== 4. 异内容复用同一包号：拒绝（FIELD_PACKAGE_CONTENT_MISMATCH） =="
curl -s -X POST "$BASE/field-packages" -H 'Content-Type: application/json' -d "{
  \"site\": \"西山坡\",
  \"terminal_id\": \"$T\",
  \"package_no\": \"2026-09-01-03\",
  \"expected_revision\": 999,
  \"mappings\": [
    {\"short_code\": \"s1\", \"stratum_id\": \"XSP-01\"},
    {\"short_code\": \"s2\", \"stratum_id\": \"XSP-02\"}
  ],
  \"assertions\": [
    {\"seq\": 1, \"stratum_a\": \"s1\", \"stratum_b\": \"s2\", \"relation\": \"earlier_than\"}
  ]}" | pp || true

echo; echo "== 5. 结构与绑定类拒绝（结构错误先于修订号校验；绑定类错误携带当前修订号） =="
REV="$(cur_rev)"
echo "-- 5.0 首次汇入且修订号过期：STALE_REVISION（幂等重试不受此限，见第 3 步）"
curl -s -X POST "$BASE/field-packages" -H 'Content-Type: application/json' -d "{
  \"site\": \"西山坡\", \"terminal_id\": \"$T-stale\", \"package_no\": \"stale\",
  \"expected_revision\": 0,
  \"mappings\": [
    {\"short_code\": \"a\", \"stratum_id\": \"XSP-STALE-1\"},
    {\"short_code\": \"b\", \"stratum_id\": \"XSP-STALE-2\"}
  ],
  \"assertions\": [{\"seq\": 1, \"stratum_a\": \"a\", \"stratum_b\": \"b\", \"relation\": \"same_period\"}]}" | pp || true

echo "-- 5.1 未知短号（端点 sX 不在本包 mappings）："
curl -s -X POST "$BASE/field-packages" -H 'Content-Type: application/json' -d "{
  \"site\": \"西山坡\", \"terminal_id\": \"$T\", \"package_no\": \"bad-unknown\",
  \"expected_revision\": $REV,
  \"mappings\": [
    {\"short_code\": \"s1\", \"stratum_id\": \"XSP-01\"},
    {\"short_code\": \"s2\", \"stratum_id\": \"XSP-02\"}
  ],
  \"assertions\": [{\"seq\": 1, \"stratum_a\": \"s1\", \"stratum_b\": \"sX\", \"relation\": \"same_period\"}]}" | pp || true

echo "-- 5.2 包内序号重复："
curl -s -X POST "$BASE/field-packages" -H 'Content-Type: application/json' -d "{
  \"site\": \"西山坡\", \"terminal_id\": \"$T\", \"package_no\": \"bad-seq\",
  \"expected_revision\": $REV,
  \"mappings\": [
    {\"short_code\": \"s1\", \"stratum_id\": \"XSP-01\"},
    {\"short_code\": \"s2\", \"stratum_id\": \"XSP-02\"}
  ],
  \"assertions\": [
    {\"seq\": 7, \"stratum_a\": \"s1\", \"stratum_b\": \"s2\", \"relation\": \"same_period\"},
    {\"seq\": 7, \"stratum_a\": \"s2\", \"stratum_b\": \"s1\", \"relation\": \"earlier_than\"}
  ]}" | pp || true

echo "-- 5.3 包内短号重复："
curl -s -X POST "$BASE/field-packages" -H 'Content-Type: application/json' -d "{
  \"site\": \"西山坡\", \"terminal_id\": \"$T\", \"package_no\": \"bad-short\",
  \"expected_revision\": $REV,
  \"mappings\": [
    {\"short_code\": \"s1\", \"stratum_id\": \"XSP-01\"},
    {\"short_code\": \"s1\", \"stratum_id\": \"XSP-02\"}
  ],
  \"assertions\": [{\"seq\": 1, \"stratum_a\": \"s1\", \"stratum_b\": \"s1\", \"relation\": \"same_period\"}]}" | pp || true

echo "-- 5.4 站点冲突（XSP-01 已登记为西山坡，本包写北山头）："
curl -s -X POST "$BASE/field-packages" -H 'Content-Type: application/json' -d "{
  \"site\": \"北山头\", \"terminal_id\": \"$T\", \"package_no\": \"bad-site\",
  \"expected_revision\": $REV,
  \"mappings\": [
    {\"short_code\": \"s1\", \"stratum_id\": \"XSP-01\"},
    {\"short_code\": \"s9\", \"stratum_id\": \"XSP-99\"}
  ],
  \"assertions\": [{\"seq\": 1, \"stratum_a\": \"s1\", \"stratum_b\": \"s9\", \"relation\": \"same_period\"}]}" | pp || true

echo "-- 5.5 同一终端跨包改绑短号 s1（已绑 XSP-01，请求改绑 XSP-99）："
curl -s -X POST "$BASE/field-packages" -H 'Content-Type: application/json' -d "{
  \"site\": \"西山坡\", \"terminal_id\": \"$T\", \"package_no\": \"bad-rebind\",
  \"expected_revision\": $REV,
  \"mappings\": [
    {\"short_code\": \"s1\", \"stratum_id\": \"XSP-99\"},
    {\"short_code\": \"s2\", \"stratum_id\": \"XSP-02\"}
  ],
  \"assertions\": [{\"seq\": 1, \"stratum_a\": \"s1\", \"stratum_b\": \"s2\", \"relation\": \"same_period\"}]}" | pp || true

echo; echo "== 6. 整包关系冲突：s4<s5<s6 与既有 s2<s3<s4 反向成环 =="
echo "   整批拒绝（FIELD_CONFLICT，附证据路径），XSP-05/06 不登记、绑定不建立。"
REV="$(cur_rev)"
curl -s -X POST "$BASE/field-packages" -H 'Content-Type: application/json' -d "{
  \"site\": \"西山坡\",
  \"terminal_id\": \"$T\",
  \"package_no\": \"bad-cycle\",
  \"expected_revision\": $REV,
  \"mappings\": [
    {\"short_code\": \"s2\", \"stratum_id\": \"XSP-02\"},
    {\"short_code\": \"s4\", \"stratum_id\": \"XSP-04\"},
    {\"short_code\": \"s5\", \"stratum_id\": \"XSP-05\"},
    {\"short_code\": \"s6\", \"stratum_id\": \"XSP-06\"}
  ],
  \"assertions\": [
    {\"seq\": 1, \"stratum_a\": \"s4\", \"stratum_b\": \"s5\", \"relation\": \"earlier_than\"},
    {\"seq\": 2, \"stratum_a\": \"s5\", \"stratum_b\": \"s6\", \"relation\": \"earlier_than\"},
    {\"seq\": 3, \"stratum_a\": \"s6\", \"stratum_b\": \"s2\", \"relation\": \"earlier_than\"}
  ]}" | pp || true

echo; echo "== 7. 第二个包成功汇入：沿用 s4 既有绑定（不随包重建），新增 s5 绑定 =="
REV="$(cur_rev)"
curl -s -X POST "$BASE/field-packages" -H 'Content-Type: application/json' -d "{
  \"site\": \"西山坡\",
  \"terminal_id\": \"$T\",
  \"package_no\": \"2026-09-02-01\",
  \"expected_revision\": $REV,
  \"mappings\": [
    {\"short_code\": \"s4\", \"stratum_id\": \"XSP-04\"},
    {\"short_code\": \"s5\", \"stratum_id\": \"XSP-05\"}
  ],
  \"assertions\": [
    {\"seq\": 1, \"stratum_a\": \"s4\", \"stratum_b\": \"s5\", \"relation\": \"same_period\"}
  ]}" | pp

echo; echo "== 8. 变化追踪（source=field_package）与包台账 =="
curl -s "$BASE/revision-changes?limit=5" | pp
curl -s "$BASE/field-packages" | pp
curl -s "$BASE/field-packages/$T/2026-09-01-03" | pp

echo; echo "全部野外整包汇入示例执行完毕。"
