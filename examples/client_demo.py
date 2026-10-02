#!/usr/bin/env python3
"""考古地层关系核验服务 —— Python 调用示例。

前提：服务已启动（docker compose up --build）。
依赖：pip install requests
运行：python3 examples/client_demo.py
"""
import json
import os
import sys

import requests

BASE = os.getenv("BASE_URL", "http://localhost:8000")


def show(title: str, resp: requests.Response) -> dict:
    body = resp.json()
    print(f"\n== {title} ==  HTTP {resp.status_code}")
    print(json.dumps(body, ensure_ascii=False, indent=2))
    return body


def main() -> None:
    # 1. 登记地层
    for s in ["L1", "L2", "L3", "L4"]:
        r = requests.post(f"{BASE}/strata", json={"stratum_id": s, "site": "甲遗址"})
        assert r.status_code in (201, 409), r.text

    # 2. 批次断言：L1~L2 同一期，L2 早于 L3，L3 早于 L4
    rev = requests.get(f"{BASE}/revision").json()["revision"]
    r = requests.post(
        f"{BASE}/batches",
        json={
            "expected_revision": rev,
            "operations": [
                {"op": "assert", "evidence_id": "EV-001", "stratum_a": "L1", "stratum_b": "L2", "relation": "same_period"},
                {"op": "assert", "evidence_id": "EV-002", "stratum_a": "L2", "stratum_b": "L3", "relation": "earlier_than"},
                {"op": "assert", "evidence_id": "EV-003", "stratum_a": "L3", "stratum_b": "L4", "relation": "earlier_than"},
            ],
        },
    )
    rev = show("批次断言", r)["revision"]

    # 3. 查询关系（推导：L1 早于 L4，证据路径 EV-001/EV-002/EV-003）
    show("查询 L1 vs L4", requests.get(f"{BASE}/relationship", params={"a": "L1", "b": "L4"}))

    # 4. 冲突：L4 早于 L1 形成闭环 -> 整批拒绝，原状态不变
    conflict_batch = {
        "expected_revision": rev,
        "operations": [
            {"op": "assert", "evidence_id": "EV-004", "stratum_a": "L4", "stratum_b": "L1", "relation": "earlier_than"}
        ],
    }
    # 4a. 提交前先做只读预检：预算 1 内寻找消除闭环所需撤回的最少旧证据
    show(
        "预检：撤哪条旧证据可让冲突批次通过（只读，不改状态）",
        requests.post(f"{BASE}/batches/preflight", json={**conflict_batch, "extra_retraction_budget": 1}),
    )
    r = requests.post(f"{BASE}/batches", json=conflict_batch)
    show("冲突批次（被拒绝）", r)

    # 5. 定版
    version = show("定版", requests.post(f"{BASE}/snapshots", json={"label": "第一期发掘定版"}))["version"]

    # 6. 撤回 EV-002 后，当前关系变为未定；旧版快照结论不变
    r = requests.post(
        f"{BASE}/batches",
        json={"expected_revision": rev, "operations": [{"op": "retract", "evidence_id": "EV-002"}]},
    )
    show("撤回 EV-002", r)
    show("撤回后查询 L1 vs L3", requests.get(f"{BASE}/relationship", params={"a": "L1", "b": "L3"}))
    show(
        f"版本 {version} 快照中的 L1 vs L3（不可变）",
        requests.get(f"{BASE}/snapshots/{version}/relationship", params={"a": "L1", "b": "L3"}),
    )

    # 7. 正式修订关系变化追踪：游标分页整轮读取
    print("\n== 正式修订关系变化追踪（游标分页） ==")
    cursor = None
    bound = None
    total = 0
    while True:
        params = {"limit": 2}  # 每页条数（1-1000）
        if cursor:
            params["cursor"] = cursor  # 原样回传服务端下发的 next_cursor
        body = requests.get(f"{BASE}/revision-changes", params=params).json()
        bound = body["bound_revision"]  # 本轮固定的正式修订上界
        for ch in body["changes"]:
            total += 1
            print(
                f"  修订{ch['revision']} [{ch['stratum_a']}/{ch['stratum_b']}] "
                f"{ch['old_relation_label']} -> {ch['new_relation_label']}  "
                f"旧路径={ch['old_evidence_path']} 新路径={ch['new_evidence_path']}  "
                f"来源={ch['source']}"
            )
        cursor = body["next_cursor"]
        if not cursor:
            break
    print(f"本轮上界修订={bound}，共 {total} 条变化（has_more=false 表示本轮读完）")


if __name__ == "__main__":
    try:
        main()
    except requests.ConnectionError:
        sys.exit(f"无法连接 {BASE}，请先启动服务：docker compose up --build")
