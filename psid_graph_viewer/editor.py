from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable


EDITOR_FILE = Path(".psid_gui/model.json")

INSTANCE_PARAMETER_DEFAULTS: dict[str, dict[str, Any]] = {
    "ACBus": {
        "base_voltage": 230.0,
        "angle": 0.0,
        "magnitude": 1.0,
        "voltage_limits": {"min": 0.9, "max": 1.1},
    },
    "Line": {
        "active_power_flow": 0.0,
        "reactive_power_flow": 0.0,
        "r": 0.0,
        "x": 0.1,
        "b": {"from": 0.0, "to": 0.0},
        "g": {"from": 0.0, "to": 0.0},
        "rating": 1.0,
        "angle_limits": {"min": -3.1415926536, "max": 3.1415926536},
    },
    "Transformer2W": {
        "base_power": 100.0,
        "active_power_flow": 0.0,
        "reactive_power_flow": 0.0,
        "r": 0.0,
        "x": 0.1,
        "primary_shunt": {"real": 0.0, "imag": 0.0},
        "rating": 1.0,
    },
    "StandardLoad": {
        "base_power": 100.0,
        "constant_active_power": 0.0,
        "constant_reactive_power": 0.0,
        "impedance_active_power": 0.0,
        "impedance_reactive_power": 0.0,
        "current_active_power": 0.0,
        "current_reactive_power": 0.0,
        "max_constant_active_power": 0.0,
        "max_constant_reactive_power": 0.0,
        "max_impedance_active_power": 0.0,
        "max_impedance_reactive_power": 0.0,
        "max_current_active_power": 0.0,
        "max_current_reactive_power": 0.0,
    },
    "Source": {
        "base_power": 100.0,
        "active_power": 0.0,
        "reactive_power": 0.0,
        "active_power_limits": {"min": 0.0, "max": 0.0},
        "reactive_power_limits": {"min": 0.0, "max": 0.0},
        "R_th": 0.0,
        "X_th": 0.0,
        "internal_voltage": 1.0,
        "internal_angle": 0.0,
    },
    "DynamicGenerator": {
        "base_power": 100.0,
        "active_power": 0.5,
        "reactive_power": 0.0,
        "rating": 1.0,
        "ω_ref": 1.0,
        "machine": {
            "R": 0.0, "Xd": 0.8979, "Xq": 0.646, "Xd_p": 0.2995,
            "Xq_p": 0.646, "Xd_pp": 0.23, "Xq_pp": 0.4,
            "Td0_p": 3.0, "Tq0_p": 0.1, "Td0_pp": 0.01, "Tq0_pp": 0.033,
        },
        "shaft": {"H": 3.01, "D": 0.0},
        "avr": {
            "K0": 200.0, "T1": 4.0, "T2": 1.0, "T3": 0.006,
            "T4": 0.06, "Te": 0.0001, "Tr": 0.0001,
            "Va_lim": {"min": -50.0, "max": 50.0},
            "Ae": 0.0, "Be": 0.0, "V_ref": 1.0,
        },
        "prime_mover": {
            "R": 0.02, "Ts": 0.1, "Tc": 0.45, "T3": 0.0,
            "T4": 12.0, "T5": 50.0,
            "valve_position_limits": {"min": 0.0, "max": 1.2},
            "P_ref": 0.5,
        },
        "pss": {"V_pss": 0.0},
    },
    "DynamicInverter": {
        "base_power": 100.0,
        "active_power": 0.5,
        "reactive_power": 0.0,
        "rating": 1.0,
        "ω_ref": 1.0,
        "converter": {"rated_voltage": 690.0, "rated_current": 2.75},
        "active_power_control": {"Rp": 0.05, "ωz": 31.4159265359, "P_ref": 0.5},
        "reactive_power_control": {"kq": 0.2, "ωf": 1000.0, "V_ref": 1.0},
        "inner_control": {
            "kpv": 0.59, "kiv": 736.0, "kffv": 0.0, "rv": 0.0,
            "lv": 0.2, "kpc": 1.27, "kic": 14.3, "kffi": 0.0,
            "ωad": 50.0, "kad": 0.0,
        },
        "dc_source": {"voltage": 600.0},
        "frequency_estimator": {"frequency": 1.0},
        "filter": {"lf": 0.08, "rf": 0.003, "cf": 0.074, "lg": 0.2, "rg": 0.01},
    },
}


def default_instance_parameters(model_ref: str) -> dict[str, Any]:
    return json.loads(json.dumps(INSTANCE_PARAMETER_DEFAULTS.get(model_ref, {})))


@dataclass(frozen=True)
class PortDefinition:
    port_id: str
    name: str
    domain: str = "AC_BUS"
    role: str = "bidirectional"
    required: bool = True
    side: str = "auto"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PortDefinition":
        return cls(
            port_id=str(value.get("id", "")),
            name=str(value.get("name", "")),
            domain=str(value.get("domain", "AC_BUS")),
            role=str(value.get("role", "bidirectional")),
            required=bool(value.get("required", True)),
            side=str(value.get("side", "auto")),
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["id"] = value.pop("port_id")
        return value


@dataclass(frozen=True)
class EditorInstance:
    instance_id: str
    name: str
    kind: str
    model_ref: str
    ports: tuple[PortDefinition, ...]
    position: tuple[float, float]
    parameters: dict[str, Any]
    bus_number: int | None = None
    backend_supported: bool = True

    @property
    def key(self) -> str:
        return f"editor:{self.instance_id}"

    @classmethod
    def new(
        cls,
        name: str,
        kind: str,
        model_ref: str,
        ports: Iterable[PortDefinition],
        position: tuple[float, float],
        parameters: dict[str, Any] | None = None,
        bus_number: int | None = None,
        backend_supported: bool = True,
    ) -> "EditorInstance":
        return cls(
            str(uuid.uuid4()),
            name,
            kind,
            model_ref,
            tuple(ports),
            position,
            dict(parameters or {}),
            bus_number,
            backend_supported,
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EditorInstance":
        position = value.get("position", [0.0, 0.0])
        return cls(
            instance_id=str(value.get("id", "")),
            name=str(value.get("name", "")),
            kind=str(value.get("kind", "")),
            model_ref=str(value.get("model_ref", "")),
            ports=tuple(PortDefinition.from_dict(item) for item in value.get("ports", [])),
            position=(float(position[0]), float(position[1])),
            parameters=dict(value.get("parameters", {})),
            bus_number=int(value["bus_number"]) if value.get("bus_number") is not None else None,
            backend_supported=bool(value.get("backend_supported", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.instance_id,
            "name": self.name,
            "kind": self.kind,
            "model_ref": self.model_ref,
            "ports": [port.to_dict() for port in self.ports],
            "position": list(self.position),
            "parameters": self.parameters,
            "bus_number": self.bus_number,
            "backend_supported": self.backend_supported,
        }


@dataclass(frozen=True)
class EditorConnection:
    connection_id: str
    first_key: str
    first_port: str
    second_key: str
    second_port: str

    @classmethod
    def new(
        cls, first_key: str, first_port: str, second_key: str, second_port: str
    ) -> "EditorConnection":
        return cls(str(uuid.uuid4()), first_key, first_port, second_key, second_port)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EditorConnection":
        return cls(
            str(value.get("id", "")),
            str(value.get("first_key", "")),
            str(value.get("first_port", "")),
            str(value.get("second_key", "")),
            str(value.get("second_port", "")),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.connection_id,
            "first_key": self.first_key,
            "first_port": self.first_port,
            "second_key": self.second_key,
            "second_port": self.second_port,
        }

    def touches(self, key: str) -> bool:
        return key in {self.first_key, self.second_key}


@dataclass(frozen=True)
class SlotOverride:
    component_path: str
    model_ref: str
    model_name: str
    base_category: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SlotOverride":
        return cls(
            str(value.get("component_path", "")),
            str(value.get("model_ref", "")),
            str(value.get("model_name", "")),
            str(value.get("base_category", "")),
        )


@dataclass(frozen=True)
class ValidationIssue:
    severity: str
    message: str
    key: str = ""


@dataclass(frozen=True)
class EditorDocument:
    instances: tuple[EditorInstance, ...] = ()
    connections: tuple[EditorConnection, ...] = ()
    deleted_keys: tuple[str, ...] = ()
    slot_overrides: tuple[SlotOverride, ...] = ()
    geometry: dict[str, tuple[float, float, float, float, int, bool, bool]] = field(default_factory=dict)
    port_positions: dict[str, tuple[float, float]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EditorDocument":
        return cls(
            instances=tuple(EditorInstance.from_dict(item) for item in value.get("instances", [])),
            connections=tuple(EditorConnection.from_dict(item) for item in value.get("connections", [])),
            deleted_keys=tuple(str(item) for item in value.get("deleted_keys", [])),
            slot_overrides=tuple(SlotOverride.from_dict(item) for item in value.get("slot_overrides", [])),
            geometry={
                str(key): (
                    float(item[0]), float(item[1]), float(item[2]), float(item[3]),
                    int(item[4]), bool(item[5]), bool(item[6]),
                )
                for key, item in value.get("geometry", {}).items()
            },
            port_positions={
                str(key): (float(item[0]), float(item[1]))
                for key, item in value.get("port_positions", {}).items()
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "instances": [item.to_dict() for item in self.instances],
            "connections": [item.to_dict() for item in self.connections],
            "deleted_keys": list(self.deleted_keys),
            "slot_overrides": [asdict(item) for item in self.slot_overrides],
            "geometry": {key: list(item) for key, item in self.geometry.items()},
            "port_positions": {
                key: list(item) for key, item in self.port_positions.items()
            },
        }

    @property
    def instance_by_key(self) -> dict[str, EditorInstance]:
        return {item.key: item for item in self.instances}

    def with_instance(self, instance: EditorInstance) -> "EditorDocument":
        return replace(self, instances=(*self.instances, instance))

    def replace_instance(self, instance: EditorInstance) -> "EditorDocument":
        return replace(
            self,
            instances=tuple(instance if item.instance_id == instance.instance_id else item for item in self.instances),
        )

    def without_key(self, key: str, baseline: bool = False) -> "EditorDocument":
        return replace(
            self,
            instances=tuple(item for item in self.instances if item.key != key),
            connections=tuple(item for item in self.connections if not item.touches(key)),
            deleted_keys=(
                tuple(dict.fromkeys((*self.deleted_keys, key)))
                if baseline
                else self.deleted_keys
            ),
            geometry={name: value for name, value in self.geometry.items() if name != key},
            port_positions={
                name: value
                for name, value in self.port_positions.items()
                if not name.startswith(f"{key}|")
            },
        )

    def with_port_position(
        self, key: str, port_name: str, position: tuple[float, float]
    ) -> "EditorDocument":
        positions = dict(self.port_positions)
        positions[f"{key}|{port_name}"] = (
            max(0.0, min(1.0, float(position[0]))),
            max(0.0, min(1.0, float(position[1]))),
        )
        return replace(self, port_positions=positions)

    def with_connection(self, connection: EditorConnection) -> "EditorDocument":
        duplicate = any(
            {item.first_key, item.second_key} == {connection.first_key, connection.second_key}
            and {item.first_port, item.second_port} == {connection.first_port, connection.second_port}
            for item in self.connections
        )
        return self if duplicate else replace(self, connections=(*self.connections, connection))

    def without_connection(self, first_key: str, first_port: str, second_key: str, second_port: str) -> "EditorDocument":
        wanted = {(first_key, first_port), (second_key, second_port)}
        return replace(
            self,
            connections=tuple(
                item
                for item in self.connections
                if {(item.first_key, item.first_port), (item.second_key, item.second_port)} != wanted
            ),
        )

    def with_slot_override(self, override: SlotOverride) -> "EditorDocument":
        return replace(
            self,
            slot_overrides=tuple(
                item for item in self.slot_overrides if item.component_path != override.component_path
            ) + (override,),
        )

    def fingerprint(self) -> str:
        structural = self.to_dict()
        structural.pop("geometry", None)
        structural.pop("port_positions", None)
        for instance in structural["instances"]:
            instance.pop("position", None)
            # Parameters are runtime values.  The selected whole-device model
            # is structural, but changing its values does not invalidate the
            # compiled graph.
            instance["parameters"] = {}
        canonical = json.dumps(structural, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def validate(self, project: Any) -> tuple[ValidationIssue, ...]:
        issues: list[ValidationIssue] = []
        active_base_keys = {
            *(f"bus:{item.number}" for item in project.buses),
            *(f"branch:{item.name}" for item in project.branches),
            *(f"injection:{item.name}" for item in project.injections),
        } - set(self.deleted_keys)
        known = active_base_keys | set(self.instance_by_key)
        bus_keys = {
            *(f"bus:{item.number}" for item in project.buses),
            *(item.key for item in self.instances if item.kind == "bus"),
        } - set(self.deleted_keys)
        names = [item.name.casefold() for item in self.instances]
        if len(names) != len(set(names)):
            issues.append(ValidationIssue("error", "新增器件名称不能重复"))
        base_numbers = {item.number for item in project.buses if f"bus:{item.number}" in active_base_keys}
        added_numbers = [item.bus_number for item in self.instances if item.kind == "bus"]
        if any(number is None or number in base_numbers for number in added_numbers) or len(added_numbers) != len(set(added_numbers)):
            issues.append(ValidationIssue("error", "新增 Bus 编号缺失或重复"))
        usage: dict[tuple[str, str], int] = {}
        for connection in self.connections:
            if connection.first_key not in known or connection.second_key not in known:
                issues.append(ValidationIssue("error", "连接引用了不存在的器件", connection.connection_id))
                continue
            endpoints = {connection.first_key, connection.second_key}
            connected_buses = endpoints & bus_keys
            if len(connected_buses) != 1:
                issues.append(ValidationIssue("error", "拓扑连接必须恰好连接一个 Bus", connection.connection_id))
                continue
            other_key = next(iter(endpoints - connected_buses))
            instance = self.instance_by_key.get(other_key)
            if not instance:
                issues.append(ValidationIssue("error", "只能为新增器件创建可编辑连接", connection.connection_id))
                continue
            other_port = connection.first_port if connection.first_key == other_key else connection.second_port
            definition = next((port for port in instance.ports if port.port_id == other_port), None)
            if not definition:
                issues.append(ValidationIssue("error", f"{instance.name} 引用了不存在的端口 {other_port}", connection.connection_id))
                continue
            if definition.domain != "AC_BUS":
                issues.append(ValidationIssue("error", f"{instance.name}.{definition.name} 不是 AC Bus 端口", connection.connection_id))
            for endpoint in ((connection.first_key, connection.first_port), (connection.second_key, connection.second_port)):
                usage[endpoint] = usage.get(endpoint, 0) + 1
        for instance in self.instances:
            if not instance.backend_supported:
                issues.append(ValidationIssue("error", f"{instance.name} 的端口合同尚不受 Julia 后端支持", instance.key))
            for port in instance.ports:
                count = usage.get((instance.key, port.port_id), 0)
                if port.required and count == 0:
                    issues.append(ValidationIssue("error", f"{instance.name}.{port.name} 尚未连接", instance.key))
                if instance.kind != "bus" and count > 1:
                    issues.append(ValidationIssue("error", f"{instance.name}.{port.name} 只能连接一次", instance.key))
        return tuple(issues)


class EditorDocumentStore:
    @staticmethod
    def path(root: Path) -> Path:
        return root / EDITOR_FILE

    @classmethod
    def load(cls, root: Path) -> EditorDocument:
        path = cls.path(root)
        if not path.is_file():
            return EditorDocument()
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"{EDITOR_FILE}：{error}") from error
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError(f"{EDITOR_FILE}：仅支持 schema_version 1")
        document = EditorDocument.from_dict(value)
        ids = [item.instance_id for item in document.instances]
        if not all(ids) or len(ids) != len(set(ids)):
            raise ValueError(f"{EDITOR_FILE}：实例 id 缺失或重复")
        return document

    @classmethod
    def save(cls, root: Path, document: EditorDocument) -> None:
        path = cls.path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(document.to_dict(), stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.replace(temporary, path)
        except Exception:
            Path(temporary).unlink(missing_ok=True)
            raise


def upgrade_editor_instances(document: EditorDocument) -> tuple[EditorDocument, int]:
    """Upgrade early editor instances to library-backed device definitions."""
    changed = 0
    instances = []
    for instance in document.instances:
        if instance.model_ref not in INSTANCE_PARAMETER_DEFAULTS:
            instances.append(instance)
            continue
        old_parameters = dict(instance.parameters)
        old_parameters.pop("template", None)
        parameters = {
            **default_instance_parameters(instance.model_ref),
            **old_parameters,
        }
        if parameters != instance.parameters or not instance.backend_supported:
            instance = replace(instance, parameters=parameters, backend_supported=True)
            changed += 1
        instances.append(instance)
    return replace(document, instances=tuple(instances)), changed


def default_ports(kind: str) -> tuple[PortDefinition, ...]:
    if kind == "bus":
        return (PortDefinition("bus", "电气连接", role="junction", required=False),)
    if kind in {"line", "transformer", "dynamic_branch"}:
        return (
            PortDefinition("from", "起点", role="bidirectional"),
            PortDefinition("to", "终点", role="bidirectional"),
        )
    role = "sink" if kind == "load" else "source" if kind in {"generator", "inverter", "source"} else "bidirectional"
    return (PortDefinition("terminal", "电气端口", role=role),)
