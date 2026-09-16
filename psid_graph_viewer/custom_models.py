from __future__ import annotations

import ast
import hashlib
import html
import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

from Qt import QtCore, QtGui, QtWidgets

from .models import EquationComponent, ExportedProject, Injection
from .model_library import ModelLibrary
from .editor import (
    PortDefinition,
    default_instance_parameters,
)


MODEL_FILE = Path(".psid_gui/custom_models.json")
SYSTEM_INPUTS = (
    "t",
    "v_r",
    "v_i",
    "omega_sys",
    "P_ref",
    "Q_ref",
    "V_ref",
    "omega_ref",
    "system_base_power",
    "device_base_power",
)
OUTPUT_NAMES = ("i_r", "i_i")
ALLOWED_FUNCTIONS = {
    "sin",
    "cos",
    "tan",
    "asin",
    "acos",
    "atan",
    "exp",
    "log",
    "sqrt",
    "abs",
    "min",
    "max",
    "clamp",
}
CONSTANTS = {"pi", "e"}

COMPONENT_INPUTS = {
    "励磁系统": ("V_ref", "V_pss", "V_tR", "V_tI"),
    "同步机": ("delta", "omega", "Vf", "V_tR", "V_tI"),
    "电力系统稳定器": ("omega", "tau_e", "V_tR"),
    "轴系": ("tau_e", "tau_m", "omega_sys"),
    "调速器": ("omega", "P_ref", "omega_ref"),
    "变流器": ("md", "mq", "Vdc", "V_tR", "V_tI", "omega_sys"),
    "直流侧": ("P_ref", "Vdc"),
    "滤波器": ("V_tR", "V_tI", "V_cnvR", "V_cnvI", "theta", "omega_sys"),
    "频率估计器": ("v_filter_r", "v_filter_i", "theta", "omega_sys"),
    "外环控制器": ("P_ref", "Q_ref", "V_ref", "omega_ref", "omega_sys"),
    "内环控制器": ("i_filter_r", "i_filter_i", "v_filter_r", "v_filter_i"),
    "输出电流限幅器": ("Id_ref", "Iq_ref", "V_t"),
    "聚合动态设备": SYSTEM_INPUTS,
    "动态支路": ("from_v_r", "from_v_i", "to_v_r", "to_v_i", "omega_sys"),
    "电源": ("v_r", "v_i", "V_ref", "theta_ref"),
    "负荷": ("v_r", "v_i", "P_ref", "Q_ref"),
}

COMPONENT_OUTPUTS = {
    "励磁系统": ("Vf",),
    "同步机": ("i_r", "i_i", "tau_e"),
    "电力系统稳定器": ("V_pss",),
    "轴系": ("delta", "omega"),
    "调速器": ("tau_m",),
    "变流器": ("V_cnvR", "V_cnvI"),
    "直流侧": ("Vdc",),
    "滤波器": ("v_filter_r", "v_filter_i", "i_r", "i_i"),
    "频率估计器": ("theta", "omega"),
    "外环控制器": ("Id_ref", "Iq_ref", "V_oc", "omega_oc"),
    "内环控制器": ("md", "mq"),
    "输出电流限幅器": ("Id_ref", "Iq_ref"),
    "聚合动态设备": OUTPUT_NAMES,
    "动态支路": ("from_i_r", "from_i_i", "to_i_r", "to_i_i"),
    "电源": OUTPUT_NAMES,
    "负荷": OUTPUT_NAMES,
}

JULIA_FORBIDDEN = re.compile(
    r"(?:`|@|\b(?:baremodule|module|using|import|export|include|eval|ccall|run|pipeline|"
    r"open|read|write|download|unsafe_[A-Za-z_]*|global|const)\b)"
)
JULIA_PROPERTY = re.compile(r"\b(dx|y|x|u|p)\.([A-Za-z_][A-Za-z0-9_]*)")
JULIA_HELPER = re.compile(r"\bfunction\s+([A-Za-z_][A-Za-z0-9_!]*)\s*\(")


class CustomModelError(ValueError):
    pass


@dataclass(frozen=True)
class CustomState:
    name: str
    rhs: str
    mass: float = 1.0
    initial: float = 0.0

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CustomState":
        return cls(
            name=str(value.get("name", "")),
            rhs=str(value.get("rhs", "")),
            mass=float(value.get("mass", 1.0)),
            initial=float(value.get("initial", 0.0)),
        )


@dataclass(frozen=True)
class CustomParameter:
    name: str
    value: float
    unit: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CustomParameter":
        return cls(str(value.get("name", "")), float(value.get("value", 0.0)), str(value.get("unit", "")))


@dataclass(frozen=True)
class CustomModel:
    model_id: str
    name: str
    static_injection: str
    description: str
    base_power: float
    initialization: str
    states: tuple[CustomState, ...]
    parameters: tuple[CustomParameter, ...]
    outputs: dict[str, str]
    model_kind: str = "equation"
    base_model: str = ""
    base_category: str = ""
    interface_inputs: tuple[str, ...] = ()
    interface_outputs: tuple[str, ...] = ()
    julia_body: str = ""
    helper_functions: str = ""
    ports: tuple[PortDefinition, ...] = ()

    @classmethod
    def new(cls, static_injection: str = "", injection_sign: int = 1) -> "CustomModel":
        denominator = "(v_r^2 + v_i^2)"
        sign = "-" if injection_sign < 0 else ""
        return cls(
            model_id=str(uuid.uuid4()),
            name="新建微分方程模型",
            static_injection=static_injection,
            description="",
            base_power=100.0,
            initialization="fixed",
            states=(CustomState("x", "0.0", 1.0, 0.0),),
            parameters=(),
            outputs={
                "i_r": f"{sign}(P_ref * v_r + Q_ref * v_i) / {denominator}",
                "i_i": f"{sign}(P_ref * v_i - Q_ref * v_r) / {denominator}",
            },
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CustomModel":
        return cls(
            model_id=str(value.get("id", "")),
            name=str(value.get("name", "")),
            static_injection=str(value.get("static_injection", "")),
            description=str(value.get("description", "")),
            base_power=float(value.get("base_power", 100.0)),
            initialization=str(value.get("initialization", "fixed")),
            states=tuple(CustomState.from_dict(item) for item in value.get("states", [])),
            parameters=tuple(CustomParameter.from_dict(item) for item in value.get("parameters", [])),
            outputs={str(key): str(item) for key, item in value.get("outputs", {}).items()},
            model_kind=str(value.get("model_kind", "equation")),
            base_model=str(value.get("base_model", "")),
            base_category=str(value.get("base_category", "")),
            interface_inputs=tuple(str(item) for item in value.get("interface_inputs", [])),
            interface_outputs=tuple(str(item) for item in value.get("interface_outputs", [])),
            julia_body=str(value.get("julia_body", "")),
            helper_functions=str(value.get("helper_functions", "")),
            ports=tuple(PortDefinition.from_dict(item) for item in value.get("ports", [])),
        )

    def to_dict(self) -> dict[str, Any]:
        value = {
            "id": self.model_id,
            "name": self.name,
            "static_injection": self.static_injection,
            "description": self.description,
            "base_power": self.base_power,
            "initialization": self.initialization,
            "states": [state.__dict__ for state in self.states],
            "parameters": [parameter.__dict__ for parameter in self.parameters],
            "outputs": self.outputs,
        }
        if self.model_kind in {"derived", "topology"}:
            value.update(
                model_kind=self.model_kind,
                base_model=self.base_model,
                base_category=self.base_category,
                interface_inputs=list(self.interface_inputs),
                interface_outputs=list(self.interface_outputs),
                julia_body=self.julia_body,
                helper_functions=self.helper_functions,
            )
        if self.ports:
            value["ports"] = [port.to_dict() for port in self.ports]
        return value

    @property
    def is_derived(self) -> bool:
        return self.model_kind == "derived"

    @property
    def is_topology_model(self) -> bool:
        return self.model_kind == "topology"


class _ExpressionValidator(ast.NodeVisitor):
    def __init__(self, symbols: set[str]) -> None:
        self.symbols = symbols

    def generic_visit(self, node: ast.AST) -> None:
        allowed = (
            ast.Expression,
            ast.BinOp,
            ast.UnaryOp,
            ast.Constant,
            ast.Name,
            ast.Call,
            ast.Add,
            ast.Sub,
            ast.Mult,
            ast.Div,
            ast.Pow,
            ast.USub,
            ast.UAdd,
        )
        if not isinstance(node, allowed):
            raise CustomModelError(f"不支持的表达式结构：{type(node).__name__}")
        super().generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id not in self.symbols and node.id not in CONSTANTS:
            raise CustomModelError(f"未定义符号：{node.id}")

    def visit_Call(self, node: ast.Call) -> None:
        if not isinstance(node.func, ast.Name) or node.func.id not in ALLOWED_FUNCTIONS:
            name = getattr(node.func, "id", type(node.func).__name__)
            raise CustomModelError(f"不允许调用函数：{name}")
        if node.keywords:
            raise CustomModelError("函数调用不支持关键字参数")
        for argument in node.args:
            self.visit(argument)


def validate_expression(source: str, symbols: Iterable[str]) -> None:
    text = source.strip().replace("^", "**")
    if not text:
        raise CustomModelError("表达式不能为空")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as error:
        raise CustomModelError(f"表达式语法错误：{error.msg}") from error
    _ExpressionValidator(set(symbols)).visit(tree)


def validate_julia_model(model: CustomModel) -> None:
    if not model.base_model or not model.base_category:
        raise CustomModelError("派生模型必须选择基础官方模型")
    implementations = ModelLibrary().lookup(model.base_model)
    if not implementations:
        raise CustomModelError(f"基础官方模型不存在：{model.base_model}")
    if model.base_category not in {item.category for item in implementations}:
        raise CustomModelError(
            f"{model.base_model} 不属于 {model.base_category} 分类"
        )
    if not model.interface_outputs:
        raise CustomModelError("基础模型没有可继承的输出接口")
    source = f"{model.helper_functions}\n{model.julia_body}"
    forbidden = JULIA_FORBIDDEN.search(source)
    if forbidden:
        raise CustomModelError(f"Julia 数学函数不允许使用：{forbidden.group(0)}")
    if source.count("(") != source.count(")"):
        raise CustomModelError("Julia 代码中的圆括号不匹配")
    if source.count("[") != source.count("]"):
        raise CustomModelError("Julia 代码中的方括号不匹配")
    if not model.julia_body.strip():
        raise CustomModelError("模型函数体不能为空")
    state_names = {state.name for state in model.states}
    parameter_names = {parameter.name for parameter in model.parameters}
    declared_names = [state.name for state in model.states] + [
        parameter.name for parameter in model.parameters
    ]
    if any(not name.isidentifier() for name in declared_names):
        raise CustomModelError("状态名和参数名必须是有效标识符")
    if len(declared_names) != len(set(declared_names)):
        raise CustomModelError("状态名和参数名不能重复")
    allowed = {
        "dx": state_names,
        "x": state_names,
        "u": set(model.interface_inputs),
        "y": set(model.interface_outputs),
        "p": parameter_names,
    }
    for root, name in JULIA_PROPERTY.findall(source):
        if name not in allowed[root]:
            raise CustomModelError(f"{root}.{name} 不在继承接口中")
    for state in state_names:
        if not re.search(rf"\bdx\.{re.escape(state)}\s*=", model.julia_body):
            raise CustomModelError(f"模型函数必须给 dx.{state} 赋值")
    for output in model.interface_outputs:
        if not re.search(rf"\by\.{re.escape(output)}\s*=", model.julia_body):
            raise CustomModelError(f"模型函数必须给 y.{output} 赋值")
    helpers = JULIA_HELPER.findall(model.helper_functions)
    if len(helpers) != len(set(helpers)):
        raise CustomModelError("辅助函数名称不能重复")
    if "equations!" in helpers:
        raise CustomModelError("equations! 由系统生成，不能在辅助函数中重新定义")
    member_roots = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\.[A-Za-z_]", source)
    invalid_root = next((root for root in member_roots if root not in allowed), None)
    if invalid_root:
        raise CustomModelError(f"不允许访问 {invalid_root} 的成员")
    calls = set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_!]*)\s*\(", source))
    allowed_calls = ALLOWED_FUNCTIONS | {"zero", "one", "ifelse"} | set(helpers)
    invalid_call = next(
        (name for name in calls if name not in allowed_calls and name != "function"),
        None,
    )
    if invalid_call:
        raise CustomModelError(f"不允许调用 Julia 函数：{invalid_call}")
    depth = 0
    for token in re.findall(r"\b(function|if|for|while|let|begin|end)\b", source):
        depth += -1 if token == "end" else 1
        if depth < 0:
            raise CustomModelError("Julia 代码包含多余的 end")
    if depth:
        raise CustomModelError("Julia 代码块缺少 end")


def validate_model(model: CustomModel, injection_names: set[str]) -> None:
    if not model.model_id:
        raise CustomModelError("模型缺少 id")
    if not model.name.strip():
        raise CustomModelError("模型名称不能为空")
    if model.is_derived:
        validate_julia_model(model)
        return
    if model.is_topology_model:
        validate_topology_model(model)
        return
    if model.static_injection not in injection_names:
        raise CustomModelError(f"静态注入设备不存在：{model.static_injection}")
    if model.base_power <= 0:
        raise CustomModelError("设备基准功率必须大于零")
    if model.initialization not in {"fixed", "steady_state"}:
        raise CustomModelError("初始化方式必须是 fixed 或 steady_state")
    if not model.states:
        raise CustomModelError("至少需要一个微分状态")
    names = [state.name for state in model.states]
    parameters = [parameter.name for parameter in model.parameters]
    all_names = names + parameters
    if any(not name.isidentifier() for name in all_names):
        raise CustomModelError("状态名和参数名必须是有效标识符")
    if len(all_names) != len(set(all_names)):
        raise CustomModelError("状态名和参数名不能重复")
    reserved = set(SYSTEM_INPUTS) | set(OUTPUT_NAMES) | ALLOWED_FUNCTIONS | CONSTANTS
    conflict = sorted(set(all_names) & reserved)
    if conflict:
        raise CustomModelError(f"名称与系统符号冲突：{', '.join(conflict)}")
    symbols = set(SYSTEM_INPUTS) | set(all_names)
    for state in model.states:
        if state.mass <= 0:
            raise CustomModelError(f"状态 {state.name} 的质量系数必须大于零")
        validate_expression(state.rhs, symbols)
    if set(model.outputs) != set(OUTPUT_NAMES):
        raise CustomModelError("输出必须且只能定义 i_r 和 i_i")
    for expression in model.outputs.values():
        validate_expression(expression, symbols)


def validate_topology_model(model: CustomModel) -> None:
    if not model.ports:
        raise CustomModelError("自定义整体器件至少需要一个拓扑端口")
    port_ids = [port.port_id for port in model.ports]
    port_names = [port.name.casefold() for port in model.ports]
    if any(not value.isidentifier() for value in port_ids):
        raise CustomModelError("端口 ID 必须是有效标识符")
    if len(port_ids) != len(set(port_ids)) or len(port_names) != len(set(port_names)):
        raise CustomModelError("端口 ID 和显示名称不能重复")
    if any(port.domain not in {"AC_BUS", "DC_BUS", "SIGNAL"} for port in model.ports):
        raise CustomModelError("端口域必须是 AC_BUS、DC_BUS 或 SIGNAL")
    if any(port.role not in {"bidirectional", "source", "sink"} for port in model.ports):
        raise CustomModelError("端口角色无效")
    if model.base_power <= 0:
        raise CustomModelError("设备基准功率必须大于零")
    expected_inputs = tuple(
        value
        for port in model.ports
        for value in (f"{port.port_id}_v_r", f"{port.port_id}_v_i")
    )
    expected_outputs = tuple(
        value
        for port in model.ports
        for value in (f"{port.port_id}_i_r", f"{port.port_id}_i_i")
    )
    if model.interface_inputs != expected_inputs or model.interface_outputs != expected_outputs:
        raise CustomModelError("模型函数接口与拓扑端口定义不一致")
    source = f"{model.helper_functions}\n{model.julia_body}"
    forbidden = JULIA_FORBIDDEN.search(source)
    if forbidden:
        raise CustomModelError(f"Julia 数学函数不允许使用：{forbidden.group(0)}")
    if not model.julia_body.strip():
        raise CustomModelError("模型函数体不能为空")
    state_names = {state.name for state in model.states}
    parameter_names = {parameter.name for parameter in model.parameters}
    allowed = {
        "dx": state_names,
        "x": state_names,
        "u": set(expected_inputs),
        "y": set(expected_outputs),
        "p": parameter_names,
    }
    for root, name in JULIA_PROPERTY.findall(source):
        if name not in allowed[root]:
            raise CustomModelError(f"{root}.{name} 不在端口接口中")
    for state in state_names:
        if not re.search(rf"\bdx\.{re.escape(state)}\s*=", model.julia_body):
            raise CustomModelError(f"模型函数必须给 dx.{state} 赋值")
    for output in expected_outputs:
        if not re.search(rf"\by\.{re.escape(output)}\s*=", model.julia_body):
            raise CustomModelError(f"模型函数必须给 y.{output} 赋值")


class CustomModelStore:
    @staticmethod
    def path(root: Path) -> Path:
        return root / MODEL_FILE

    @classmethod
    def load(cls, root: Path, injections: tuple[Injection, ...]) -> tuple[CustomModel, ...]:
        path = cls.path(root)
        if not path.is_file():
            return ()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CustomModelError(f"{MODEL_FILE}：{error}") from error
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise CustomModelError(f"{MODEL_FILE}：仅支持 schema_version 1")
        raw_models = data.get("models", [])
        if not isinstance(raw_models, list) or any(not isinstance(item, dict) for item in raw_models):
            raise CustomModelError(f"{MODEL_FILE}：models 必须是对象数组")
        models = tuple(CustomModel.from_dict(item) for item in raw_models)
        ids = [model.model_id for model in models]
        names = [model.name.casefold() for model in models]
        if len(ids) != len(set(ids)):
            raise CustomModelError(f"{MODEL_FILE}：模型 id 不能重复")
        if len(names) != len(set(names)):
            raise CustomModelError(f"{MODEL_FILE}：模型名称不能重复")
        injection_by_name = {item.name: item for item in injections}
        attachments: set[str] = set()
        for model in models:
            validate_model(model, set(injection_by_name))
            if model.is_derived or model.is_topology_model:
                continue
            injection = injection_by_name[model.static_injection]
            if injection.dynamic_type:
                raise CustomModelError(f"{MODEL_FILE}：{injection.name} 已有动态模型")
            if model.static_injection in attachments:
                raise CustomModelError(f"{MODEL_FILE}：{model.static_injection} 重复绑定模型")
            attachments.add(model.static_injection)
        return models

    @classmethod
    def save(cls, root: Path, models: tuple[CustomModel, ...]) -> None:
        path = cls.path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": 1, "models": [model.to_dict() for model in models]}
        handle, temporary = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.replace(temporary, path)
        except Exception:
            Path(temporary).unlink(missing_ok=True)
            raise

    @staticmethod
    def fingerprint(models: tuple[CustomModel, ...]) -> str:
        structure = [
            {
                "model_kind": model.model_kind,
                "static_injection": model.static_injection,
                "base_model": model.base_model,
                "base_category": model.base_category,
                "interface_inputs": model.interface_inputs,
                "interface_outputs": model.interface_outputs,
                "ports": [port.to_dict() for port in model.ports],
                "states": [
                    {"name": state.name, "rhs": state.rhs, "mass": state.mass}
                    for state in model.states
                ],
                "parameters": [parameter.name for parameter in model.parameters],
                "outputs": model.outputs,
                "julia_body": model.julia_body,
                "helper_functions": model.helper_functions,
            }
            for model in models
            if model.model_kind in {"equation", "topology"}
        ]
        canonical = json.dumps(
            structure,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()


def apply_custom_models(project: ExportedProject, models: tuple[CustomModel, ...]) -> ExportedProject:
    executable = tuple(model for model in models if model.model_kind == "equation")
    by_injection = {model.static_injection: model for model in executable}
    injections = tuple(
        replace(item, dynamic_type="GUIEquationModel")
        if item.name in by_injection
        else replace(item, dynamic_type="")
        if item.dynamic_type == "GUIEquationModel"
        else item
        for item in project.injections
    )
    equations = [item for item in project.equations if item.role != "custom_dynamic"]
    for model in executable:
        root = f"injections/{model.static_injection}"
        raw = {
            "__metadata__": {"type": "GUIEquationModel"},
            "base_power": model.base_power,
            **{parameter.name: parameter.value for parameter in model.parameters},
        }
        equations.append(
            EquationComponent(root, "custom_dynamic", "GUIEquationModel", tuple(state.name for state in model.states), ("device!",), str(MODEL_FILE), raw)
        )
        for state in model.states:
            equations.append(
                EquationComponent(f"{root}/states/{state.name}", "custom_state", "DifferentialEquation", (state.name,), ("device!",), str(MODEL_FILE), {"rhs": state.rhs, "mass": state.mass, "initial": state.initial})
            )
    return replace(project, injections=injections, equations=tuple(equations), custom_models=models)


class CustomModelEditor(QtWidgets.QDialog):
    def __init__(self, model: CustomModel, injections: list[str], parent: QtWidgets.QWidget) -> None:
        super().__init__(parent)
        self.model_id = model.model_id
        self.setWindowTitle("微分方程模型编辑器")
        self.resize(900, 680)
        layout = QtWidgets.QVBoxLayout(self)
        tabs = QtWidgets.QTabWidget()
        layout.addWidget(tabs)

        basic = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(basic)
        self.name = QtWidgets.QLineEdit(model.name)
        self.injection = QtWidgets.QComboBox()
        self.injection.addItems(injections)
        self.injection.setCurrentText(model.static_injection)
        self.base_power = QtWidgets.QDoubleSpinBox()
        self.base_power.setRange(1e-6, 1e12)
        self.base_power.setDecimals(6)
        self.base_power.setValue(model.base_power)
        self.description = QtWidgets.QPlainTextEdit(model.description)
        form.addRow("模型名称", self.name)
        form.addRow("绑定静态注入设备", self.injection)
        form.addRow("设备基准功率 (MVA)", self.base_power)
        form.addRow("说明", self.description)
        tabs.addTab(basic, "基本信息")

        ports = QtWidgets.QWidget()
        ports_layout = QtWidgets.QVBoxLayout(ports)
        ports_layout.addWidget(QtWidgets.QLabel("系统输入（只读，由 PowerSimulationsDynamics 运行时提供）"))
        ports_layout.addWidget(QtWidgets.QLabel("、".join(SYSTEM_INPUTS)))
        ports_layout.addWidget(QtWidgets.QLabel("电流输出（正值表示向网络注入）"))
        ports_layout.addWidget(QtWidgets.QLabel("i_r、i_i"))
        ports_layout.addStretch()
        tabs.addTab(ports, "端口")

        self.states = self._table(("状态名", "右端 f(x,u)", "质量系数", "初值"), (130, 440, 100, 100))
        for state in model.states:
            self._append_row(self.states, (state.name, state.rhs, state.mass, state.initial))
        tabs.addTab(self._table_page(self.states), "状态与方程")

        self.parameters = self._table(("参数名", "数值", "单位"), (200, 180, 160))
        for parameter in model.parameters:
            self._append_row(self.parameters, (parameter.name, parameter.value, parameter.unit))
        tabs.addTab(self._table_page(self.parameters), "参数")

        outputs = QtWidgets.QWidget()
        output_form = QtWidgets.QFormLayout(outputs)
        self.i_r = QtWidgets.QLineEdit(model.outputs.get("i_r", "0.0"))
        self.i_i = QtWidgets.QLineEdit(model.outputs.get("i_i", "0.0"))
        output_form.addRow("i_r =", self.i_r)
        output_form.addRow("i_i =", self.i_i)
        output_form.addRow(QtWidgets.QLabel("可用符号：状态、参数和系统输入；支持 + - * / ^ 与常用数学函数。"))
        tabs.addTab(outputs, "网络输出")

        initialize = QtWidgets.QWidget()
        init_form = QtWidgets.QFormLayout(initialize)
        self.initialization = QtWidgets.QComboBox()
        self.initialization.addItem("使用状态表中的固定初值", "fixed")
        self.initialization.addItem("求解稳态 f(x,u)=0", "steady_state")
        index = self.initialization.findData(model.initialization)
        self.initialization.setCurrentIndex(max(index, 0))
        init_form.addRow("初始化方式", self.initialization)
        init_form.addRow(QtWidgets.QLabel("稳态初始化以表中初值作为非线性求解初猜。"))
        tabs.addTab(initialize, "初始化")

        self.error = QtWidgets.QLabel()
        self.error.setStyleSheet("color:#c0392b")
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Save
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _table(headers: tuple[str, ...], widths: tuple[int, ...]) -> QtWidgets.QTableWidget:
        table = QtWidgets.QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        for index, width in enumerate(widths):
            table.setColumnWidth(index, width)
        table.horizontalHeader().setStretchLastSection(True)
        table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        return table

    def _table_page(self, table: QtWidgets.QTableWidget) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.addWidget(table)
        row = QtWidgets.QHBoxLayout()
        add = QtWidgets.QPushButton("添加")
        remove = QtWidgets.QPushButton("删除所选")
        add.clicked.connect(lambda: table.insertRow(table.rowCount()))
        remove.clicked.connect(lambda: self._remove_selected(table))
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch()
        layout.addLayout(row)
        return page

    @staticmethod
    def _append_row(table: QtWidgets.QTableWidget, values: tuple[Any, ...]) -> None:
        row = table.rowCount()
        table.insertRow(row)
        for column, value in enumerate(values):
            table.setItem(row, column, QtWidgets.QTableWidgetItem(str(value)))

    @staticmethod
    def _remove_selected(table: QtWidgets.QTableWidget) -> None:
        for row in sorted({index.row() for index in table.selectedIndexes()}, reverse=True):
            table.removeRow(row)

    @staticmethod
    def _cell(table: QtWidgets.QTableWidget, row: int, column: int) -> str:
        item = table.item(row, column)
        return item.text().strip() if item else ""

    def value(self) -> CustomModel:
        states = tuple(
            CustomState(
                self._cell(self.states, row, 0),
                self._cell(self.states, row, 1),
                float(self._cell(self.states, row, 2)),
                float(self._cell(self.states, row, 3)),
            )
            for row in range(self.states.rowCount())
        )
        parameters = tuple(
            CustomParameter(
                self._cell(self.parameters, row, 0),
                float(self._cell(self.parameters, row, 1)),
                self._cell(self.parameters, row, 2),
            )
            for row in range(self.parameters.rowCount())
        )
        return CustomModel(
            self.model_id,
            self.name.text().strip(),
            self.injection.currentText(),
            self.description.toPlainText().strip(),
            self.base_power.value(),
            str(self.initialization.currentData()),
            states,
            parameters,
            {"i_r": self.i_r.text().strip(), "i_i": self.i_i.text().strip()},
        )

    def _accept(self) -> None:
        try:
            model = self.value()
            validate_model(model, {self.injection.itemText(i) for i in range(self.injection.count())})
        except (CustomModelError, ValueError) as error:
            self.error.setText(str(error))
            return
        self.accept()


def inherited_model_template(
    model_type: str,
    category: str,
    implementations: tuple[Any, ...],
    project: ExportedProject | None = None,
) -> CustomModel:
    relevant = tuple(item for item in implementations if item.category == category) or implementations[:1]
    equations = tuple(
        equation
        for implementation in relevant
        for equation in (*implementation.derivatives, *implementation.algebraic, *implementation.mass_matrix)
    )
    project_states: tuple[str, ...] = ()
    if project:
        component = next(
            (
                item
                for item in project.equations
                if item.component_type.rsplit(".", 1)[-1] == model_type and item.states
            ),
            None,
        )
        if component:
            project_states = component.states
    inferred_states = tuple(
        dict.fromkeys(
            re.findall(r"\b[dD]([A-Za-z_][A-Za-z0-9_]*)_dt\b", "\n".join(equations))
            + re.findall(r"global_index\[:([A-Za-z_][A-Za-z0-9_]*)\]", "\n".join(equations))
        )
    )
    derivative_count = max(
        (
            int(index)
            for index in re.findall(r"(?:output_ode|device_ode)\[(?:local_ix\[)?(\d+)", "\n".join(equations))
        ),
        default=0,
    )
    state_names = project_states or inferred_states
    if not state_names and derivative_count:
        state_names = tuple(f"state_{index}" for index in range(1, derivative_count + 1))
    outputs = list(COMPONENT_OUTPUTS.get(category, ()))
    extracted_outputs = []
    for equation in equations:
        inner = re.match(r"inner_vars\[([A-Za-z_][A-Za-z0-9_]*)_var\]", equation)
        if inner:
            extracted_outputs.append(inner.group(1))
        if equation.startswith("current_r"):
            extracted_outputs.append("i_r")
        if equation.startswith("current_i"):
            extracted_outputs.append("i_i")
    if extracted_outputs:
        outputs = list(dict.fromkeys(extracted_outputs))
    parameter_names = tuple(
        dict.fromkeys(
            name
            for name in re.findall(r"PSY\.get_([A-Za-z_][A-Za-z0-9_]*)\(", "\n".join(equations))
            if name not in {"states", "n_states", "ext", "internal"}
        )
    )
    body = [f"dx.{name} = 0.0" for name in state_names]
    for output in outputs:
        body.append(f"y.{output} = x.{output}" if output in state_names else f"y.{output} = 0.0")
    return CustomModel(
        model_id=str(uuid.uuid4()),
        name=f"My{model_type}",
        static_injection="",
        description=f"继承 {model_type} 的输入、状态和输出接口",
        base_power=100.0,
        initialization="fixed",
        states=tuple(CustomState(name, "", 1.0, 0.0) for name in state_names),
        parameters=tuple(CustomParameter(name, 0.0) for name in parameter_names),
        outputs={},
        model_kind="derived",
        base_model=model_type,
        base_category=category,
        interface_inputs=COMPONENT_INPUTS.get(category, ()),
        interface_outputs=tuple(outputs),
        julia_body="\n".join(body),
    )


class JuliaHighlighter(QtGui.QSyntaxHighlighter):
    def __init__(self, document: QtGui.QTextDocument) -> None:
        super().__init__(document)
        keyword = QtGui.QTextCharFormat()
        keyword.setForeground(QtGui.QColor("#9b59b6"))
        keyword.setFontWeight(QtGui.QFont.Weight.Bold)
        number = QtGui.QTextCharFormat()
        number.setForeground(QtGui.QColor("#d35400"))
        comment = QtGui.QTextCharFormat()
        comment.setForeground(QtGui.QColor("#7f8c8d"))
        self.rules = (
            (QtCore.QRegularExpression(r"\b(function|end|if|elseif|else|return|for|while|local)\b"), keyword),
            (QtCore.QRegularExpression(r"\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b"), number),
            (QtCore.QRegularExpression(r"#.*$"), comment),
        )

    def highlightBlock(self, text: str) -> None:
        for expression, style in self.rules:
            iterator = expression.globalMatch(text)
            while iterator.hasNext():
                match = iterator.next()
                self.setFormat(match.capturedStart(), match.capturedLength(), style)


class TopologyModelEditor(QtWidgets.QDialog):
    """Two-step editor for a reusable whole-device topology model."""

    def __init__(self, model: CustomModel, parent: QtWidgets.QWidget) -> None:
        super().__init__(parent)
        self.original = model
        self.setWindowTitle("自定义整体器件 — 端口与数学模型")
        self.resize(1040, 780)
        layout = QtWidgets.QVBoxLayout(self)
        self.steps = QtWidgets.QTabWidget()
        layout.addWidget(self.steps, 1)

        ports_page = QtWidgets.QWidget()
        ports_layout = QtWidgets.QVBoxLayout(ports_page)
        ports_layout.addWidget(QtWidgets.QLabel("第一步：定义稳定端口 ID。每个非 Bus 端口最多连接一次。"))
        self.ports = CustomModelEditor._table(
            ("端口 ID", "显示名称", "端口域", "角色", "必需", "侧边"),
            (150, 170, 120, 130, 80, 100),
        )
        for port in model.ports:
            CustomModelEditor._append_row(
                self.ports,
                (port.port_id, port.name, port.domain, port.role, "是" if port.required else "否", port.side),
            )
        ports_layout.addWidget(self.ports)
        port_buttons = QtWidgets.QHBoxLayout()
        add_port = QtWidgets.QPushButton("添加端口")
        remove_port = QtWidgets.QPushButton("删除所选")
        add_port.clicked.connect(self._add_port)
        remove_port.clicked.connect(lambda: CustomModelEditor._remove_selected(self.ports))
        port_buttons.addWidget(add_port)
        port_buttons.addWidget(remove_port)
        port_buttons.addStretch()
        ports_layout.addLayout(port_buttons)
        self.steps.addTab(ports_page, "1  拓扑端口")

        model_page = QtWidgets.QWidget()
        model_layout = QtWidgets.QVBoxLayout(model_page)
        form = QtWidgets.QFormLayout()
        self.name = QtWidgets.QLineEdit(model.name)
        self.base_model = QtWidgets.QComboBox()
        self.base_model.addItem("不继承官方模型", "")
        library = ModelLibrary()
        for model_type in library.model_types:
            categories = {item.category for item in library.lookup(model_type)}
            if categories & {"聚合动态设备", "电源", "负荷", "动态支路"}:
                self.base_model.addItem(model_type, model_type)
        index = self.base_model.findData(model.base_model)
        self.base_model.setCurrentIndex(max(index, 0))
        self.base_power = QtWidgets.QDoubleSpinBox()
        self.base_power.setRange(1e-6, 1e12)
        self.base_power.setDecimals(6)
        self.base_power.setValue(model.base_power)
        self.description = QtWidgets.QPlainTextEdit(model.description)
        form.addRow("模型名称", self.name)
        form.addRow("继承官方整体模型", self.base_model)
        form.addRow("设备基准功率 (MVA)", self.base_power)
        form.addRow("说明", self.description)
        model_layout.addLayout(form)
        self.states = CustomModelEditor._table(("状态名", "质量系数", "初值"), (220, 150, 150))
        for state in model.states:
            CustomModelEditor._append_row(self.states, (state.name, state.mass, state.initial))
        model_layout.addWidget(QtWidgets.QLabel("状态"))
        model_layout.addWidget(self.states)
        state_buttons = QtWidgets.QHBoxLayout()
        add_state = QtWidgets.QPushButton("添加状态")
        remove_state = QtWidgets.QPushButton("删除所选")
        add_state.clicked.connect(lambda: self.states.insertRow(self.states.rowCount()))
        remove_state.clicked.connect(lambda: CustomModelEditor._remove_selected(self.states))
        state_buttons.addWidget(add_state)
        state_buttons.addWidget(remove_state)
        state_buttons.addStretch()
        model_layout.addLayout(state_buttons)
        self.parameters = CustomModelEditor._table(("参数名", "默认值", "单位"), (220, 160, 150))
        for parameter in model.parameters:
            CustomModelEditor._append_row(self.parameters, (parameter.name, parameter.value, parameter.unit))
        model_layout.addWidget(QtWidgets.QLabel("参数"))
        model_layout.addWidget(self.parameters)
        parameter_buttons = QtWidgets.QHBoxLayout()
        add_parameter = QtWidgets.QPushButton("添加参数")
        remove_parameter = QtWidgets.QPushButton("删除所选")
        add_parameter.clicked.connect(lambda: self.parameters.insertRow(self.parameters.rowCount()))
        remove_parameter.clicked.connect(lambda: CustomModelEditor._remove_selected(self.parameters))
        parameter_buttons.addWidget(add_parameter)
        parameter_buttons.addWidget(remove_parameter)
        parameter_buttons.addStretch()
        model_layout.addLayout(parameter_buttons)
        self.steps.addTab(model_page, "2  模型特性")

        code_page = QtWidgets.QWidget()
        code_layout = QtWidgets.QVBoxLayout(code_page)
        code_layout.addWidget(QtWidgets.QLabel("function equations!(dx, y, x, u, p, t)"))
        self.julia_body = QtWidgets.QPlainTextEdit(model.julia_body)
        self.julia_body.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont))
        self._body_highlighter = JuliaHighlighter(self.julia_body.document())
        code_layout.addWidget(self.julia_body, 1)
        code_layout.addWidget(QtWidgets.QLabel("辅助函数"))
        self.helpers = QtWidgets.QPlainTextEdit(model.helper_functions)
        self.helpers.setMaximumHeight(150)
        self.helpers.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont))
        self._helper_highlighter = JuliaHighlighter(self.helpers.document())
        code_layout.addWidget(self.helpers)
        self.steps.addTab(code_page, "3  Julia 数学函数")

        self.error = QtWidgets.QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color:#c0392b")
        layout.addWidget(self.error)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Save
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        refresh = buttons.addButton("按端口生成函数模板", QtWidgets.QDialogButtonBox.ButtonRole.ActionRole)
        refresh.clicked.connect(self._generate_template)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _add_port(self) -> None:
        index = self.ports.rowCount() + 1
        CustomModelEditor._append_row(self.ports, (f"port_{index}", f"端口 {index}", "AC_BUS", "bidirectional", "是", "auto"))

    def _port_values(self) -> tuple[PortDefinition, ...]:
        return tuple(
            PortDefinition(
                CustomModelEditor._cell(self.ports, row, 0),
                CustomModelEditor._cell(self.ports, row, 1),
                CustomModelEditor._cell(self.ports, row, 2).upper(),
                CustomModelEditor._cell(self.ports, row, 3).lower(),
                CustomModelEditor._cell(self.ports, row, 4) not in {"否", "false", "0"},
                CustomModelEditor._cell(self.ports, row, 5).lower() or "auto",
            )
            for row in range(self.ports.rowCount())
        )

    def _generate_template(self) -> None:
        ports = self._port_values()
        lines = [
            f"dx.{CustomModelEditor._cell(self.states, row, 0)} = 0.0"
            for row in range(self.states.rowCount())
            if CustomModelEditor._cell(self.states, row, 0)
        ]
        lines.extend(
            f"y.{port.port_id}_{axis} = 0.0"
            for port in ports
            for axis in ("i_r", "i_i")
        )
        self.julia_body.setPlainText("\n".join(lines))
        self.steps.setCurrentIndex(2)

    def value(self) -> CustomModel:
        ports = self._port_values()
        states = tuple(
            CustomState(
                CustomModelEditor._cell(self.states, row, 0),
                "",
                float(CustomModelEditor._cell(self.states, row, 1)),
                float(CustomModelEditor._cell(self.states, row, 2)),
            )
            for row in range(self.states.rowCount())
        )
        parameters = tuple(
            CustomParameter(
                CustomModelEditor._cell(self.parameters, row, 0),
                float(CustomModelEditor._cell(self.parameters, row, 1)),
                CustomModelEditor._cell(self.parameters, row, 2),
            )
            for row in range(self.parameters.rowCount())
        )
        inputs = tuple(value for port in ports for value in (f"{port.port_id}_v_r", f"{port.port_id}_v_i"))
        outputs = tuple(value for port in ports for value in (f"{port.port_id}_i_r", f"{port.port_id}_i_i"))
        return replace(
            self.original,
            name=self.name.text().strip(),
            description=self.description.toPlainText().strip(),
            base_power=self.base_power.value(),
            model_kind="topology",
            base_model=str(self.base_model.currentData()),
            base_category="自定义整体器件",
            states=states,
            parameters=parameters,
            interface_inputs=inputs,
            interface_outputs=outputs,
            julia_body=self.julia_body.toPlainText().strip(),
            helper_functions=self.helpers.toPlainText().strip(),
            ports=ports,
            static_injection="",
        )

    def _accept(self) -> None:
        try:
            validate_topology_model(self.value())
        except (CustomModelError, ValueError) as error:
            self.error.setText(str(error))
            return
        self.accept()


class InheritedModelEditor(QtWidgets.QDialog):
    def __init__(self, model: CustomModel, parent: QtWidgets.QWidget) -> None:
        super().__init__(parent)
        self.original = model
        self.setWindowTitle(f"官方派生模型编辑器 — {model.base_model}")
        self.resize(980, 760)
        layout = QtWidgets.QVBoxLayout(self)
        banner = QtWidgets.QLabel(
            "接口继承自官方模型并保持只读；函数体支持纯数学 Julia 语法。"
            "派生组件将在后续组件添加/替换时装配到复合设备。"
        )
        banner.setWordWrap(True)
        banner.setStyleSheet("background:rgba(35,137,215,0.12);padding:8px;color:#1676b5")
        layout.addWidget(banner)
        tabs = QtWidgets.QTabWidget()
        layout.addWidget(tabs, 1)

        basic = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(basic)
        self.name = QtWidgets.QLineEdit(model.name)
        base = QtWidgets.QLineEdit(model.base_model)
        base.setReadOnly(True)
        category = QtWidgets.QLineEdit(model.base_category)
        category.setReadOnly(True)
        self.description = QtWidgets.QPlainTextEdit(model.description)
        form.addRow("模型名称", self.name)
        form.addRow("基础官方模型", base)
        form.addRow("兼容分类", category)
        form.addRow("说明", self.description)
        tabs.addTab(basic, "基本信息")

        interface = QtWidgets.QTableWidget(0, 3)
        interface.setHorizontalHeaderLabels(("种类", "名称", "来源"))
        interface.horizontalHeader().setStretchLastSection(True)
        interface.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        for kind, names in (
            ("输入 u", model.interface_inputs),
            ("状态 x / dx", tuple(state.name for state in model.states)),
            ("输出 y", model.interface_outputs),
        ):
            for name in names:
                row = interface.rowCount()
                interface.insertRow(row)
                interface.setItem(row, 0, QtWidgets.QTableWidgetItem(kind))
                interface.setItem(row, 1, QtWidgets.QTableWidgetItem(name))
                interface.setItem(row, 2, QtWidgets.QTableWidgetItem(f"🔒 {model.base_model}"))
        tabs.addTab(interface, "继承接口")

        parameter_page = QtWidgets.QWidget()
        parameter_layout = QtWidgets.QVBoxLayout(parameter_page)
        self.parameters = CustomModelEditor._table(("参数名", "默认值", "单位"), (230, 180, 180))
        for parameter in model.parameters:
            CustomModelEditor._append_row(
                self.parameters, (parameter.name, parameter.value, parameter.unit)
            )
        parameter_layout.addWidget(self.parameters)
        parameter_buttons = QtWidgets.QHBoxLayout()
        add_parameter = QtWidgets.QPushButton("添加自定义参数")
        remove_parameter = QtWidgets.QPushButton("删除所选")
        add_parameter.clicked.connect(lambda: self.parameters.insertRow(self.parameters.rowCount()))
        remove_parameter.clicked.connect(
            lambda: CustomModelEditor._remove_selected(self.parameters)
        )
        parameter_buttons.addWidget(add_parameter)
        parameter_buttons.addWidget(remove_parameter)
        parameter_buttons.addStretch()
        parameter_layout.addLayout(parameter_buttons)
        tabs.addTab(parameter_page, "参数")

        function_page = QtWidgets.QWidget()
        function_layout = QtWidgets.QVBoxLayout(function_page)
        signature = QtWidgets.QLineEdit("function equations!(dx, y, x, u, p, t)")
        signature.setReadOnly(True)
        signature.setStyleSheet("font-family:monospace;background:rgba(127,127,127,0.12)")
        function_layout.addWidget(signature)
        self.julia_body = QtWidgets.QPlainTextEdit(model.julia_body)
        self.julia_body.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
        self.julia_body.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont))
        self._body_highlighter = JuliaHighlighter(self.julia_body.document())
        function_layout.addWidget(self.julia_body, 1)
        function_layout.addWidget(QtWidgets.QLabel("end  # 签名与 end 由系统生成"))
        tabs.addTab(function_page, "模型函数")

        helper_page = QtWidgets.QWidget()
        helper_layout = QtWidgets.QVBoxLayout(helper_page)
        helper_layout.addWidget(
            QtWidgets.QLabel("可定义纯数学辅助函数；禁止模块加载、宏、I/O、进程和动态求值。")
        )
        self.helpers = QtWidgets.QPlainTextEdit(model.helper_functions)
        self.helpers.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
        self.helpers.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont))
        self._helper_highlighter = JuliaHighlighter(self.helpers.document())
        helper_layout.addWidget(self.helpers)
        tabs.addTab(helper_page, "辅助函数")

        self.error = QtWidgets.QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color:#c0392b")
        layout.addWidget(self.error)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Save
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        check = buttons.addButton("检查接口与语法", QtWidgets.QDialogButtonBox.ButtonRole.ActionRole)
        check.clicked.connect(self._check)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def value(self) -> CustomModel:
        parameters = tuple(
            CustomParameter(
                CustomModelEditor._cell(self.parameters, row, 0),
                float(CustomModelEditor._cell(self.parameters, row, 1)),
                CustomModelEditor._cell(self.parameters, row, 2),
            )
            for row in range(self.parameters.rowCount())
        )
        return replace(
            self.original,
            name=self.name.text().strip(),
            description=self.description.toPlainText().strip(),
            parameters=parameters,
            julia_body=self.julia_body.toPlainText().strip(),
            helper_functions=self.helpers.toPlainText().strip(),
        )

    def _check(self) -> bool:
        try:
            validate_model(self.value(), set())
        except (CustomModelError, ValueError) as error:
            self.error.setStyleSheet("color:#c0392b")
            self.error.setText(str(error))
            return False
        self.error.setStyleSheet("color:#2389d7")
        self.error.setText("接口与受限 Julia 数学语法检查通过")
        return True

    def _accept(self) -> None:
        if self._check():
            self.accept()


class CustomModelManagerDialog(QtWidgets.QDialog):
    models_changed = QtCore.Signal(object)
    editing_changed = QtCore.Signal(bool)
    locate_requested = QtCore.Signal(str)
    instantiate_requested = QtCore.Signal(object)

    ACTIVE_CONTROLS = {
        "ActivePowerDroop",
        "ActivePowerPI",
        "ActiveRenewableControllerAB",
        "ActiveVirtualOscillator",
        "VirtualInertia",
    }
    REACTIVE_CONTROLS = {
        "ReactivePowerDroop",
        "ReactivePowerPI",
        "ReactiveRenewableControllerAB",
        "ReactiveVirtualOscillator",
    }
    FILTERS = {"LCLFilter", "RLFilter"}
    CATEGORY_PATHS = {
        "同步机": ("动态注入设备", "动态发电机", "同步机"),
        "轴系": ("动态注入设备", "动态发电机", "轴系"),
        "励磁系统": ("动态注入设备", "动态发电机", "励磁系统"),
        "调速器": ("动态注入设备", "动态发电机", "调速器"),
        "电力系统稳定器": ("动态注入设备", "动态发电机", "电力系统稳定器"),
        "内环控制器": ("动态注入设备", "动态逆变器", "内环控制器"),
        "直流侧": ("动态注入设备", "动态逆变器", "直流侧"),
        "频率估计器": ("动态注入设备", "动态逆变器", "频率估计器"),
        "滤波器": ("动态注入设备", "动态逆变器", "滤波器"),
        "输出电流限幅器": ("动态注入设备", "动态逆变器", "输出电流限幅器"),
        "聚合动态设备": ("动态注入设备", "独立动态设备"),
        "电源": ("网络元件模型", "电源"),
        "负荷": ("网络元件模型", "负荷"),
        "动态支路": ("网络元件模型", "动态支路"),
    }
    ADDABLE_PATHS = {
        ("动态注入设备",),
        ("动态注入设备", "独立动态设备"),
        ("项目自定义模型",),
        ("项目自定义模型", "自定义动态注入"),
        ("项目自定义模型", "自定义整体器件"),
    }
    DEVICE_MODELS = {
        "ACBus": (("网络元件模型", "母线"), "Bus"),
        "Line": (("网络元件模型", "支路"), "线路"),
        "Transformer2W": (("网络元件模型", "支路"), "双绕组变压器"),
        "StandardLoad": (("网络元件模型", "负荷"), "负荷"),
        "Source": (("网络元件模型", "电源"), "电源"),
        "DynamicGenerator": (("动态注入设备", "动态发电机"), "动态发电机"),
        "DynamicInverter": (("动态注入设备", "动态逆变器"), "动态逆变器"),
    }

    def __init__(self, parent: QtWidgets.QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("模型管理器")
        self.resize(1180, 720)
        self.project: ExportedProject | None = None
        self.models: tuple[CustomModel, ...] = ()
        self.library = ModelLibrary()
        self._catalogue = self._build_catalogue()
        self._editing_enabled = False
        self._instantiation: dict[str, str] | None = None

        layout = QtWidgets.QVBoxLayout(self)
        filters = QtWidgets.QHBoxLayout()
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("搜索模型名称、分类、方程入口或状态")
        self.search.setClearButtonEnabled(True)
        self.source_filter = QtWidgets.QComboBox()
        self.source_filter.addItems(("全部来源", "官方模型", "项目自定义"))
        self.edit_toggle = QtWidgets.QCheckBox("编辑模式")
        filters.addWidget(QtWidgets.QLabel("搜索"))
        filters.addWidget(self.search, 1)
        filters.addWidget(QtWidgets.QLabel("来源"))
        filters.addWidget(self.source_filter)
        filters.addWidget(self.edit_toggle)
        layout.addLayout(filters)

        splitter = QtWidgets.QSplitter()
        self.categories = QtWidgets.QTreeWidget()
        self.categories.setHeaderLabel("模型分类")
        self.categories.setMinimumWidth(240)
        self.list = QtWidgets.QListWidget()
        self.list.setMinimumWidth(340)
        self.list.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.details = QtWidgets.QTextBrowser()
        self.details.setOpenExternalLinks(False)
        splitter.addWidget(self.categories)
        splitter.addWidget(self.list)
        splitter.addWidget(self.details)
        splitter.setSizes((260, 380, 520))
        layout.addWidget(splitter, 1)

        controls = QtWidgets.QHBoxLayout()
        self.create_button = QtWidgets.QPushButton("新建自定义模型…")
        self.create_topology_button = QtWidgets.QPushButton("新建自定义整体器件…")
        self.edit_button = QtWidgets.QPushButton("编辑…")
        self.duplicate_button = QtWidgets.QPushButton("复制")
        self.delete_button = QtWidgets.QPushButton("删除")
        for button in (self.create_button, self.create_topology_button, self.edit_button, self.duplicate_button, self.delete_button):
            controls.addWidget(button)
        controls.addStretch()
        self.instantiate_button = QtWidgets.QPushButton("实例化")
        self.instantiate_button.setDefault(True)
        self.instantiate_button.hide()
        self.instantiate_button.clicked.connect(self._instantiate_selected)
        controls.addWidget(self.instantiate_button)
        close = QtWidgets.QPushButton("关闭")
        close.clicked.connect(self.close)
        controls.addWidget(close)
        layout.addLayout(controls)
        self.status = QtWidgets.QLabel("选择一个分类或模型")
        self.status.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        self.status.setMargin(6)
        layout.addWidget(self.status)

        self.search.textChanged.connect(self.refresh)
        self.source_filter.currentIndexChanged.connect(self.refresh)
        self.edit_toggle.toggled.connect(self._edit_toggle_changed)
        self.categories.currentItemChanged.connect(lambda *_: self.refresh())
        self.list.currentItemChanged.connect(self._selection_changed)
        self.list.itemDoubleClicked.connect(self._activate_item)
        self.list.customContextMenuRequested.connect(self._show_context_menu)
        self.create_button.clicked.connect(self.create_model)
        self.create_topology_button.clicked.connect(self.create_topology_model)
        self.edit_button.clicked.connect(self.edit_selected)
        self.duplicate_button.clicked.connect(self.duplicate_selected)
        self.delete_button.clicked.connect(self.delete_selected)
        self._populate_categories()
        self._update_actions()

    def _build_catalogue(self) -> dict[str, tuple[str, ...]]:
        result: dict[str, tuple[str, ...]] = {}
        for model_type in self.library.model_types:
            implementations = self.library.lookup(model_type)
            categories = {item.category for item in implementations}
            if model_type in self.ACTIVE_CONTROLS:
                path = ("动态注入设备", "动态逆变器", "外环控制器", "有功控制")
            elif model_type in self.REACTIVE_CONTROLS:
                path = ("动态注入设备", "动态逆变器", "外环控制器", "无功控制")
            elif model_type in self.FILTERS:
                path = ("动态注入设备", "动态逆变器", "滤波器")
            else:
                category = next((key for key in self.CATEGORY_PATHS if key in categories), "")
                path = self.CATEGORY_PATHS.get(category, ("其他模型",))
                if category == "变流器":
                    path = ("动态注入设备", "动态逆变器", "变流器")
            result[model_type] = path
        # Whole-device definitions are valid library entries too.  Most of the
        # equation catalogue describes strong-typed internal slots and must not
        # be instantiated directly on the network canvas.
        result.update({name: value[0] for name, value in self.DEVICE_MODELS.items()})
        return result

    def _populate_categories(self) -> None:
        self.categories.clear()
        nodes: dict[tuple[str, ...], QtWidgets.QTreeWidgetItem] = {}
        all_models = QtWidgets.QTreeWidgetItem(
            self.categories.invisibleRootItem(),
            [f"全部模型  ({len(self._catalogue) + len(self.models)})"],
        )
        all_models.setData(0, QtCore.Qt.ItemDataRole.UserRole, ())
        nodes[()] = all_models
        paths = set(self._catalogue.values()) | {
            ("动态注入设备", "动态发电机"),
            ("动态注入设备", "动态逆变器"),
            ("项目自定义模型", "自定义动态注入"),
            ("项目自定义模型", "自定义整体器件"),
            *(value[0] for value in self.DEVICE_MODELS.values()),
        }
        for path in sorted(paths):
            for depth in range(1, len(path) + 1):
                current = path[:depth]
                if current in nodes:
                    continue
                parent = nodes.get(current[:-1], self.categories.invisibleRootItem())
                item = QtWidgets.QTreeWidgetItem(parent, [current[-1]])
                item.setData(0, QtCore.Qt.ItemDataRole.UserRole, current)
                nodes[current] = item
        for path, item in nodes.items():
            if not path:
                continue
            official_count = sum(value[: len(path)] == path for value in self._catalogue.values())
            if path == ("项目自定义模型",):
                count = sum(not item.is_derived for item in self.models)
            elif path == ("项目自定义模型", "自定义动态注入"):
                count = sum(item.model_kind == "equation" for item in self.models)
            elif path == ("项目自定义模型", "自定义整体器件"):
                count = sum(item.is_topology_model for item in self.models)
            else:
                count = official_count
            item.setText(0, f"{path[-1]}  ({count})")
        self.categories.expandToDepth(1)
        target = nodes.get(())
        if target:
            self.categories.setCurrentItem(target)

    def set_project(self, project: ExportedProject) -> None:
        self.project = project
        self.models = tuple(project.custom_models)
        self.setWindowTitle(f"模型管理器 — {project.root.name}")
        current_path = self._current_category()
        self._populate_categories()
        self._select_category(current_path)
        self.refresh()

    def set_editing_enabled(self, enabled: bool) -> None:
        blocker = QtCore.QSignalBlocker(self.edit_toggle)
        self.edit_toggle.setChecked(enabled)
        del blocker
        self._editing_enabled = enabled
        self._update_actions()

    def _edit_toggle_changed(self, enabled: bool) -> None:
        self._editing_enabled = enabled
        self.editing_changed.emit(enabled)
        self._update_actions()

    def _current_category(self) -> tuple[str, ...]:
        item = self.categories.currentItem()
        value = item.data(0, QtCore.Qt.ItemDataRole.UserRole) if item else ()
        return tuple(value or ())

    def _select_category(self, path: tuple[str, ...]) -> None:
        iterator = QtWidgets.QTreeWidgetItemIterator(self.categories)
        while iterator.value():
            item = iterator.value()
            if tuple(item.data(0, QtCore.Qt.ItemDataRole.UserRole) or ()) == path:
                self.categories.setCurrentItem(item)
                return
            iterator += 1

    def refresh(self) -> None:
        if not hasattr(self, "list"):
            return
        selected = self.list.currentItem()
        selected_data = selected.data(QtCore.Qt.ItemDataRole.UserRole) if selected else None
        self.list.clear()
        path = self._current_category()
        query = self.search.text().strip().casefold()
        source = self.source_filter.currentText()
        show_official = source != "项目自定义" and (not path or path[0] != "项目自定义模型")
        show_custom = source != "官方模型"

        if show_official:
            for model_type, model_path in sorted(self._catalogue.items(), key=lambda item: item[0].casefold()):
                if path and model_path[: len(path)] != path:
                    continue
                implementations = self.library.lookup(model_type)
                search_text = " ".join(
                    (model_type, *model_path, *(item.entrypoint for item in implementations))
                ).casefold()
                if query and query not in search_text:
                    continue
                usages = self._official_usage_count(model_type)
                suffix = f"    使用 {usages}" if usages else ""
                item = QtWidgets.QListWidgetItem(f"🔒  {model_type}{suffix}")
                item.setData(QtCore.Qt.ItemDataRole.UserRole, ("official", model_type))
                item.setToolTip("PowerSimulationsDynamics 官方模型，只读")
                self.list.addItem(item)

        if show_custom:
            for model in sorted(self.models, key=lambda item: item.name.casefold()):
                model_path = (
                    self._catalogue.get(model.base_model, ("其他模型",))
                    if model.is_derived
                    else ("项目自定义模型", "自定义整体器件")
                    if model.is_topology_model
                    else ("项目自定义模型", "自定义动态注入")
                )
                in_project_index = not model.is_derived and path and (
                    path == ("项目自定义模型",)
                    or model_path == path
                )
                if path and not in_project_index and model_path[: len(path)] != path:
                    continue
                search_text = " ".join(
                    (
                        model.name,
                        model.base_model,
                        model.base_category,
                        model.static_injection,
                        *(state.name for state in model.states),
                    )
                ).casefold()
                if query and query not in search_text:
                    continue
                if model.is_derived:
                    label = f"◆  {model.name}    基于 {model.base_model}    ✓ 接口有效\n     {model.base_category} · 尚未装配"
                elif model.is_topology_model:
                    label = f"◆  {model.name}    项目自定义整体器件    ✓ 接口有效\n     {len(model.ports)} 个拓扑端口"
                else:
                    label = f"◆  {model.name}    项目自定义    ✓ 有效    使用 1\n     → {model.static_injection}"
                item = QtWidgets.QListWidgetItem(label)
                item.setData(QtCore.Qt.ItemDataRole.UserRole, ("custom", model.model_id))
                item.setForeground(QtGui.QColor("#2389d7"))
                item.setToolTip("保存在当前项目 .psid_gui/custom_models.json")
                self.list.addItem(item)

        derived_path = path in set(self._catalogue.values())
        if source != "官方模型" and (path in self.ADDABLE_PATHS or derived_path):
            add_kind = "topology" if path == ("项目自定义模型", "自定义整体器件") else "derived" if derived_path else "dynamic_injection"
            label = "＋  添加自定义整体器件" if add_kind == "topology" else f"＋  添加自定义{path[-1]}模型" if derived_path else "＋  添加自定义动态注入模型"
            add = QtWidgets.QListWidgetItem(label)
            add.setData(
                QtCore.Qt.ItemDataRole.UserRole,
                ("add", add_kind),
            )
            add.setForeground(QtGui.QColor("#1689d8"))
            font = add.font()
            font.setBold(True)
            add.setFont(font)
            add.setToolTip(
                "先定义拓扑端口，再使用 Julia 数学函数描述整体器件"
                if add_kind == "topology"
                else "继承本分类官方模型的接口并定义 Julia 数学函数"
                if derived_path
                else "创建由微分方程描述的项目自定义动态注入"
            )
            self.list.addItem(add)

        for row in range(self.list.count()):
            item = self.list.item(row)
            if item.data(QtCore.Qt.ItemDataRole.UserRole) == selected_data:
                self.list.setCurrentRow(row)
                break
        if self.list.currentRow() < 0 and self.list.count():
            self.list.setCurrentRow(0)
        self.status.setText(
            f"当前分类显示 {self.list.count()} 项 · 官方目录 {len(self._catalogue)} 个类型 · 项目自定义 {len(self.models)} 个"
        )
        if self._instantiation:
            self._refresh_instantiation()
        self._update_actions()

    def begin_instantiation(
        self,
        kind: str,
        component_path: str = "",
        base_category: str = "",
    ) -> None:
        self._instantiation = {
            "kind": kind,
            "component_path": component_path,
            "base_category": base_category,
        }
        self.setWindowTitle("模型管理器 — 选择要实例化的模型")
        self.instantiate_button.show()
        default_path = {
            "bus": ("网络元件模型", "母线"),
            "branch": ("网络元件模型", "支路"),
            "load": ("网络元件模型", "负荷"),
            "source": ("网络元件模型", "电源"),
            "generator": ("动态注入设备", "动态发电机"),
            "inverter": ("动态注入设备", "动态逆变器"),
            "injection": ("动态注入设备",),
        }.get(kind)
        if default_path:
            self._select_category(default_path)
        self.refresh()
        self.show()
        self.raise_()
        self.activateWindow()

    def end_instantiation(self) -> None:
        self._instantiation = None
        self.instantiate_button.hide()
        if self.project:
            self.setWindowTitle(f"模型管理器 — {self.project.root.name}")
        self.refresh()

    def _refresh_instantiation(self) -> None:
        assert self._instantiation is not None
        request = self._instantiation
        kind = request["kind"]
        base_category = request["base_category"]
        self.list.clear()
        if kind == "internal":
            for model_type, path in sorted(self._catalogue.items()):
                if path[-1] != base_category:
                    continue
                item = QtWidgets.QListWidgetItem(f"🔒  {model_type}\n     {base_category} · 官方模型")
                item.setData(QtCore.Qt.ItemDataRole.UserRole, ("instantiate_official", model_type))
                self.list.addItem(item)
            for model in sorted(self.models, key=lambda value: value.name.casefold()):
                if model.is_derived and model.base_category == base_category:
                    item = QtWidgets.QListWidgetItem(f"◆  {model.name}\n     {base_category} · 项目派生模型")
                    item.setForeground(QtGui.QColor("#2389d7"))
                    item.setData(QtCore.Qt.ItemDataRole.UserRole, ("instantiate_custom", model.model_id))
                    self.list.addItem(item)
        else:
            path = self._current_category()
            compatible = {
                "bus": {"ACBus"},
                "branch": {"Line", "Transformer2W"},
                "load": {"StandardLoad"},
                "source": {"Source"},
                "generator": {"DynamicGenerator"},
                "inverter": {"DynamicInverter"},
                "injection": {"StandardLoad", "Source", "DynamicGenerator", "DynamicInverter"},
            }.get(kind, set())
            for model_ref in sorted(compatible, key=str.casefold):
                model_path, label = self.DEVICE_MODELS[model_ref]
                if path and model_path[: len(path)] != path:
                    continue
                item = QtWidgets.QListWidgetItem(
                    f"🔒  {model_ref}\n     {label} · 官方设备模型"
                )
                item.setData(QtCore.Qt.ItemDataRole.UserRole, ("instantiate_builtin", model_ref))
                self.list.addItem(item)
            if kind in {"custom", "injection"} and path in {
                (),
                ("项目自定义模型",),
                ("项目自定义模型", "自定义整体器件"),
            }:
                for model in sorted(self.models, key=lambda value: value.name.casefold()):
                    if not model.is_topology_model:
                        continue
                    item = QtWidgets.QListWidgetItem(f"◆  {model.name}\n     {len(model.ports)} 端口 · 项目自定义整体器件")
                    item.setForeground(QtGui.QColor("#2389d7"))
                    item.setData(QtCore.Qt.ItemDataRole.UserRole, ("instantiate_custom", model.model_id))
                    self.list.addItem(item)
        if self.list.count():
            self.list.setCurrentRow(0)
        self.status.setText(f"实例化选择 · {self.list.count()} 个兼容模型")

    def _instantiate_selected(self) -> None:
        if not self._instantiation:
            return
        data = self._item_data()
        if not data or not data[0].startswith("instantiate_"):
            return
        descriptor = {**self._instantiation, "source": data[0], "model_ref": data[1]}
        if data[0] == "instantiate_builtin":
            descriptor["parameters"] = default_instance_parameters(data[1])
        if data[0] == "instantiate_custom":
            model = next((item for item in self.models if item.model_id == data[1]), None)
            if not model:
                return
            descriptor.update(
                name=model.name,
                ports=model.ports,
                base_category=model.base_category,
                parameters={
                    "base_power": model.base_power,
                    **{parameter.name: parameter.value for parameter in model.parameters},
                },
                backend_supported=all(port.domain == "AC_BUS" for port in model.ports),
            )
        self.instantiate_requested.emit(descriptor)
        self.hide()

    def _official_usage_count(self, model_type: str) -> int:
        if not self.project:
            return 0
        return sum(item.component_type.rsplit(".", 1)[-1] == model_type for item in self.project.equations)

    def _item_data(self) -> tuple[str, ...] | None:
        item = self.list.currentItem()
        value = item.data(QtCore.Qt.ItemDataRole.UserRole) if item else None
        return tuple(value) if value else None

    def _selected(self) -> CustomModel | None:
        data = self._item_data()
        if not data or data[0] != "custom":
            return None
        return next((model for model in self.models if model.model_id == data[1]), None)

    def _selection_changed(self, *_: Any) -> None:
        data = self._item_data()
        if not data:
            self.details.clear()
        elif data[0] == "official":
            self.details.setHtml(self._official_html(data[1]))
        elif data[0] == "custom":
            model = self._selected()
            self.details.setHtml(self._custom_html(model) if model else "")
        elif data[0].startswith("instantiate_"):
            self.details.setHtml(
                f"<h2>{html.escape(self.list.currentItem().text().splitlines()[0])}</h2>"
                "<p>点击右下角“实例化”，然后在画布中单击完成放置。</p>"
                "<p>内部组件会装配到当前强类型槽位；整体器件进入拓扑放置模式。</p>"
            )
        else:
            if data[1] == "derived":
                self.details.setHtml(
                    "<h2 style='color:#1689d8'>添加官方派生模型</h2>"
                    "<p>选择本分类的官方模型作为基础，继承并锁定其输入、状态和输出接口，"
                    "使用受限 Julia 代码定义数学函数。</p>"
                )
            elif data[1] == "topology":
                self.details.setHtml(
                    "<h2 style='color:#1689d8'>添加自定义整体器件</h2>"
                    "<p>第一步定义端口，第二步定义参数、状态和 Julia 数学函数。</p>"
                )
            else:
                self.details.setHtml(
                    "<h2 style='color:#1689d8'>添加自定义动态注入模型</h2>"
                    "<p>创建项目级方程模型并绑定到尚未具有动态模型的静态注入设备。</p>"
                    "<p>支持微分状态、对角质量矩阵、参数、网络电流输出和稳态初始化。</p>"
                )
        self._update_actions()

    def _official_html(self, model_type: str) -> str:
        implementations = self.library.lookup(model_type)
        path = " / ".join(self._catalogue.get(model_type, ("其他模型",)))
        sections = [
            "<style>body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;}"
            "code{word-break:break-all;}pre{white-space:pre-wrap;}"
            ".badge{background:rgba(119,119,119,0.2);border-radius:4px;padding:2px 6px;}</style>",
            f"<h2>🔒 {html.escape(model_type)}</h2>",
            "<p><span class='badge'>官方模型</span></p>",
            f"<p><b>层级：</b>{html.escape(path)}<br><b>当前项目使用：</b>{self._official_usage_count(model_type)} 次</p>",
        ]
        for implementation in implementations:
            equations = (*implementation.derivatives, *implementation.algebraic)
            sections.append(
                f"<h3>{html.escape(implementation.category)}</h3>"
                f"<p><b>方程入口：</b><code>{html.escape(implementation.entrypoint)}</code><br>"
                f"<b>源码：</b><code>{html.escape(implementation.source)}</code></p>"
                f"<pre>{html.escape(chr(10).join(equations) if equations else '无动态方程')}</pre>"
            )
            if implementation.mass_matrix:
                sections.append(
                    "<h4>质量矩阵</h4><pre>"
                    + html.escape("\n".join(implementation.mass_matrix))
                    + "</pre>"
                )
        return "".join(sections)

    @staticmethod
    def _custom_html(model: CustomModel) -> str:
        if model.is_derived:
            states = ", ".join(html.escape(item.name) for item in model.states) or "无"
            inputs = ", ".join(html.escape(item) for item in model.interface_inputs) or "无"
            outputs = ", ".join(html.escape(item) for item in model.interface_outputs) or "无"
            return (
                "<style>body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;}"
                "code{word-break:break-all;}pre{white-space:pre-wrap;}"
                ".custom{color:#1689d8;font-weight:600;}</style>"
                f"<h2>◆ {html.escape(model.name)}</h2>"
                f"<p class='custom'>官方派生模型 · 基于 {html.escape(model.base_model)} · ✓ 接口有效</p>"
                f"<p>{html.escape(model.description) or '无说明'}</p>"
                f"<p><b>分类：</b>{html.escape(model.base_category)}<br>"
                f"<b>输入：</b>{inputs}<br><b>状态：</b>{states}<br><b>输出：</b>{outputs}</p>"
                "<h3>equations! 函数体</h3>"
                f"<pre>{html.escape(model.julia_body)}</pre>"
                + (
                    f"<h3>辅助函数</h3><pre>{html.escape(model.helper_functions)}</pre>"
                    if model.helper_functions
                    else ""
                )
                + f"<p><b>存储：</b><code>{html.escape(str(MODEL_FILE))}</code></p>"
            )
        if model.is_topology_model:
            ports = "".join(
                f"<tr><td>{html.escape(port.name)}</td><td>{html.escape(port.domain)}</td>"
                f"<td>{html.escape(port.role)}</td><td>{'是' if port.required else '否'}</td></tr>"
                for port in model.ports
            )
            return (
                "<style>body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;}"
                "table{border-collapse:collapse;width:100%}td,th{padding:4px;border-bottom:1px solid #8884}"
                "pre{white-space:pre-wrap}.custom{color:#1689d8;font-weight:600}</style>"
                f"<h2>◆ {html.escape(model.name)}</h2><p class='custom'>项目自定义整体器件</p>"
                f"<p>{html.escape(model.description) or '无说明'}</p>"
                f"<table><tr><th>端口</th><th>域</th><th>角色</th><th>必需</th></tr>{ports}</table>"
                f"<h3>equations! 函数体</h3><pre>{html.escape(model.julia_body)}</pre>"
                f"<p><b>存储：</b><code>{html.escape(str(MODEL_FILE))}</code></p>"
            )
        equations = "<br>".join(
            f"<code>{state.mass:g} · d({html.escape(state.name)})/dt = {html.escape(state.rhs)}</code>"
            for state in model.states
        )
        parameters = ", ".join(
            f"{html.escape(item.name)}={item.value:g}{(' ' + html.escape(item.unit)) if item.unit else ''}"
            for item in model.parameters
        ) or "无"
        return (
            "<style>body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;}code{word-break:break-all;}"
            ".custom{color:#1689d8;font-weight:600;}</style>"
            f"<h2>◆ {html.escape(model.name)}</h2><p class='custom'>项目自定义 · ✓ 有效</p>"
            f"<p>{html.escape(model.description) or '无说明'}</p>"
            f"<p><b>绑定设备：</b>{html.escape(model.static_injection)}<br>"
            f"<b>设备基准：</b>{model.base_power:g} MVA<br><b>初始化：</b>{html.escape(model.initialization)}</p>"
            f"<h3>微分方程</h3><p>{equations}</p><h3>参数</h3><p>{parameters}</p>"
            f"<h3>网络输出</h3><p><code>i_r = {html.escape(model.outputs['i_r'])}</code><br>"
            f"<code>i_i = {html.escape(model.outputs['i_i'])}</code></p>"
            f"<p><b>存储：</b><code>{html.escape(str(MODEL_FILE))}</code></p>"
        )

    def _activate_item(self, *_: Any) -> None:
        data = self._item_data()
        if not data:
            return
        if data[0].startswith("instantiate_"):
            self._instantiate_selected()
        elif data[0] == "add":
            self.create_topology_model() if data[1] == "topology" else self.create_model()
        elif data[0] == "custom":
            self.edit_selected()

    def _show_context_menu(self, position: QtCore.QPoint) -> None:
        data = self._item_data()
        if not data:
            return
        menu = QtWidgets.QMenu(self)
        if data[0].startswith("instantiate_"):
            menu.addAction("实例化", self._instantiate_selected)
            menu.exec(self.list.mapToGlobal(position))
            return
        if data[0] == "official":
            inherit = menu.addAction("继承为自定义模型…")
            inherit.setEnabled(self._editing_enabled)
            inherit.triggered.connect(lambda: self.inherit_model(data[1]))
            menu.addSeparator()
            copy_name = menu.addAction("复制 Julia 类型名")
            copy_name.triggered.connect(
                lambda: QtWidgets.QApplication.clipboard().setText(data[1])
            )
            locate = menu.addAction("显示当前项目中的使用次数")
            locate.triggered.connect(
                lambda: self.status.setText(
                    f"{data[1]}：当前项目使用 {self._official_usage_count(data[1])} 次"
                )
            )
            highlight = menu.addAction("转到模型")
            highlight.setEnabled(bool(self._official_usage_count(data[1])))
            highlight.triggered.connect(lambda: self.locate_requested.emit(data[1]))
            implementations = self.library.lookup(data[1])
            source = implementations[0].source if implementations else ""
            source_path = Path(__file__).resolve().parent.parent / "PowerSimulationsDynamics.jl" / source
            open_source = menu.addAction("打开 Julia 源码")
            open_source.setEnabled(source_path.is_file())
            open_source.triggered.connect(
                lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(source_path)))
            )
        elif data[0] == "custom":
            menu.addAction("编辑", self.edit_selected).setEnabled(self._editing_enabled)
            menu.addAction("复制", self.duplicate_selected).setEnabled(self._editing_enabled)
            model = self._selected()
            locate = menu.addAction("转到绑定器件")
            locate.setEnabled(model is not None and model.model_kind == "equation")
            if model and model.model_kind == "equation":
                locate.triggered.connect(
                    lambda: self.locate_requested.emit(model.static_injection)
                )
            menu.addAction("删除", self.delete_selected).setEnabled(self._editing_enabled)
        else:
            menu.addAction("添加自定义动态注入模型", self.create_model).setEnabled(
                self._editing_enabled
            )
            menu.addAction("添加自定义整体器件", self.create_topology_model).setEnabled(
                self._editing_enabled
            )
        menu.exec(self.list.mapToGlobal(position))

    def _update_actions(self) -> None:
        data = self._item_data() if hasattr(self, "list") else None
        custom = bool(data and data[0] == "custom")
        official = bool(data and data[0] == "official")
        add_derived = bool(data and data == ("add", "derived"))
        add_topology = bool(data and data == ("add", "topology"))
        can_create = self._editing_enabled and (
            official or add_derived or add_topology or bool(self._available_injections())
        )
        self.create_button.setText(
            "新建自定义整体器件…" if add_topology else "继承为自定义模型…" if official or add_derived else "新建自定义模型…"
        )
        self.create_button.setEnabled(can_create)
        self.edit_button.setEnabled(self._editing_enabled and custom)
        self.duplicate_button.setEnabled(
            self._editing_enabled
            and custom
            and (bool(self._available_injections()) or bool(self._selected() and self._selected().is_derived))
        )
        self.delete_button.setEnabled(self._editing_enabled and custom)
        self.create_topology_button.setEnabled(self._editing_enabled)
        if self._instantiation:
            data = self._item_data()
            self.instantiate_button.setEnabled(bool(data and data[0].startswith("instantiate_")))

    def _available_injections(self, current: CustomModel | None = None) -> list[str]:
        if not self.project:
            return []
        occupied = {
            model.static_injection
            for model in self.models
            if model is not current and not model.is_derived
        }
        return [
            item.name
            for item in self.project.injections
            if (not item.dynamic_type or (current and item.name == current.static_injection))
            and item.name not in occupied
        ]

    @QtCore.Slot()
    def create_model(self) -> None:
        if not self._editing_enabled:
            self.status.setText("请先启用编辑模式")
            return
        data = self._item_data()
        if data == ("add", "topology"):
            self.create_topology_model()
            return
        if data and data[0] == "official":
            self.inherit_model(data[1])
            return
        if data == ("add", "derived"):
            path = self._current_category()
            candidates = [
                model_type
                for model_type, model_path in self._catalogue.items()
                if model_path == path
            ]
            if candidates:
                choice, accepted = QtWidgets.QInputDialog.getItem(
                    self, "选择基础官方模型", "基础模型", sorted(candidates), 0, False
                )
                if accepted:
                    self.inherit_model(choice)
            return
        choices = self._available_injections()
        if not choices:
            QtWidgets.QMessageBox.information(self, "无法新建", "没有尚未绑定动态模型的静态注入设备。")
            return
        self._edit(self._new_model(choices[0]), True)

    def create_topology_model(self) -> None:
        if not self._editing_enabled:
            return
        port = PortDefinition("terminal", "电气端口")
        model = CustomModel(
            model_id=str(uuid.uuid4()),
            name="新建自定义整体器件",
            static_injection="",
            description="",
            base_power=100.0,
            initialization="fixed",
            states=(CustomState("x", "", 1.0, 0.0),),
            parameters=(),
            outputs={},
            model_kind="topology",
            base_category="自定义整体器件",
            interface_inputs=("terminal_v_r", "terminal_v_i"),
            interface_outputs=("terminal_i_r", "terminal_i_i"),
            julia_body="dx.x = 0.0\ny.terminal_i_r = 0.0\ny.terminal_i_i = 0.0",
            ports=(port,),
        )
        dialog = TopologyModelEditor(model, self)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        self._commit((*self.models, dialog.value()))

    def inherit_model(self, model_type: str) -> None:
        if not self._editing_enabled:
            self.status.setText("请先启用编辑模式")
            return
        implementations = self.library.lookup(model_type)
        path = self._catalogue.get(model_type, ())
        category_hint = path[-1] if path else ""
        category = (
            category_hint
            if category_hint in COMPONENT_INPUTS
            else implementations[0].category
            if implementations
            else ""
        )
        model = inherited_model_template(
            model_type, category, implementations, self.project
        )
        self._edit(model, True)

    def create_for(self, injection_name: str) -> None:
        if not self._editing_enabled or injection_name not in self._available_injections():
            return
        self._edit(self._new_model(injection_name), True)

    def _new_model(self, injection_name: str) -> CustomModel:
        assert self.project is not None
        injection = next(item for item in self.project.injections if item.name == injection_name)
        sign = -1 if "load" in injection.injection_type.casefold() else 1
        return CustomModel.new(injection_name, sign)

    @QtCore.Slot()
    def edit_selected(self) -> None:
        model = self._selected()
        if self._editing_enabled and model:
            self._edit(model, False)

    def _edit(self, model: CustomModel, creating: bool) -> None:
        dialog = (
            InheritedModelEditor(model, self)
            if model.is_derived
            else TopologyModelEditor(model, self)
            if model.is_topology_model
            else CustomModelEditor(
                model,
                self._available_injections(None if creating else model),
                self,
            )
        )
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        updated = dialog.value()
        models = list(self.models)
        if creating:
            models.append(updated)
        else:
            models[models.index(model)] = updated
        self._commit(tuple(models))

    @QtCore.Slot()
    def duplicate_selected(self) -> None:
        model = self._selected()
        choices = self._available_injections()
        if not self._editing_enabled or not model or (model.model_kind == "equation" and not choices):
            return
        duplicate = replace(
            model,
            model_id=str(uuid.uuid4()),
            name=f"{model.name} 副本",
            static_injection=choices[0] if model.model_kind == "equation" else "",
        )
        self._edit(duplicate, True)

    @QtCore.Slot()
    def delete_selected(self) -> None:
        model = self._selected()
        if not self._editing_enabled or not model:
            return
        message = (
            f"删除项目派生模型“{model.name}”？"
            if model.is_derived or model.is_topology_model
            else f"“{model.name}”当前绑定到 {model.static_injection}。\n"
            "删除会同时解除该设备的自定义动态模型绑定，是否继续？"
        )
        if QtWidgets.QMessageBox.question(self, "删除模型", message) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self._commit(tuple(item for item in self.models if item is not model))

    def _commit(self, models: tuple[CustomModel, ...]) -> None:
        assert self.project is not None
        try:
            names = [model.name.casefold() for model in models]
            if len(names) != len(set(names)):
                raise CustomModelError("项目自定义模型名称不能重复")
            for model in models:
                validate_model(model, {item.name for item in self.project.injections})
            CustomModelStore.save(self.project.root, models)
        except (CustomModelError, OSError) as error:
            QtWidgets.QMessageBox.critical(self, "无法保存模型", str(error))
            return
        self.models = models
        self._populate_categories()
        selected = next((item for item in models if item.is_derived), None)
        self._select_category(
            self._catalogue.get(selected.base_model, ("其他模型",))
            if selected
            else ("项目自定义模型", "自定义动态注入")
        )
        self.refresh()
        self.models_changed.emit(models)
