#!/usr/bin/env bash
# 考古地层关系核验服务 —— 野外终端整包汇入端到端示例
# 前提：服务已启动（docker compose up --build），监听 http://localhost:8000
#
# 演示：
#   1) 登记地层并提交正式批次作为基线；
#   2) 整包汇入：已登记目标站点一致、未登记目标随包建立、终端短号绑定建立；
#   3) 证据编号由终端/包号/序号稳定确定；成功只递增一次正式修订号；
#   4) 正式修订前进后，同内容重试幂等返回原结果（replayed=true）；
#   5) 同终端同包号异内容复用拒绝（PACKAGE_CONTENT_MISMATCH）；
#   6) 跨包改绑拒绝（SHORT_CODE_REBIND）、站点冲突拒绝（SITE_CONFLICT）；
#   7) 未知短号 / 重复序号 / 过期修订明确报错；
#   8) 关系冲突整包拒绝附证据路径，地层/绑定/断言/修订号均不变；
#   9) 稳定推导的证据编号被占用拒绝（DUPLICATE_EVIDENCE）；
#  10) 包列表 / 详情 / 终端绑定查询，变化记录来源 field_ingest。
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pp() { python3 -m json.tool 2>/dev/null || cat; }
rev() { curl -s "$BASE/revision" | python3 -c 'import sys,json;print(json.load(sys.stdin)["revision"])'; }

echo "== 0. 健康检查 =="
curl -s "$BASE/health" | pp

echo; echo "== 1. 登记地层（F1/F2 甲遗址，F3 乙遗址） =="
curl -s -X POST "$BASE/strata" -H 'Content-Type: application/json' \
  -d '{"stratum_id": "F1", "site": "甲遗址"}' >/dev/null || true
curl -s -X POST "$BASE/strata" -H 'Content-Type: application/json' \
  -d '{"stratum_id": "F2", "site": "甲遗址"}' >/dev/null || true
curl -s -X POST "$BASE/strata" -H 'Content-Type: application/json' \
  -d '{"stratum_id": "F3", "site": "乙遗址"}' >/dev/null || true
echo "已登记（若此前已存在则返回 STRATUM_EXISTS，不影响后续演示）"

echo; echo "== 2. 正式批次基线：F1 早于 F2 =="
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": $(rev),
  \"operations\": [
    {\"op\": \"assert\", \"evidence_id\": \"EV-FP-1\", \"stratum_a\": \"F1\", \"stratum_b\": \"F2\", \"relation\": \"earlier_than\"}
  ]}" | pp || true

echo; echo "== 3. 整包汇入 T-DEMO / PKG-1：A→F1（已登记）、B→F4（随包建立）、C→F2 =="
# 包体保存为变量：第 6 步的同内容重试须逐字节一致
PKG1_BODY="{
  \"site\": \"甲遗址\",
  \"terminal_id\": \"T-DEMO\",
  \"package_no\": \"PKG-1\",
  \"expected_revision\": $(rev),
  \"strata_map\": [
    {\"short_code\": \"A\", \"stratum_id\": \"F1\"},
    {\"short_code\": \"B\", \"stratum_id\": \"F4\"},
    {\"short_code\": \"C\", \"stratum_id\": \"F2\"}
  ],
  \"assertions\": [
    {\"seq\": 1, \"short_a\": \"A\", \"short_b\": \"B\", \"relation\": \"same_period\"},
    {\"seq\": 2, \"short_a\": \"B\", \"short_b\": \"C\", \"relation\": \"earlier_than\"}
  ]}"
echo "HTTP $(curl -s -o /tmp/fp_ingest.out -w '%{http_code}' -X POST "$BASE/field-packages" -H 'Content-Type: application/json' -d "$PKG1_BODY")"
pp < /tmp/fp_ingest.out

echo; echo "== 4. 关系查询：F4（短号 B）早于 F2 =="
curl -s "$BASE/relationship?a=F4&b=F2" | pp

echo; echo "== 5. 正式批次推进修订号（F2 早于 F3） =="
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": $(rev),
  \"operations\": [
    {\"op\": \"assert\", \"evidence_id\": \"EV-FP-2\", \"stratum_a\": \"F2\", \"stratum_b\": \"F3\", \"relation\": \"earlier_than\"}
  ]}" | pp || true

echo; echo "== 6. 同终端同包号同内容重试：HTTP 200，replayed=true，返回原修订号 =="
echo "HTTP $(curl -s -o /tmp/fp_replay.out -w '%{http_code}' -X POST "$BASE/field-packages" -H 'Content-Type: application/json' -d "$PKG1_BODY")"
pp < /tmp/fp_replay.out
echo "当前修订号（重试不递增）: $(rev)"

echo; echo "== 7. 同终端同包号异内容复用：409 PACKAGE_CONTENT_MISMATCH =="
curl -s -X POST "$BASE/field-packages" -H 'Content-Type: application/json' -d "{
  \"site\": \"甲遗址\",
  \"terminal_id\": \"T-DEMO\",
  \"package_no\": \"PKG-1\",
  \"expected_revision\": $(rev),
  \"strata_map\": [
    {\"short_code\": \"A\", \"stratum_id\": \"F1\"},
    {\"short_code\": \"B\", \"stratum_id\": \"F4\"},
    {\"short_code\": \"C\", \"stratum_id\": \"F2\"}
  ],
  \"assertions\": [
    {\"seq\": 1, \"short_a\": \"A\", \"short_b\": \"B\", \"relation\": \"same_period\"}
  ]}" | pp || true

echo; echo "== 8. 跨包改绑：短号 A 已绑定 F1，改绑 F2 拒绝（409 SHORT_CODE_REBIND） =="
curl -s -X POST "$BASE/field-packages" -H 'Content-Type: application/json' -d "{
  \"site\": \"甲遗址\",
  \"terminal_id\": \"T-DEMO\",
  \"package_no\": \"PKG-2\",
  \"expected_revision\": $(rev),
  \"strata_map\": [{\"short_code\": \"A\", \"stratum_id\": \"F2\"}],
  \"assertions\": [{\"seq\": 1, \"short_a\": \"A\", \"short_b\": \"A\", \"relation\": \"same_period\"}]
  }" | pp || true

echo; echo "== 9. 站点冲突：对照目标 F3 登记站点为乙遗址，包站点为甲遗址（409 SITE_CONFLICT） =="
curl -s -X POST "$BASE/field-packages" -H 'Content-Type: application/json' -d "{
  \"site\": \"甲遗址\",
  \"terminal_id\": \"T-DEMO\",
  \"package_no\": \"PKG-3\",
  \"expected_revision\": $(rev),
  \"strata_map\": [{\"short_code\": \"X\", \"stratum_id\": \"F3\"}, {\"short_code\": \"Y\", \"stratum_id\": \"F5\"}],
  \"assertions\": [{\"seq\": 1, \"short_a\": \"X\", \"short_b\": \"Y\", \"relation\": \"same_period\"}]
  }" | pp || true

echo; echo "== 10. 结构错误：未知短号 / 重复序号 / 过期修订 =="
echo "-- 10a. 断言引用对照外短号 Z：400 UNKNOWN_SHORT_CODE --"
curl -s -X POST "$BASE/field-packages" -H 'Content-Type: application/json' -d "{
  \"site\": \"甲遗址\",
  \"terminal_id\": \"T-DEMO\",
  \"package_no\": \"PKG-4\",
  \"expected_revision\": $(rev),
  \"strata_map\": [{\"short_code\": \"A\", \"stratum_id\": \"F1\"}],
  \"assertions\": [{\"seq\": 1, \"short_a\": \"A\", \"short_b\": \"Z\", \"relation\": \"same_period\"}]
  }" | pp || true
echo "-- 10b. 包内序号重复：400 DUPLICATE_SEQUENCE --"
curl -s -X POST "$BASE/field-packages" -H 'Content-Type: application/json' -d "{
  \"site\": \"甲遗址\",
  \"terminal_id\": \"T-DEMO\",
  \"package_no\": \"PKG-4\",
  \"expected_revision\": $(rev),
  \"strata_map\": [{\"short_code\": \"A\", \"stratum_id\": \"F1\"}, {\"short_code\": \"C\", \"stratum_id\": \"F2\"}],
  \"assertions\": [
    {\"seq\": 1, \"short_a\": \"A\", \"short_b\": \"C\", \"relation\": \"same_period\"},
    {\"seq\": 1, \"short_a\": \"C\", \"short_b\": \"A\", \"relation\": \"same_period\"}
  ]}" | pp || true
echo "-- 10c. 预期修订号过期：409 STALE_REVISION --"
curl -s -X POST "$BASE/field-packages" -H 'Content-Type: application/json' -d '{
  "site": "甲遗址",
  "terminal_id": "T-DEMO",
  "package_no": "PKG-4",
  "expected_revision": 999,
  "strata_map": [{"short_code": "A", "stratum_id": "F1"}, {"short_code": "C", "stratum_id": "F2"}],
  "assertions": [{"seq": 1, "short_a": "A", "short_b": "C", "relation": "same_period"}]
  }' | pp || true

echo; echo "== 11. 关系冲突：断言 F2 早于 F1 与 EV-FP-1 成环，整包拒绝（409 CONFLICT 附证据路径） =="
BEFORE=$(rev)
curl -s -X POST "$BASE/field-packages" -H 'Content-Type: application/json' -d "{
  \"site\": \"甲遗址\",
  \"terminal_id\": \"T-DEMO\",
  \"package_no\": \"PKG-5\",
  \"expected_revision\": $(rev),
  \"strata_map\": [{\"short_code\": \"P\", \"stratum_id\": \"F2\"}, {\"short_code\": \"Q\", \"stratum_id\": \"F1\"}, {\"short_code\": \"N\", \"stratum_id\": \"F9\"}],
  \"assertions\": [
    {\"seq\": 1, \"short_a\": \"P\", \"short_b\": \"Q\", \"relation\": \"earlier_than\"},
    {\"seq\": 2, \"short_a\": \"N\", \"short_b\": \"P\", \"relation\": \"same_period\"}
  ]}" | pp || true
echo "修订号未变: $(rev)（冲突前 $BEFORE）；F9 未随包建立："
curl -s "$BASE/strata" | python3 -c 'import sys,json;print([s["stratum_id"] for s in json.load(sys.stdin)])'

echo; echo "== 12. 证据编号占用：正式批次先占用 FT-T-DEMO2-PKG-6-1，汇入同编号拒绝 =="
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d "{
  \"expected_revision\": $(rev),
  \"operations\": [
    {\"op\": \"assert\", \"evidence_id\": \"FT-T-DEMO2-PKG-6-1\", \"stratum_a\": \"F4\", \"stratum_b\": \"F3\", \"relation\": \"earlier_than\"}
  ]}" >/dev/null || true
curl -s -X POST "$BASE/field-packages" -H 'Content-Type: application/json' -d "{
  \"site\": \"乙遗址\",
  \"terminal_id\": \"T-DEMO2\",
  \"package_no\": \"PKG-6\",
  \"expected_revision\": $(rev),
  \"strata_map\": [{\"short_code\": \"A\", \"stratum_id\": \"F3\"}, {\"short_code\": \"B\", \"stratum_id\": \"F6\"}],
  \"assertions\": [{\"seq\": 1, \"short_a\": \"A\", \"short_b\": \"B\", \"relation\": \"same_period\"}]
  }" | pp || true

echo; echo "== 13. 包列表 / 包详情 / 终端短号绑定 =="
curl -s "$BASE/field-packages" | pp
curl -s "$BASE/field-packages/T-DEMO/PKG-1" | pp
curl -s "$BASE/terminals/T-DEMO/short-codes" | pp

echo; echo "== 14. 变化记录中的 field_ingest 来源 =="
curl -s "$BASE/revision-changes?limit=1000" | python3 -c '
import sys, json
data = json.load(sys.stdin)
rows = [c for c in data["changes"] if c["source"] == "field_ingest"]
print(json.dumps(rows, ensure_ascii=False, indent=2))'

echo; echo "全部示例执行完毕。"
