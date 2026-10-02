"""分期骨架视图（只读）。

考古人员需要一次性查看**当前全部地层**在某一正式修订视图下的分期骨架：

- 同一期断言把地层连成等价组（对称、可传递；孤立地层各自成组，全部已登记地层
  恰好出现一次）：组内地层编号排序，全部组按组内最小编号排序；
- 组间早于关系图只保留**传递约简**后的直接先后边——即不存在“经其他组到达”
  的路径（长度 >= 2）的边；每条保留边列出该组间**全部生效的直接早于证据编号**
  （按编号排序），同一组间的平行断言只形成一条边；
- 返回所用修订号、组、边及被传递关系省略的组间边数，顺序稳定。

修订号语义与正式批次一致：请求携带 expected_revision，过期返回 409
STALE_REVISION（details 含 current_revision），**只返回当前修订号，不输出任何
混合版本的数据**。全程只读：在 REPEATABLE READ READ ONLY 快照事务内读取
revision_counter 一次，组与边均由同一快照下的生效断言推导，不写库、不递增修订号、
不产生变化记录；现有批次、关系查询、定版及推演的请求与返回约定均不改变。
"""
from __future__ import annotations

from typing import Any, Dict, List

from .db import db_conn
from .derivation import Assertion, periodization_skeleton
from .errors import ApiError


def get_periodization(expected_revision: int) -> Dict[str, Any]:
    """只读分期骨架：同一正式修订视图下的等价组与组间直接早于关系图。"""
    with db_conn() as conn:
        with conn.cursor() as cur:
            # 只读快照事务：组与边取自同一快照，与并发写事务互不可见半批状态；
            # SET TRANSACTION 仅对当前事务生效，连接归还池后下一事务恢复默认模式。
            cur.execute("SET TRANSACTION READ ONLY ISOLATION LEVEL REPEATABLE READ")

            # 修订号在事务内只读取这一次：过期即拒绝，绝不混用更新修订下的组与边
            cur.execute("SELECT revision FROM revision_counter WHERE id = TRUE")
            current = cur.fetchone()[0]
            if expected_revision != current:
                raise ApiError(
                    409,
                    "STALE_REVISION",
                    f"修订号过期：请求携带 {expected_revision}，当前为 {current}",
                    {"current_revision": current},
                )

            cur.execute("SELECT stratum_id FROM strata ORDER BY stratum_id")
            strata = [r[0] for r in cur.fetchall()]
            assertions: List[Assertion] = _load_active(cur)

    # 纯函数推导：与 I/O 无关，顺序确定（组与边均按编号稳定排序）
    skeleton = periodization_skeleton(strata, assertions)

    return {
        "expected_revision": expected_revision,
        "current_revision": current,
        "revision": current,
        "groups": [
            {"group": i + 1, "min_stratum": g[0], "strata": list(g)}
            for i, g in enumerate(skeleton.groups)
        ],
        "edges": [
            {
                "from_group": e.from_group,
                "to_group": e.to_group,
                "from_min_stratum": e.from_min,
                "to_min_stratum": e.to_min,
                "relation": "earlier_than",
                "relation_label": "早于",
                "evidence": list(e.evidence),
            }
            for e in skeleton.edges
        ],
        "omitted_edge_count": skeleton.omitted_edge_count,
    }


def _load_active(cur) -> List[Assertion]:
    cur.execute(
        "SELECT evidence_id, stratum_a, stratum_b, relation "
        "FROM assertions WHERE status = 'active' ORDER BY evidence_id"
    )
    return [Assertion(evidence_id=r[0], a=r[1], b=r[2], relation=r[3]) for r in cur.fetchall()]
