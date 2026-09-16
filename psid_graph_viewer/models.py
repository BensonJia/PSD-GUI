from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


Data = Mapping[str, Any]


@dataclass(frozen=True)
class Bus:
    number: int
    name: str
    bus_type: str
    available: bool
    raw: Data


@dataclass(frozen=True)
class Branch:
    name: str
    branch_type: str
    from_bus: int
    to_bus: int
    available: bool
    raw: Data


@dataclass(frozen=True)
class Injection:
    name: str
    injection_type: str
    dynamic_type: str
    bus: int
    available: bool
    raw: Data

    @property
    def is_dynamic(self) -> bool:
        return bool(self.dynamic_type)


@dataclass(frozen=True)
class EquationComponent:
    path: str
    role: str
    component_type: str
    states: tuple[str, ...]
    entrypoints: tuple[str, ...]
    source: str
    raw: Data


@dataclass(frozen=True)
class ExportedProject:
    root: Path
    manifest: Data
    graph: Data
    buses: tuple[Bus, ...]
    branches: tuple[Branch, ...]
    injections: tuple[Injection, ...]
    equations: tuple[EquationComponent, ...]
    system: Data | None
    custom_models: tuple[Any, ...] = ()
    editor_document: Any = None

    @property
    def bus_by_number(self) -> dict[int, Bus]:
        return {bus.number: bus for bus in self.buses}

    @property
    def graph_injector_by_name(self) -> dict[str, Data]:
        return {
            str(item.get("name", "")): item
            for item in self.graph.get("injectors", [])
        }

    @property
    def system_components(self) -> tuple[Data, ...]:
        if not self.system:
            return ()
        components = self.system.get("data", {}).get("components", [])
        return tuple(item for item in components if isinstance(item, dict))

    def runtime_component(self, name: str, component_type: str = "") -> Data | None:
        matches = [item for item in self.system_components if item.get("name") == name]
        if component_type:
            for item in matches:
                if item.get("__metadata__", {}).get("type") == component_type:
                    return item
        return matches[0] if matches else None

    def runtime_for_equation(self, path: str) -> Data | None:
        parts = path.split("/")
        if len(parts) < 2 or parts[0] != "injections":
            return None
        root = None
        for item in self.system_components:
            metadata_type = item.get("__metadata__", {}).get("type", "")
            if item.get("name") == parts[1] and metadata_type.startswith("Dynamic"):
                root = item
                break
        current: Any = root
        for key in parts[2:]:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return current if isinstance(current, dict) else None
