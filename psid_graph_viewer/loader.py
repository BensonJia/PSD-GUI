from __future__ import annotations

import json
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any

from .models import Branch, Bus, EquationComponent, ExportedProject, Injection
from .custom_models import CustomModelError, CustomModelStore, apply_custom_models
from .editor import EditorDocumentStore


class ProjectLoadError(ValueError):
    """An exported project cannot be read or does not satisfy schema version 1."""


class ProjectLoader:
    REQUIRED_FILES = (
        "manifest.toml",
        "topology.toml",
        "graph.toml",
        "equations/index.toml",
    )

    @classmethod
    def load(cls, directory: str | Path) -> ExportedProject:
        root = Path(directory).expanduser().resolve()
        if not root.is_dir():
            raise ProjectLoadError(f"项目目录不存在：{root}")
        for relative in cls.REQUIRED_FILES:
            if not (root / relative).is_file():
                raise ProjectLoadError(f"缺少必需文件：{relative}")

        manifest = cls._read_toml(root, "manifest.toml")
        topology = cls._read_toml(root, "topology.toml")
        graph = cls._read_toml(root, "graph.toml")
        equation_data = cls._read_toml(root, "equations/index.toml")
        if manifest.get("schema_version") != 1:
            raise ProjectLoadError(
                f"manifest.toml：不支持 schema_version={manifest.get('schema_version')!r}，仅支持 1"
            )

        buses = tuple(cls._bus(item, index) for index, item in enumerate(cls._items(topology, "buses", "topology.toml")))
        branches = tuple(cls._branch(item, index) for index, item in enumerate(cls._items(topology, "branches", "topology.toml")))
        injections = tuple(cls._injection(item, index) for index, item in enumerate(cls._items(topology, "injections", "topology.toml")))
        equations = tuple(
            cls._equation(item, index)
            for index, item in enumerate(cls._items(equation_data, "components", "equations/index.toml"))
        )
        cls._validate_topology(buses, branches, injections)
        cls._validate_graph(graph, {bus.number for bus in buses})

        system_path = root / "system.json"
        system = cls._read_json(system_path) if system_path.is_file() else None
        project = ExportedProject(
            root=root,
            manifest=manifest,
            graph=graph,
            buses=buses,
            branches=branches,
            injections=injections,
            equations=equations,
            system=system,
        )
        try:
            models = CustomModelStore.load(root, injections)
        except CustomModelError as error:
            raise ProjectLoadError(str(error)) from error
        try:
            editor_document = EditorDocumentStore.load(root)
        except ValueError as error:
            raise ProjectLoadError(str(error)) from error
        return replace(
            apply_custom_models(project, models),
            editor_document=editor_document,
        )

    @staticmethod
    def _read_toml(root: Path, relative: str) -> dict[str, Any]:
        path = root / relative
        try:
            with path.open("rb") as stream:
                return tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise ProjectLoadError(f"{relative}：{error}") from error

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            with path.open(encoding="utf-8") as stream:
                value = json.load(stream)
        except (OSError, json.JSONDecodeError) as error:
            raise ProjectLoadError(f"system.json：{error}") from error
        if not isinstance(value, dict):
            raise ProjectLoadError("system.json：根对象必须是 JSON object")
        return value

    @staticmethod
    def _items(data: dict[str, Any], key: str, filename: str) -> list[dict[str, Any]]:
        value = data.get(key, [])
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise ProjectLoadError(f"{filename}：{key} 必须是对象数组")
        return value

    @staticmethod
    def _required(item: dict[str, Any], key: str, location: str) -> Any:
        if key not in item:
            raise ProjectLoadError(f"{location}：缺少字段 {key}")
        return item[key]

    @classmethod
    def _bus(cls, item: dict[str, Any], index: int) -> Bus:
        location = f"topology.toml buses[{index}]"
        try:
            return Bus(
                number=int(cls._required(item, "number", location)),
                name=str(cls._required(item, "name", location)),
                bus_type=str(cls._required(item, "bus_type", location)),
                available=bool(item.get("available", item.get("status", True))),
                raw=item,
            )
        except (TypeError, ValueError) as error:
            raise ProjectLoadError(f"{location}：字段类型无效") from error

    @classmethod
    def _branch(cls, item: dict[str, Any], index: int) -> Branch:
        location = f"topology.toml branches[{index}]"
        try:
            return Branch(
                name=str(cls._required(item, "name", location)),
                branch_type=str(cls._required(item, "type", location)),
                from_bus=int(cls._required(item, "from", location)),
                to_bus=int(cls._required(item, "to", location)),
                available=bool(item.get("available", item.get("status", True))),
                raw=item,
            )
        except (TypeError, ValueError) as error:
            raise ProjectLoadError(f"{location}：字段类型无效") from error

    @classmethod
    def _injection(cls, item: dict[str, Any], index: int) -> Injection:
        location = f"topology.toml injections[{index}]"
        try:
            return Injection(
                name=str(cls._required(item, "name", location)),
                injection_type=str(cls._required(item, "type", location)),
                dynamic_type=str(item.get("dynamic_type", "")),
                bus=int(cls._required(item, "bus", location)),
                available=bool(item.get("available", item.get("status", True))),
                raw=item,
            )
        except (TypeError, ValueError) as error:
            raise ProjectLoadError(f"{location}：字段类型无效") from error

    @classmethod
    def _equation(cls, item: dict[str, Any], index: int) -> EquationComponent:
        location = f"equations/index.toml components[{index}]"
        return EquationComponent(
            path=str(cls._required(item, "path", location)),
            role=str(item.get("role", "")),
            component_type=str(cls._required(item, "type", location)),
            states=tuple(str(value) for value in item.get("states", [])),
            entrypoints=tuple(str(value) for value in item.get("entrypoints", [])),
            source=str(item.get("source", "")),
            raw=item,
        )

    @staticmethod
    def _validate_topology(
        buses: tuple[Bus, ...],
        branches: tuple[Branch, ...],
        injections: tuple[Injection, ...],
    ) -> None:
        numbers = [bus.number for bus in buses]
        if len(numbers) != len(set(numbers)):
            raise ProjectLoadError("topology.toml：母线编号必须唯一")
        known = set(numbers)
        for branch in branches:
            missing = {branch.from_bus, branch.to_bus} - known
            if missing:
                raise ProjectLoadError(
                    f"topology.toml：支路 {branch.name!r} 引用了不存在的母线 {sorted(missing)}"
                )
        for injection in injections:
            if injection.bus not in known:
                raise ProjectLoadError(
                    f"topology.toml：注入设备 {injection.name!r} 引用了不存在的母线 {injection.bus}"
                )

    @staticmethod
    def _validate_graph(graph: dict[str, Any], known_buses: set[int]) -> None:
        if not isinstance(graph.get("variable_count"), int):
            raise ProjectLoadError("graph.toml：variable_count 必须是整数")
        for index, injector in enumerate(graph.get("injectors", [])):
            if not isinstance(injector, dict):
                raise ProjectLoadError("graph.toml：injectors 必须是对象数组")
            if injector.get("bus") not in known_buses:
                raise ProjectLoadError(
                    f"graph.toml injectors[{index}]：引用了不存在的母线 {injector.get('bus')!r}"
                )
