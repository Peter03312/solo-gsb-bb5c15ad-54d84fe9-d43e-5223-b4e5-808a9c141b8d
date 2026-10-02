"""地层关系推导引擎（纯 Python，无外部依赖）。

语义规则：
- 同一期（same_period）：对称、可传递，构成等价类（并查集）。
- 早于（earlier_than）：沿“同一期 + 早于”混合路径推导。
  若 x ~ a（同一期链）、a < b（早于）、b ~ y（同一期链），则 x 早于 y。
- 约束（对整批应用后的最终关系核验）：
  1. 组内早于禁止：同一等价类内不得存在（直接或间接的）早于关系；
  2. 早于闭环禁止：等价类商图上的早于关系必须是有向无环图（DAG）。

路径选取：证据数最少者优先；并列时按证据编号序列字典序最小者。

分期试排（trial_arrangement）：在同一期等价组商图（DAG）上求每组在
“同组同期、早于严格递增、锚点期位固定、总期数 1～64”约束下，于**全部**
可行排布中可取的最早 / 最晚期位（ASAP/ALAP 界），或分类给出不可行原因。
"""
from __future__ import annotations

import heapq
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

SAME = "same_period"
EARLIER = "earlier_than"

RELATION_LABELS = {
    "same_period": "同一期",
    "earlier_than": "早于",
    "later_than": "晚于",
    "undetermined": "未定",
}


@dataclass(frozen=True)
class Assertion:
    """一条生效中的断言。relation=earlier_than 表示 a 早于 b。"""

    evidence_id: str
    a: str
    b: str
    relation: str


class UnionFind:
    def __init__(self) -> None:
        self.parent: Dict[str, str] = {}
        self.rank: Dict[str, int] = {}

    def find(self, x: str) -> str:
        if x not in self.parent:
            self.parent[x] = x
            self.rank[x] = 0
            return x
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        # 路径压缩
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, x: str, y: str) -> str:
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return rx
        if self.rank[rx] < self.rank[ry]:
            rx, ry = ry, rx
        self.parent[ry] = rx
        if self.rank[rx] == self.rank[ry]:
            self.rank[rx] += 1
        return rx


def _build_adj(assertions: Iterable[Assertion], kinds: frozenset) -> Dict[str, List[Tuple[str, str]]]:
    """构建邻接表：同一期为无向边，早于是 a -> b 的有向边。边权为证据编号。"""
    adj: Dict[str, List[Tuple[str, str]]] = {}
    for a in assertions:
        if a.relation == SAME and SAME in kinds:
            adj.setdefault(a.a, []).append((a.b, a.evidence_id))
            adj.setdefault(a.b, []).append((a.a, a.evidence_id))
        elif a.relation == EARLIER and EARLIER in kinds:
            adj.setdefault(a.a, []).append((a.b, a.evidence_id))
    return adj


def best_path(adj: Dict[str, List[Tuple[str, str]]], start: str, goal: str) -> Optional[Tuple[str, ...]]:
    """Dijkstra，代价为 (证据数, 证据编号元组)，即先最少证据、再按编号字典序。

    返回证据编号序列（推导顺序），不可达返回 None。
    """
    if start == goal:
        return ()
    best: Dict[str, Tuple[int, Tuple[str, ...]]] = {start: (0, ())}
    pq: List[Tuple[Tuple[int, Tuple[str, ...]], str]] = [((0, ()), start)]
    while pq:
        cost, node = heapq.heappop(pq)
        if cost != best.get(node):
            continue
        if node == goal:
            return cost[1]
        for nxt, eid in adj.get(node, ()):
            ncost = (cost[0] + 1, cost[1] + (eid,))
            old = best.get(nxt)
            if old is None or ncost < old:
                best[nxt] = ncost
                heapq.heappush(pq, (ncost, nxt))
    return None


def _find_cycle(edges: Dict[str, List[Tuple[str, Assertion]]]) -> Optional[List[Assertion]]:
    """在等价类商图上找一个有向环，返回构成环的“早于”断言序列（沿环方向）。"""
    color: Dict[str, int] = {}  # 0/缺省=未访问 1=在栈上 2=已完成

    def ordered(n: str) -> List[Tuple[str, Assertion]]:
        return sorted(edges.get(n, []), key=lambda t: (t[1].evidence_id, t[0]))

    for start in sorted(edges):
        if color.get(start):
            continue
        color[start] = 1
        path_nodes: List[str] = [start]
        path_edges: List[Assertion] = []  # path_edges[i]: path_nodes[i] -> path_nodes[i+1]
        stack = [(start, iter(ordered(start)))]
        while stack:
            node, it = stack[-1]
            advanced = False
            for v, a in it:
                c = color.get(v, 0)
                if c == 0:
                    color[v] = 1
                    path_nodes.append(v)
                    path_edges.append(a)
                    stack.append((v, iter(ordered(v))))
                    advanced = True
                    break
                if c == 1:
                    idx = path_nodes.index(v)
                    return path_edges[idx:] + [a]
            if not advanced:
                color[node] = 2
                stack.pop()
                path_nodes.pop()
                if path_edges:
                    path_edges.pop()
    return None


class Derivation:
    """对一组生效断言的推导与核验。"""

    def __init__(self, assertions: Sequence[Assertion]) -> None:
        self.assertions: List[Assertion] = sorted(assertions, key=lambda a: a.evidence_id)
        self.uf = UnionFind()
        for a in self.assertions:
            self.uf.find(a.a)
            self.uf.find(a.b)
        for a in self.assertions:
            if a.relation == SAME:
                self.uf.union(a.a, a.b)
        self.same_adj = _build_adj(self.assertions, frozenset({SAME}))
        self.mixed_adj = _build_adj(self.assertions, frozenset({SAME, EARLIER}))

    # ------------------------------------------------------------------ 核验
    def validate(self) -> List[dict]:
        """核验最终关系，返回冲突列表（空列表表示通过）。"""
        conflicts: List[dict] = []

        # 1) 组内早于：早于断言两端落在同一等价类
        for a in self.assertions:
            if a.relation != EARLIER:
                continue
            if self.uf.find(a.a) == self.uf.find(a.b):
                chain = best_path(self.same_adj, a.a, a.b) or ()
                conflicts.append(
                    {
                        "type": "intra_group_earlier",
                        "message": (
                            f"证据 {a.evidence_id} 断言 {a.a} 早于 {a.b}，"
                            f"但同一期证据链表明二者同属一期（组内早于）"
                        ),
                        "evidence_path": list(chain) + [a.evidence_id],
                    }
                )

        # 2) 早于闭环：等价类商图上的有向环
        best_edge: Dict[Tuple[str, str], Assertion] = {}
        for a in self.assertions:
            if a.relation != EARLIER:
                continue
            ra, rb = self.uf.find(a.a), self.uf.find(a.b)
            if ra == rb:
                continue
            key = (ra, rb)
            if key not in best_edge or a.evidence_id < best_edge[key].evidence_id:
                best_edge[key] = a
        edges: Dict[str, List[Tuple[str, Assertion]]] = {}
        for (ra, rb), a in best_edge.items():
            edges.setdefault(ra, []).append((rb, a))

        cycle = _find_cycle(edges)
        if cycle:
            full_path: List[str] = []
            k = len(cycle)
            for i, cur in enumerate(cycle):
                nxt = cycle[(i + 1) % k]
                full_path.append(cur.evidence_id)
                full_path.extend(best_path(self.same_adj, cur.b, nxt.a) or ())
            conflicts.append(
                {
                    "type": "earlier_cycle",
                    "message": "早于关系形成闭环: "
                    + " -> ".join(f"{e.evidence_id}({e.a} 早于 {e.b})" for e in cycle),
                    "evidence_path": full_path,
                }
            )
        return conflicts

    # ------------------------------------------------------------------ 查询
    def relation(self, x: str, y: str) -> Tuple[str, Optional[List[str]]]:
        """返回 (关系, 证据路径)。关系 ∈ same_period / earlier_than / later_than / undetermined。"""
        if x == y:
            return "same_period", []
        if self.uf.find(x) == self.uf.find(y):
            path = best_path(self.same_adj, x, y) or ()
            return "same_period", list(path)
        p = best_path(self.mixed_adj, x, y)
        if p is not None:
            return "earlier_than", list(p)
        p = best_path(self.mixed_adj, y, x)
        if p is not None:
            return "later_than", list(p)
        return "undetermined", None


# ------------------------------------------------------------------ 分期骨架
@dataclass(frozen=True)
class SkeletonEdge:
    """组间早于关系图上的一条**直接**（传递约简后保留）边。

    from_min < to_min 不做要求：方向即时间方向（from 组早于 to 组）；
    evidence 为该组间全部生效的直接早于证据编号（平行断言合并为一条边，按编号排序）。
    """

    from_group: int          # 组序号（组按最小编号排序后从 1 起）
    to_group: int
    from_min: str            # 源组最小编号（稳定标识）
    to_min: str
    evidence: Tuple[str, ...]


@dataclass
class Skeleton:
    """分期骨架：同一期等价组（含孤立地层）+ 组间直接早于关系图（传递约简图）。"""

    groups: Tuple[Tuple[str, ...], ...]  # 每组内地层按编号排序；组间按首元素（最小编号）排序
    edges: Tuple[SkeletonEdge, ...]      # 边按 (源组最小编号, 目标组最小编号) 排序
    omitted_edge_count: int              # 被传递关系省略的组间边数（平行断言只算一条边）


def periodization_skeleton(
    strata: Sequence[str],
    assertions: Sequence[Assertion],
) -> Skeleton:
    """由已登记地层与生效断言构造分期骨架（纯函数，结果顺序稳定）。

    - 同一期（same_period）断言的传递闭包构成等价类；未出现在任何同一期断言中的
      地层各自成组（孤立地层），因此全部已登记地层恰好出现一次；
    - 组间早于图：每条生效“早于”断言按其两端所在组投影为一条商图有向边
      （源组早于目标组），同一组间的多条平行断言合并为一条边；
    - 传递约简：若从源组到目标组存在长度 >= 2 的组间路径（经其他组），则该直接边
      可由传递关系推出，予以省略；只保留“不存在经其他组到达”的直接先后边。
      生效断言集已通过 validate()（无组内早于、商图为 DAG），DAG 的传递约简唯一。
    """
    uf = UnionFind()
    for s in strata:
        uf.find(s)
    for a in assertions:
        uf.find(a.a)
        uf.find(a.b)
    for a in assertions:
        if a.relation == SAME:
            uf.union(a.a, a.b)

    members: Dict[str, List[str]] = defaultdict(list)
    for s in strata:
        members[uf.find(s)].append(s)
    groups_list = [sorted(m) for m in members.values()]
    groups_list.sort(key=lambda g: g[0])
    groups_tuple = tuple(tuple(g) for g in groups_list)

    # 最小地层编号 -> 组序号（从 1 起），组按最小编号排序
    index_by_min: Dict[str, int] = {g[0]: i + 1 for i, g in enumerate(groups_tuple)}
    index_by_root: Dict[str, int] = {uf.find(g[0]): i + 1 for i, g in enumerate(groups_tuple)}

    # 平行断言合并：(源组最小编号, 目标组最小编号) -> 全部生效直接早于证据编号
    raw_edges: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
    for a in assertions:
        if a.relation != EARLIER:
            continue
        ra, rb = uf.find(a.a), uf.find(a.b)
        if ra == rb:
            # 生效断言集理论上已通过 validate()（组内早于是冲突，不可能落库）；
            # 防御性跳过，保证骨架视图在任何输入下只产出组间边。
            continue
        ga, gb = groups_list[index_by_root[ra] - 1], groups_list[index_by_root[rb] - 1]
        raw_edges[(ga[0], gb[0])].add(a.evidence_id)

    # 商图邻接（不含证据），用于“是否存在经其他组到达”的可达性判定
    adj: Dict[str, Set[str]] = {g[0]: set() for g in groups_tuple}
    for (u, v) in raw_edges:
        adj[u].add(v)

    def reachable_via_other(src: str, dst: str) -> bool:
        """从 src 出发是否存在长度 >= 2 的组间路径到达 dst（必须经过中间组）。

        枚举 src 的直接后继（跳过直达 dst 的那条边，且同组间平行断言已合并为一条边），
        再判断该后继能否（经零或多条组间边）到达 dst。商图是 DAG，普通 BFS 即可。
        """
        for first in adj.get(src, ()):  # 第一步：必须经过另一个组
            if first == dst:
                continue
            seen = {first}
            queue = deque([first])
            while queue:
                node = queue.popleft()
                if node == dst:
                    return True
                for nxt in adj.get(node, ()):
                    if nxt not in seen:
                        seen.add(nxt)
                        queue.append(nxt)
        return False

    kept: List[SkeletonEdge] = []
    omitted = 0
    for (umin, vmin), evidence in raw_edges.items():
        if reachable_via_other(umin, vmin):
            omitted += 1
            continue
        kept.append(
            SkeletonEdge(
                from_group=index_by_min[umin],
                to_group=index_by_min[vmin],
                from_min=umin,
                to_min=vmin,
                evidence=tuple(sorted(evidence)),
            )
        )
    kept.sort(key=lambda e: (e.from_min, e.to_min))
    return Skeleton(groups=groups_tuple, edges=tuple(kept), omitted_edge_count=omitted)


# ------------------------------------------------------------------ 分期试排
@dataclass(frozen=True)
class ArrangementAnchor:
    """一个地层期位锚点（结构校验在服务层完成：存在、不重复、期位 1..periods）。"""

    stratum: str
    period: int


@dataclass
class ArrangementFeasible:
    """可行试排结果：每组在**全部可行排布**中的最早/最晚期位（ASAP/ALAP 界）。

    earliest==latest 即该组期位在所有可行排布中被唯一确定；锚定组恒相等。
    这是界而不是某一套任意排布：ASAP/ALAP 各自都能扩展为完整可行排布。
    """

    feasible: bool
    total_periods: int
    groups: Tuple["ArrangementGroup", ...]


@dataclass(frozen=True)
class ArrangementGroup:
    group: int                 # 组序号（按最小编号排序，从 1 起，与分期骨架一致）
    min_stratum: str
    strata: Tuple[str, ...]
    earliest_period: int       # 所有可行排布中的最早期位
    latest_period: int         # 所有可行排布中的最晚期位
    anchored: bool
    anchors: Tuple[ArrangementAnchor, ...]


@dataclass
class ArrangementInfeasible:
    """不可行试排：分类给出全部可定位原因，不返回任何组的期位（不返回部分排布）。"""

    feasible: bool
    total_periods: int
    reasons: Tuple[dict, ...]


def _chain_evidence_path(
    assertions: Sequence[Assertion],
    groups: Tuple[Tuple[str, ...], ...],
    chain: Sequence[int],
    start_stratum: Optional[str],
    end_stratum: Optional[str],
) -> Tuple[str, ...]:
    """求覆盖整条组链（group chain[0] 早于 chain[1] 早于 ...）的最优证据路径。

    在“同一期 + 早于”混合图上只保留：
    - 链上第 i 组内部的同一期移动；
    - 从链上第 i 组直接进入第 i+1 组的早于断言（不允许跳组）。
    起点 / 终点给定时（锚点地层）固定，否则取链端组的最小编号地层。
    最优口径与全局一致：证据数最少，并列证据编号序列字典序最小。
    """
    pos_by_group = {g: i for i, g in enumerate(chain)}
    group_of_stratum: Dict[str, int] = {}
    for i, members in enumerate(groups):
        if i in pos_by_group:
            for s in members:
                group_of_stratum[s] = pos_by_group[i]
    src = start_stratum or groups[chain[0]][0]
    dst = end_stratum or groups[chain[-1]][0]

    adj: Dict[str, List[Tuple[str, str]]] = {}
    for ar in assertions:
        ga, gb = group_of_stratum.get(ar.a), group_of_stratum.get(ar.b)
        if ga is None or gb is None:
            continue
        if ar.relation == SAME:
            if ga != gb:
                continue
            adj.setdefault(ar.a, []).append((ar.b, ar.evidence_id))
            adj.setdefault(ar.b, []).append((ar.a, ar.evidence_id))
        else:  # EARLIER
            if gb != ga + 1:
                continue  # 仅保留沿链前进一步的断言（跳组边不能逐步证明本链）
            adj.setdefault(ar.a, []).append((ar.b, ar.evidence_id))
    return tuple(best_path(adj, src, dst) or ())


def trial_arrangement(
    strata: Sequence[str],
    assertions: Sequence[Assertion],
    total_periods: int,
    anchors: Sequence[ArrangementAnchor],
) -> "ArrangementFeasible | ArrangementInfeasible":
    """分期试排纯函数（入参的结构合法性由服务层保证）。

    等价组与商图口径与 periodization_skeleton 一致（生效断言集已通过核验，
    商图为 DAG；此处防御性处理环，理论上不可达）。

    锚点：stratum -> period；同一等价组上多个锚点期位不同即“同组锚点矛盾”
    （anchor_group_conflict），优先单独返回。

    界：在组商图 DAG 上做带固定点的最早 / 最迟调度。
      非锚定组沿拓扑序求下界 es[v]=max(1, max_u base(u)+1)、沿逆拓扑序求上界
      ls[v]=min(P, min_w base(w)-1)，其中 base 取前驱 / 后继的固定期位（锚点）
      或其传播值；传播在锚点处截断（链上的锚点固定占位，不能穿过）。
      锚定组另算前驱需求 fneed[v] 与后继许可 fallow[v]（计算时下界 / 上界
      传播同样在其他锚点处停止），其固定期位 < fneed 或 > fallow 即冲突——
      由此能检出两个锚定组直接或经非锚定组反序。
    可行性 ⇔ 无同组锚点矛盾、每个非锚定组 es<=ls、每个锚定组 fneed<=fixed<=fallow。
    可行时非锚定组的最早 / 最晚期位界即其 es/ls：ASAP（各非锚定组取 es）与
    ALAP（各取 ls）都能扩展为完整可行排布，故 es/ls 恰为该组在全部可行排布中的
    最早 / 最晚期位；锚定组两界恒为固定期位。
    不可行时沿决定界的前驱 / 后继指针回溯出一条不含多余锚点的最短方向链，
    锚定组的下界侧 / 上界侧冲突各自独立成链，附端点、所需 / 可用期位与证据路径。
    """
    n_periods = total_periods

    # ---- 等价组（与骨架同一口径）----
    uf = UnionFind()
    for s in strata:
        uf.find(s)
    for a in assertions:
        uf.find(a.a)
        uf.find(a.b)
    for a in assertions:
        if a.relation == SAME:
            uf.union(a.a, a.b)

    members: Dict[str, List[str]] = defaultdict(list)
    for s in strata:
        members[uf.find(s)].append(s)
    groups_list = [sorted(m) for m in members.values()]
    groups_list.sort(key=lambda g: g[0])
    groups_tuple = tuple(tuple(g) for g in groups_list)
    n = len(groups_tuple)
    index_by_root: Dict[str, int] = {uf.find(g[0]): i for i, g in enumerate(groups_tuple)}

    # ---- 组间商图直接边：平行断言保留原始断言（不做传递约简：链长按所有边计）----
    edge_assertions_map: Dict[Tuple[int, int], List[Assertion]] = defaultdict(list)
    for a in assertions:
        if a.relation != EARLIER:
            continue
        gi = index_by_root.get(uf.find(a.a))
        gj = index_by_root.get(uf.find(a.b))
        if gi is None or gj is None or gi == gj:
            continue  # 组内早于在生效断言集中不可能出现（核验已拒）
        edge_assertions_map[(gi, gj)].append(a)
    edge_assertions: Dict[Tuple[int, int], Tuple[Assertion, ...]] = {
        k: tuple(sorted(v, key=lambda ar: ar.evidence_id)) for k, v in edge_assertions_map.items()
    }
    succ: Dict[int, Set[int]] = {i: set() for i in range(n)}
    pred: Dict[int, Set[int]] = {i: set() for i in range(n)}
    for (u, v) in edge_assertions:
        succ[u].add(v)
        pred[v].add(u)

    same_adj = _build_adj(assertions, frozenset({SAME}))

    # ---- 锚点归组（服务层已保证地层存在、不重复、期位 1..n_periods）----
    group_anchors: Dict[int, List[ArrangementAnchor]] = defaultdict(list)
    for anc in anchors:
        group_anchors[index_by_root[uf.find(anc.stratum)]].append(anc)
    for v in group_anchors:
        group_anchors[v].sort(key=lambda x: x.stratum)

    # ---- 第一类不可行：同组锚点矛盾 ----
    conflict_reasons: List[dict] = []
    for v in sorted(group_anchors, key=lambda i: groups_tuple[i][0]):
        periods = sorted({anc.period for anc in group_anchors[v]})
        if len(periods) > 1:
            ancs = sorted(group_anchors[v], key=lambda x: (x.period, x.stratum))
            # 组内最早的两个不同期位锚点之间的同一期证据链
            first = next(anc for anc in ancs if anc.period == periods[0])
            second = next(anc for anc in ancs if anc.period == periods[1])
            same_path = list(best_path(same_adj, first.stratum, second.stratum) or ())
            conflict_reasons.append(
                {
                    "type": "anchor_group_conflict",
                    "message": (
                        f"同一期等价组（最小编号 {groups_tuple[v][0]}）被锚定到不同期位："
                        + "、".join(
                            f"{anc.stratum}→第{anc.period}期" for anc in ancs
                        )
                    ),
                    "group": v + 1,
                    "min_stratum": groups_tuple[v][0],
                    "strata": list(groups_tuple[v]),
                    "anchors": [{"stratum": anc.stratum, "period": anc.period} for anc in ancs],
                    "evidence_path": same_path,
                }
            )
    if conflict_reasons:
        return ArrangementInfeasible(
            feasible=False, total_periods=n_periods, reasons=tuple(conflict_reasons)
        )

    fixed: Dict[int, int] = {v: group_anchors[v][0].period for v in group_anchors}

    # ---- 防御性拓扑排序（商图理论上必为 DAG；排序仅用于 ES/LS 计算顺序）----
    indeg = {v: len(pred[v]) for v in range(n)}
    ready = [v for v in range(n) if indeg[v] == 0]
    _h = [(groups_tuple[v][0], v) for v in ready]  # (组最小编号, 组下标) 稳定堆键
    heapq.heapify(_h)
    topo: List[int] = []
    while _h:
        _, v = heapq.heappop(_h)
        topo.append(v)
        for w in sorted(succ[v], key=lambda i: groups_tuple[i][0]):
            indeg[w] -= 1
            if indeg[w] == 0:
                heapq.heappush(_h, (groups_tuple[w][0], w))

    # ---- ES：拓扑序求下界需求。锚点不“截断”需求：固定值与前驱需求取较大者，
    #      因此两个都被锚定的组直接违反先后（fixed[v] < es_pred+1）也能检出。----
    es: Dict[int, int] = {v: 1 for v in range(n)}
    es_from: Dict[int, Optional[int]] = {v: None for v in range(n)}  # 决定下界需求的前驱
    for v in topo:
        best_val, best_u = 1, None
        for u in pred[v]:
            cand = es[u] + 1
            if cand > best_val or (
                cand == best_val and (best_u is None or groups_tuple[u][0] < groups_tuple[best_u][0])
            ):
                best_val, best_u = cand, u
    # ---- 界传播（在锚点处截断）----
    # 非锚定组的最早/最晚期位只能沿“不含其他锚定组”的链推导：链上的锚点会固定
    # 占位，因此 es/ls 传播到锚点处停止。锚定组自身的可行性不靠截断后的界，
    # 而单独用前驱下界需求 / 后继上界许可比对其固定值（从而能检出两个锚定组
    # 直接或经链反序的冲突）。
    # ES：非锚定组下界 = max(1, 非锚定前驱 es+1)；锚点之后的组也会从该锚点受界，
    #     因此锚定前驱仍按 fixed+1 参与传播，只不继续向前回溯。
    es: Dict[int, int] = {v: 1 for v in range(n)}
    es_from: Dict[int, Optional[int]] = {v: None for v in range(n)}
    for v in topo:
        if v in fixed:
            es[v] = fixed[v]
            continue
        best_val, best_u = 1, None
        for u in pred[v]:
            base = fixed[u] if u in fixed else es[u]
            cand = base + 1
            if cand > best_val or (
                cand == best_val and (best_u is None or groups_tuple[u][0] < groups_tuple[best_u][0])
            ):
                best_val, best_u = cand, u
        if best_u is not None:
            es[v], es_from[v] = best_val, best_u

    # LS：非锚定组上界 = min(P, 非锚定后继 ls-1)；锚定后继按 fixed-1 参与传播。
    ls: Dict[int, int] = {v: n_periods for v in range(n)}
    ls_from: Dict[int, Optional[int]] = {v: None for v in range(n)}
    for v in reversed(topo):
        if v in fixed:
            ls[v] = fixed[v]
            continue
        best_val, best_w = n_periods, None
        for w in succ[v]:
            base = fixed[w] if w in fixed else ls[w]
            cand = base - 1
            if cand < best_val or (
                cand == best_val and (best_w is None or groups_tuple[w][0] < groups_tuple[best_w][0])
            ):
                best_val, best_w = cand, w
        if best_w is not None:
            ls[v], ls_from[v] = best_val, best_w

    # 锚定组的独立前驱需求 / 后继许可（不截断，沿非锚定节点一直推到边界，
    # 用于判定固定-固定反序与固定值夹不住整条链）。存独立字典，避免覆盖
    # 非锚定组传播所用的 es/ls。
    fneed: Dict[int, int] = {}
    fallow: Dict[int, int] = {}
    need_from: Dict[int, Optional[int]] = {v: None for v in range(n)}
    allow_from: Dict[int, Optional[int]] = {v: None for v in range(n)}
    for v in topo:
        if v not in fixed:
            continue
        best_val, best_u = 1, None
        for u in pred[v]:
            base = fixed[u] if u in fixed else es[u]
            cand = base + 1
            if cand > best_val or (
                cand == best_val and (best_u is None or groups_tuple[u][0] < groups_tuple[best_u][0])
            ):
                best_val, best_u = cand, u
        need_from[v] = best_u
        fneed[v] = best_val
    for v in reversed(topo):
        if v not in fixed:
            continue
        best_val, best_w = n_periods, None
        for w in succ[v]:
            base = fixed[w] if w in fixed else ls[w]
            cand = base - 1
            if cand < best_val or (
                cand == best_val and (best_w is None or groups_tuple[w][0] < groups_tuple[best_w][0])
            ):
                best_val, best_w = cand, w
        allow_from[v] = best_w
        fallow[v] = best_val

    # 对外的“界”：锚定组恒为其固定期位；非锚定组取传播得到的需求/许可界
    low = {v: (fixed[v] if v in fixed else es[v]) for v in range(n)}
    high = {v: (fixed[v] if v in fixed else ls[v]) for v in range(n)}

    def _back_lower(v: int) -> List[int]:
        """下界需求链（起点->…->v 的前驱），链首为边界源组或锚定组。"""
        chain: List[int] = []
        cur: Optional[int] = need_from[v] if v in fixed else es_from[v]
        while cur is not None and cur not in fixed:
            chain.append(cur)
            cur = es_from[cur]
        if cur is not None:
            chain.append(cur)  # 锚定组作链首
        chain.reverse()
        return chain

    def _fwd_upper(v: int) -> List[int]:
        """上界许可链（v 的后继->…->终点），链尾为边界汇组或锚定组。"""
        chain: List[int] = []
        cur: Optional[int] = allow_from[v] if v in fixed else ls_from[v]
        while cur is not None and cur not in fixed:
            chain.append(cur)
            cur = ls_from[cur]
        if cur is not None:
            chain.append(cur)  # 锚定组作链尾
        return chain

    def _reason(chain: List[int]) -> dict:
        start, end = chain[0], chain[-1]
        start_anchor = start in fixed
        end_anchor = end in fixed
        start_period = fixed[start] if start_anchor else 1
        end_period = fixed[end] if end_anchor else n_periods
        required = len(chain)
        available = max(0, end_period - start_period + 1)
        group_chain_payload = [
            {
                "group": g + 1,
                "min_stratum": groups_tuple[g][0],
                "strata": list(groups_tuple[g]),
                "period": fixed[g] if g in fixed else None,
            }
            for g in chain
        ]
        involved_anchors = [
            {"stratum": anc.stratum, "period": anc.period, "group": g + 1, "min_stratum": groups_tuple[g][0]}
            for g in chain if g in fixed
            for anc in group_anchors[g]
        ]
        start_stratum = group_anchors[start][0].stratum if start_anchor else groups_tuple[start][0]
        end_stratum = group_anchors[end][0].stratum if end_anchor else groups_tuple[end][0]
        evidence_path = list(
            _chain_evidence_path(assertions, groups_tuple, chain, start_stratum, end_stratum)
        )
        if start_anchor:
            head = f"{group_anchors[start][0].stratum} 锚定第{start_period}期"
        else:
            head = "最早第 1 期"
        if end_anchor:
            tail = f"{group_anchors[end][0].stratum} 锚定第{end_period}期"
        else:
            tail = f"最迟第 {n_periods} 期"
        if start_anchor or end_anchor:
            return {
                "type": "anchor_precedence_conflict",
                "message": (
                    f"先后链与锚点不相容：{head} 与 {tail} 之间含 {required} 个严格递增的等价组，"
                    f"至少需占 {required} 期，但可用期位仅 {available} 个（第{start_period}～{end_period}期）"
                ),
                "group_chain": group_chain_payload,
                "start": (
                    {"kind": "anchor", "period": start_period, "stratum": group_anchors[start][0].stratum}
                    if start_anchor else {"kind": "boundary", "period": 1, "boundary": "first"}
                ),
                "end": (
                    {"kind": "anchor", "period": end_period, "stratum": group_anchors[end][0].stratum}
                    if end_anchor else {"kind": "boundary", "period": n_periods, "boundary": "last"}
                ),
                "required_periods": required,
                "available_periods": available,
                "involved_anchors": involved_anchors,
                "evidence_path": evidence_path,
            }
        return {
            "type": "chain_exceeds_periods",
            "message": (
                f"先后链与期数不相容：{required} 个等价组须严格递增，至少需占 {required} 期，"
                f"超过总期数 {n_periods}"
            ),
            "group_chain": group_chain_payload,
            "start": {"kind": "boundary", "period": 1, "boundary": "first"},
            "end": {"kind": "boundary", "period": n_periods, "boundary": "last"},
            "required_periods": required,
            "available_periods": n_periods,
            "involved_anchors": [],
            "evidence_path": evidence_path,
        }

    reasons: List[dict] = []
    seen_chain: Set[Tuple[int, ...]] = set()

    def _emit(chain: List[int]) -> None:
        key = tuple(chain)
        if key in seen_chain:
            return
        seen_chain.add(key)
        reasons.append(_reason(chain))

    for v in sorted(range(n), key=lambda i: groups_tuple[i][0]):
        if v in fixed:
            # 锚定组：下界需求 / 上界许可两侧各自独立成链，只报真正冲突的一侧
            if fixed[v] < fneed[v]:
                _emit(_back_lower(v) + [v])
            if fixed[v] > fallow[v]:
                _emit([v] + _fwd_upper(v))
        elif es[v] > ls[v]:
            # 非锚定组：完整“源/锚点 -> v -> 锚点/汇”链
            _emit(_back_lower(v) + [v] + _fwd_upper(v))

    if reasons:
        reasons.sort(key=lambda r: (r["type"], r["group_chain"][0]["min_stratum"], r["group_chain"][-1]["min_stratum"]))
        return ArrangementInfeasible(feasible=False, total_periods=n_periods, reasons=tuple(reasons))

    return ArrangementFeasible(
        feasible=True,
        total_periods=n_periods,
        groups=tuple(
            ArrangementGroup(
                group=i + 1,
                min_stratum=groups_tuple[i][0],
                strata=groups_tuple[i],
                earliest_period=low[i],
                latest_period=high[i],
                anchored=(i in fixed),
                anchors=tuple(sorted(group_anchors.get(i, []), key=lambda x: x.stratum)),
            )
            for i in range(n)
        ),
    )
