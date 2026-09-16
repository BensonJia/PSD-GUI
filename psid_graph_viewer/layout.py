from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass

from .models import ExportedProject


@dataclass(frozen=True)
class Point:
    x: float
    y: float


def topology_positions(project: ExportedProject) -> dict[str, Point]:
    adjacency: dict[int, set[int]] = {bus.number: set() for bus in project.buses}
    for branch in project.branches:
        adjacency[branch.from_bus].add(branch.to_bus)
        adjacency[branch.to_bus].add(branch.from_bus)

    bus_by_number = project.bus_by_number
    roots = sorted(
        adjacency,
        key=lambda number: (bus_by_number[number].bus_type.upper() != "REF", number),
    )
    levels: dict[int, tuple[int, int]] = {}
    component = 0
    for root in roots:
        if root in levels:
            continue
        queue = deque([(root, 0)])
        levels[root] = (component, 0)
        while queue:
            number, depth = queue.popleft()
            for neighbor in sorted(adjacency[number]):
                if neighbor not in levels:
                    levels[neighbor] = (component, depth + 1)
                    queue.append((neighbor, depth + 1))
        component += 1

    grouped: dict[tuple[int, int], list[int]] = defaultdict(list)
    for number, group_depth in levels.items():
        grouped[group_depth].append(number)
    positions: dict[str, Point] = {}
    for (group, depth), numbers in sorted(grouped.items()):
        for row, number in enumerate(sorted(numbers)):
            positions[f"bus:{number}"] = Point(group * 1800.0 + depth * 420.0, row * 300.0)

    parallel: dict[tuple[int, int], list[str]] = defaultdict(list)
    for branch in project.branches:
        parallel[tuple(sorted((branch.from_bus, branch.to_bus)))].append(branch.name)
    for names in parallel.values():
        names.sort()
    for branch in project.branches:
        start = positions[f"bus:{branch.from_bus}"]
        end = positions[f"bus:{branch.to_bus}"]
        names = parallel[tuple(sorted((branch.from_bus, branch.to_bus)))]
        offset = (names.index(branch.name) - (len(names) - 1) / 2.0) * 90.0
        positions[f"branch:{branch.name}"] = Point(
            (start.x + end.x) / 2.0,
            (start.y + end.y) / 2.0 + offset,
        )

    devices: dict[tuple[int, bool], list[str]] = defaultdict(list)
    for injection in project.injections:
        is_load = "load" in injection.injection_type.lower()
        devices[(injection.bus, is_load)].append(injection.name)
    for names in devices.values():
        names.sort()
    for injection in project.injections:
        bus = positions[f"bus:{injection.bus}"]
        is_load = "load" in injection.injection_type.lower()
        names = devices[(injection.bus, is_load)]
        offset = (names.index(injection.name) - (len(names) - 1) / 2.0) * 230.0
        positions[f"injection:{injection.name}"] = Point(
            bus.x + offset,
            bus.y + (250.0 if is_load else -250.0),
        )
    return positions


def device_positions(paths: list[str]) -> dict[str, Point]:
    by_depth: dict[int, list[str]] = defaultdict(list)
    for path in sorted(paths):
        by_depth[path.count("/")].append(path)
    positions: dict[str, Point] = {}
    min_depth = min(by_depth, default=0)
    for depth, items in sorted(by_depth.items()):
        for row, path in enumerate(items):
            positions[path] = Point((depth - min_depth) * 430.0, row * 220.0)
    return positions
