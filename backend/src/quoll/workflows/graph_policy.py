"""Проходимость живого графа. Чистые правила, в базу не ходят.

после правки опубликованного воркфлоу: ровно одна начальная стадия и из всего,
куда заявка может попасть - от начальной или с занятых стадий, - достижима
терминальная. Недостижимая пустая стадия разрешена: админ собирает новый шаг.
Публикация строже - достижимо должно быть всё, кроме терминальных: в «Отказ»
попадают досрочным закрытием. Отказ в аппруве - тоже путь (3 -> 3.1),
вызывающий передаёт его как ребро
"""

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class StageFacts:
    id: int
    is_terminal: bool
    archived: bool
    branch: bool = False
    branch_start: bool = False
    # доп. шаг (4.1): стоит в маршруте линейно, но обычный ход его не занимает
    side: bool = False
    handler: str | None = None
    parent: int | None = None


@dataclass(frozen=True)
class EdgeFacts:
    from_stage_id: int | None
    to_stage_id: int
    irreversible: bool = False
    backward: bool = False


def edge_facts(edges) -> list[EdgeFacts]:
    """активные рёбра графа плюс пути отказа в аппруве (3 -> 3.1): заявка
    попадает туда без ребра, но это законный путь"""
    facts = [
        EdgeFacts(e.from_stage_id, e.to_stage_id, e.is_irreversible, e.is_backward)
        for e in edges
    ]
    facts += [
        EdgeFacts(e.from_stage_id, e.reject_to_stage_id)
        for e in edges
        if e.reject_to_stage_id is not None
    ]
    return facts


def _reachable(starts: Iterable[int], forward: dict[int, set[int]]) -> set[int]:
    seen: set[int] = set()
    todo = list(starts)
    while todo:
        stage = todo.pop()
        if stage in seen:
            continue
        seen.add(stage)
        todo.extend(forward[stage] - seen)
    return seen


def graph_problems(
    stages: list[StageFacts],
    edges: list[EdgeFacts],
    occupied: set[int],
    *,
    full: bool,
) -> list[str]:
    """что не так с графом; пусто - граф проходим. edges - только активные"""
    live = {s.id: s for s in stages if not s.archived}
    problems = []

    handlers = Counter(s.handler for s in live.values() if s.handler)
    problems += [
        f"expected one stage with handler {h}" for h, n in handlers.items() if n > 1
    ]

    dangling = [
        e
        for e in edges
        if e.to_stage_id not in live
        or (e.from_stage_id is not None and e.from_stage_id not in live)
    ]
    if dangling:
        problems.append("active transitions touch archived or foreign stages")

    starts = [
        e.to_stage_id
        for e in edges
        if e.from_stage_id is None and e.to_stage_id in live
    ]
    if len(starts) != 1:
        problems.append(f"expected exactly one start stage, found {len(starts)}")
    # иначе черновик первым же переходом стал бы закрытой заявкой без владельца
    if any(live[s].is_terminal for s in starts):
        problems.append("start stage cannot be terminal")
    if any(live[s].branch for s in starts):
        problems.append("start stage cannot be a branch stage")
    if any(live[s].side for s in starts):
        problems.append("start stage cannot be a side stage")

    # ветки продуктов - свой подграф: свой вход, рёбра границу не пересекают
    branch_starts = [s.id for s in live.values() if s.branch_start]
    if any(s.branch for s in live.values()) and len(branch_starts) != 1:
        problems.append(
            f"expected exactly one branch start stage, found {len(branch_starts)}"
        )
    if any(live[s].is_terminal for s in branch_starts):
        problems.append("branch start stage cannot be terminal")
    # уровни соединяет только точка невозврата: из шага заявки в начало
    # веток (4 -> 5). Заявка по ней не едет, ветки встают на начало
    crossing = [
        e
        for e in edges
        if e.from_stage_id in live
        and e.to_stage_id in live
        and live[e.from_stage_id].branch != live[e.to_stage_id].branch
        and not (
            e.irreversible
            and not live[e.from_stage_id].branch
            and live[e.to_stage_id].branch_start
        )
    ]
    if crossing:
        problems.append("transitions cross between contract and branch stages")
    irreversible = [e for e in edges if e.irreversible]
    if branch_starts and any(e.to_stage_id not in branch_starts for e in irreversible):
        problems.append("point of no return leads to the branch start stage")
    if full and branch_starts and not irreversible:
        problems.append("branches need a point of no return into their start")
    starts = [*starts, *branch_starts]

    # доп. стадии: свой выход в основной маршрут, не в терминальную, и если
    # ведут к точке невозврата - у исходной доп. стадии должен быть живой
    # родитель уровня заявки, иначе основному указателю некуда вернуться (Д39)
    side = {i for i, s in live.items() if s.side}
    main = set(live) - side
    forward_direct: dict[int, set[int]] = defaultdict(set)
    for e in edges:
        if e.from_stage_id in live and e.to_stage_id in live and not e.backward:
            forward_direct[e.from_stage_id].add(e.to_stage_id)
    for i in side:
        if not (_reachable([i], forward_direct) & main):
            problems.append(f"side stage {i} has no way back to the main route")
    for e in edges:
        if e.from_stage_id in side:
            if e.to_stage_id in live and live[e.to_stage_id].is_terminal:
                problems.append("side stage cannot lead to a terminal stage")
            if e.irreversible:
                parent = live[e.from_stage_id].parent
                if (
                    parent is None
                    or parent not in live
                    or live[parent].side
                    or live[parent].is_terminal
                    or live[parent].branch
                ):
                    problems.append(
                        "side stage leading to the point of no return needs a "
                        "live main parent"
                    )

    forward: dict[int, set[int]] = defaultdict(set)
    backward: dict[int, set[int]] = defaultdict(set)
    for e in edges:
        if e.from_stage_id in live and e.to_stage_id in live:
            forward[e.from_stage_id].add(e.to_stage_id)
            backward[e.to_stage_id].add(e.from_stage_id)

    # куда заявка может попасть: от начальной и с уже занятых стадий
    route = _reachable([*starts, *(s for s in occupied if s in live)], forward)
    finishing = _reachable([s.id for s in live.values() if s.is_terminal], backward)
    dead_ends = sorted(route - finishing)
    if dead_ends:
        problems.append(f"no way to a terminal stage from stages {dead_ends}")

    stranded = sorted(s for s in occupied if s not in live)
    if stranded:
        problems.append(f"interactions stand on archived stages {stranded}")

    if full:
        # в «Отказ» попадают досрочным закрытием, без ребра
        unreachable = sorted(
            s
            for s in set(live) - _reachable(starts, forward)
            if not live[s].is_terminal
        )
        if unreachable:
            problems.append(f"stages {unreachable} are unreachable from the start")
    return problems


def reachable_from_start(edges: list[EdgeFacts]) -> set[int]:
    """куда можно попасть от начальной по активным рёбрам"""
    forward: dict[int, set[int]] = defaultdict(set)
    for e in edges:
        if e.from_stage_id is not None:
            forward[e.from_stage_id].add(e.to_stage_id)
    return _reachable(
        [e.to_stage_id for e in edges if e.from_stage_id is None], forward
    )


def leads_to(edges: list[EdgeFacts], source: int, dest: int) -> bool:
    """из source можно дойти до dest по данным рёбрам"""
    forward: dict[int, set[int]] = defaultdict(set)
    for e in edges:
        if e.from_stage_id is not None:
            forward[e.from_stage_id].add(e.to_stage_id)
    return dest in _reachable([source], forward)
