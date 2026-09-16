from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from NodeGraphQt import BaseNode, NodeGraph
from NodeGraphQt.constants import PipeEnum, PortTypeEnum
from NodeGraphQt.qgraphics.node_base import NodeItem
from NodeGraphQt.qgraphics.pipe import PipeItem
from NodeGraphQt.qgraphics.port import CustomPortItem
from NodeGraphQt.widgets.viewer import NodeViewer
from Qt import QtCore, QtGui, QtWidgets

from .layout import device_positions, topology_positions
from .models import Branch, Bus, EquationComponent, ExportedProject, Injection
from .editor import EditorConnection, EditorDocument, EditorInstance, PortDefinition


BUS_COLORS = {"REF": (185, 74, 72), "PV": (48, 137, 186), "PQ": (73, 151, 112)}
ROLE_COLORS = {
    "dynamic_generator": (182, 119, 48),
    "dynamic_inverter": (126, 91, 184),
    "machine": (49, 130, 189),
    "shaft": (71, 148, 110),
    "avr": (190, 108, 54),
    "governor": (165, 91, 80),
    "pss": (104, 120, 136),
    "converter": (125, 87, 178),
    "outer_control": (155, 83, 156),
    "inner_control": (112, 92, 174),
    "filter": (67, 140, 158),
    "dc_source": (178, 139, 52),
    "frequency_estimator": (90, 126, 170),
    "custom_dynamic": (30, 152, 138),
    "custom_state": (45, 125, 118),
}
DISABLED_COLOR = (92, 96, 104)
PIPE_STUB_LENGTH = 20.0
BUS_PORT_MIN_DISTANCE = 20.0
ICON_SIZE = 38
ICON_DIRECTORY = (
    Path(__file__).resolve().parent.parent
    / "power_system_component_icons"
    / "SVG"
)

EDITOR_DEVICE_RECIPES = {
    "DynamicGenerator": (
        ("", "dynamic_generator", "DynamicGenerator{AndersonFouadMachine, SingleMass, AVRTypeII, TGTypeI, PSSFixed}", ("ψq", "ψd", "eq_p", "ed_p", "eq_pp", "ed_pp", "δ", "ω", "Vf", "Vr1", "Vr2", "Vm", "x_g1", "x_g2", "x_g3"), ("device!",), "models/device.jl"),
        ("avr", "avr", "AVRTypeII", ("Vf", "Vr1", "Vr2", "Vm"), ("mdl_avr_ode!",), "models/generator_models/avr_models.jl"),
        ("machine", "machine", "AndersonFouadMachine", ("ψq", "ψd", "eq_p", "ed_p", "eq_pp", "ed_pp"), ("mdl_machine_ode!",), "models/generator_models/machine_models.jl"),
        ("prime_mover", "governor", "TGTypeI", ("x_g1", "x_g2", "x_g3"), ("mdl_tg_ode!",), "models/generator_models/tg_models.jl"),
        ("pss", "pss", "PSSFixed", (), ("mdl_pss_ode!",), "models/generator_models/pss_models.jl"),
        ("shaft", "shaft", "SingleMass", ("δ", "ω"), ("mdl_shaft_ode!",), "models/generator_models/shaft_models.jl"),
    ),
    "DynamicInverter": (
        ("", "dynamic_inverter", "DynamicInverter{AverageConverter, OuterControl{ActivePowerDroop, ReactivePowerDroop}, VoltageModeControl, FixedDCSource, FixedFrequency, LCLFilter, Nothing}", ("θ_oc", "p_oc", "q_oc", "ξd_ic", "ξq_ic", "γd_ic", "γq_ic", "ϕd_ic", "ϕq_ic", "ir_cnv", "ii_cnv", "vr_filter", "vi_filter", "ir_filter", "ii_filter"), ("device!",), "models/device.jl"),
        ("converter", "converter", "AverageConverter", (), ("mdl_converter_ode!",), "models/inverter_models/converter_models.jl"),
        ("dc_source", "dc_source", "FixedDCSource", (), ("mdl_DCside_ode!",), "models/inverter_models/DCside_models.jl"),
        ("filter", "filter", "LCLFilter", ("ir_cnv", "ii_cnv", "vr_filter", "vi_filter", "ir_filter", "ii_filter"), ("mdl_filter_ode!",), "models/inverter_models/filter_models.jl"),
        ("freq_estimator", "frequency_estimator", "FixedFrequency", (), ("mdl_freq_estimator_ode!",), "models/inverter_models/frequency_estimator_models.jl"),
        ("inner_control", "inner_control", "VoltageModeControl", ("ξd_ic", "ξq_ic", "γd_ic", "γq_ic", "ϕd_ic", "ϕq_ic"), ("mdl_inner_ode!",), "models/inverter_models/inner_control_models.jl"),
        ("outer_control", "outer_control", "OuterControl{ActivePowerDroop, ReactivePowerDroop}", ("θ_oc", "p_oc", "q_oc"), ("mdl_outer_ode!",), "models/inverter_models/outer_control_models.jl"),
        ("outer_control/active_power_control", "outer_control", "ActivePowerDroop", ("θ_oc", "p_oc"), ("mdl_outer_ode!",), "models/inverter_models/outer_control_models.jl"),
        ("outer_control/reactive_power_control", "outer_control", "ReactivePowerDroop", ("q_oc",), ("mdl_outer_ode!",), "models/inverter_models/outer_control_models.jl"),
    ),
}


def editor_device_components(
    project: ExportedProject, instance: EditorInstance
) -> tuple[EquationComponent, ...]:
    """Build the inspectable hierarchy for a library-created editor device."""
    prefix = f"editor-device:{instance.instance_id}"
    recipe = EDITOR_DEVICE_RECIPES.get(instance.model_ref)
    if recipe:
        return tuple(
            EquationComponent(
                prefix if not suffix else f"{prefix}/{suffix}",
                role,
                component_type,
                states,
                entrypoints,
                source,
                {"editor_instance_key": instance.key, "parameter_path": suffix},
            )
            for suffix, role, component_type, states, entrypoints, source in recipe
        )
    model = next(
        (
            value
            for value in project.custom_models
            if value.model_id == instance.model_ref and value.is_topology_model
        ),
        None,
    )
    if not model:
        return ()
    root = EquationComponent(
        prefix,
        "custom_dynamic",
        model.name,
        tuple(state.name for state in model.states),
        ("network_device!",),
        ".psid_gui/custom_models.json",
        {"editor_instance_key": instance.key, "parameter_path": ""},
    )
    states = tuple(
        EquationComponent(
            f"{prefix}/states/{state.name}",
            "custom_state",
            "DifferentialEquation",
            (state.name,),
            ("network_device!",),
            ".psid_gui/custom_models.json",
            {"rhs": state.rhs, "mass": state.mass, "initial": state.initial},
        )
        for state in model.states
    )
    return (root, *states)


def editor_component_runtime(instance: EditorInstance, component: EquationComponent) -> dict[str, Any] | None:
    if component.role == "custom_state":
        return component.raw
    suffix = str(component.raw.get("parameter_path", ""))
    if not suffix:
        return instance.parameters
    parameter_key = {
        "freq_estimator": "frequency_estimator",
        "outer_control/active_power_control": "active_power_control",
        "outer_control/reactive_power_control": "reactive_power_control",
    }.get(suffix, suffix)
    if suffix == "outer_control":
        return {
            "active_power_control": instance.parameters.get("active_power_control", {}),
            "reactive_power_control": instance.parameters.get("reactive_power_control", {}),
        }
    value = instance.parameters.get(parameter_key)
    return value if isinstance(value, dict) else None


def topology_icon_name(payload: Bus | Branch | Injection) -> str:
    if isinstance(payload, Bus):
        return "busbar"
    if isinstance(payload, Branch):
        kind = payload.branch_type.casefold()
        if "transformer" in kind:
            return "transformer_2w"
        return "cable" if "line" in kind or "branch" in kind or "cable" in kind else "node_open"

    dynamic = payload.dynamic_type.casefold()
    static = payload.injection_type.casefold()
    combined = f"{dynamic} {static}"
    if "dynamicgenerator" in dynamic:
        return "synchronous_generator"
    if "inductionmachine" in combined:
        return "motor"
    if "load" in combined:
        return "load"
    if "solar" in combined or "photovoltaic" in combined:
        return "solar_pv"
    if "wind" in combined:
        return "wind_turbine"
    if (
        "dynamicinverter" in dynamic
        or "source" in combined
        or "csvgn" in combined
        or "renewable" in combined
    ):
        return "generic_ac_source"
    if "thermal" in static or "generator" in static or "distributedgeneration" in combined:
        return "generator"
    return "node_open"


def editor_icon_path(model_ref: str) -> str | None:
    """Return the topology icon for a whole-device library definition."""
    icon_name = {
        "ACBus": "busbar",
        "Line": "cable",
        "Transformer2W": "transformer_2w",
        "DynamicGenerator": "synchronous_generator",
        "DynamicInverter": "generic_ac_source",
        "StandardLoad": "load",
        "Source": "generic_ac_source",
    }.get(model_ref)
    if not icon_name:
        return None
    path = ICON_DIRECTORY / f"{icon_name}.svg"
    return str(path) if path.is_file() else None


def topology_icon_path(payload: Bus | Branch | Injection) -> str | None:
    path = ICON_DIRECTORY / f"{topology_icon_name(payload)}.svg"
    return str(path) if path.is_file() else None


class EdgeNormalPipeItem(PipeItem):
    structural = False

    @staticmethod
    def port_normal(port: Any) -> QtCore.QPointF:
        center = port.pos() + port.boundingRect().center()
        rect = port.node.boundingRect()
        sides = (
            (abs(center.x() - rect.left()), QtCore.QPointF(-1.0, 0.0)),
            (abs(center.x() - rect.right()), QtCore.QPointF(1.0, 0.0)),
            (abs(center.y() - rect.top()), QtCore.QPointF(0.0, -1.0)),
            (abs(center.y() - rect.bottom()), QtCore.QPointF(0.0, 1.0)),
        )
        return min(sides, key=lambda item: item[0])[1]

    @staticmethod
    def port_center(port: Any) -> QtCore.QPointF:
        return port.scenePos() + port.boundingRect().center()

    def draw_path(self, start_port: Any, end_port: Any = None, cursor_pos: Any = None) -> None:
        if not start_port or not end_port:
            return
        if self.input_port and self.output_port:
            visible = all(
                (
                    self.input_port.isVisible(),
                    self.output_port.isVisible(),
                    self.input_port.node.isVisible(),
                    self.output_port.node.isVisible(),
                )
            )
            self.setVisible(visible)
            if not visible:
                return

        start = self.port_center(start_port)
        end = self.port_center(end_port)
        start_normal = self.port_normal(start_port)
        end_normal = self.port_normal(end_port)
        start_stub = start + start_normal * PIPE_STUB_LENGTH
        end_stub = end + end_normal * PIPE_STUB_LENGTH
        distance = QtCore.QLineF(start_stub, end_stub).length()
        tangent = max(30.0, min(120.0, distance * 0.35))

        path = QtGui.QPainterPath(start)
        path.lineTo(start_stub)
        path.cubicTo(
            start_stub + start_normal * tangent,
            end_stub + end_normal * tangent,
            end_stub,
        )
        path.lineTo(end)
        self.setPath(path)
        self._draw_direction_pointer()

    def _draw_direction_pointer(self) -> None:
        if self.structural:
            self._dir_pointer.setVisible(False)
            return
        super()._draw_direction_pointer()


class EdgeNormalNodeViewer(NodeViewer):
    def establish_connection(self, start_port: Any, end_port: Any) -> None:
        pipe = EdgeNormalPipeItem()
        self.scene().addItem(pipe)
        pipe.set_connections(start_port, end_port)
        pipe.draw_path(pipe.input_port, pipe.output_port)
        if start_port.node.selected or end_port.node.selected:
            pipe.highlight()
        if not start_port.node.visible or not end_port.node.visible:
            pipe.hide()

    def sceneMouseMoveEvent(self, event: QtWidgets.QGraphicsSceneMouseEvent) -> None:
        self._show_bus_connection_handles_near(event.scenePos())
        super().sceneMouseMoveEvent(event)

    def sceneMouseReleaseEvent(self, event: QtWidgets.QGraphicsSceneMouseEvent) -> None:
        super().sceneMouseReleaseEvent(event)
        if not self._LIVE_PIPE.isVisible():
            self._set_temporary_bus_handles_visible(None)

    def _show_bus_connection_handles_near(self, scene_pos: QtCore.QPointF) -> None:
        start = self._start_port
        if not self._LIVE_PIPE.isVisible() or not start:
            return
        start_node = getattr(start.node, "viewer_node", None)
        if not start_node or start_node.is_bus_node:
            return
        target = None
        proximity = QtCore.QRectF(scene_pos.x() - 36.0, scene_pos.y() - 36.0, 72.0, 72.0)
        for item in self.scene().items(proximity):
            node = getattr(item, "viewer_node", None)
            if node and node.is_bus_node and node.bus_connection_handles_enabled:
                target = node
                break
        self._set_temporary_bus_handles_visible(
            target, self.compatible_bus_handle_type()
        )

    def compatible_bus_handle_type(self) -> str:
        if self._LIVE_PIPE.isVisible() and self._start_port:
            return (
                PortTypeEnum.OUT.value
                if self._start_port.port_type == PortTypeEnum.IN.value
                else PortTypeEnum.IN.value
            )
        return PortTypeEnum.OUT.value

    def _set_temporary_bus_handles_visible(
        self, target: Any, port_type: str | None = None
    ) -> None:
        port_type = port_type or PortTypeEnum.OUT.value
        for item in self.scene().items():
            node = getattr(item, "viewer_node", None)
            if not node or not node.is_bus_node:
                continue
            for port in (*node.input_ports(), *node.output_ports()):
                if getattr(port.view, "temporary_connection_handle", False):
                    port.view.setVisible(
                        node is target and port.view.port_type == port_type
                    )


def _paint_boundary_port(
    painter: QtGui.QPainter,
    rect: QtCore.QRectF,
    info: dict[str, Any],
) -> None:
    painter.save()
    color = QtGui.QColor(89, 184, 255) if info["hovered"] else QtGui.QColor(205, 220, 232)
    painter.setPen(QtGui.QPen(color, 1.8))
    painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
    painter.drawEllipse(rect)
    painter.restore()


def _paint_structural_port(
    painter: QtGui.QPainter,
    rect: QtCore.QRectF,
    info: dict[str, Any],
) -> None:
    """Draw hierarchy-only endpoints differently from signal ports."""
    painter.save()
    fill = QtGui.QColor(174, 181, 190) if info["hovered"] else QtGui.QColor(112, 118, 126)
    border = QtGui.QColor(225, 229, 234) if info["hovered"] else QtGui.QColor(155, 162, 171)
    painter.setPen(QtGui.QPen(border, 1.5))
    painter.setBrush(fill if info["connected"] else QtCore.Qt.BrushStyle.NoBrush)
    painter.drawRect(rect.adjusted(0.8, 0.8, -0.8, -0.8))
    painter.restore()


def _paint_bus_port(
    painter: QtGui.QPainter,
    rect: QtCore.QRectF,
    info: dict[str, Any],
) -> None:
    """Draw a movable Bus terminal with the normal signal-port appearance."""
    painter.save()
    fill = QtGui.QColor(89, 184, 255) if info["hovered"] else QtGui.QColor(49, 61, 70)
    border = QtGui.QColor(132, 222, 255) if info["hovered"] else QtGui.QColor(30, 205, 190)
    painter.setPen(QtGui.QPen(border, 1.8))
    painter.setBrush(border if info["connected"] else fill)
    painter.drawEllipse(rect.adjusted(0.8, 0.8, -0.8, -0.8))
    painter.restore()


class DraggableBusPortItem(CustomPortItem):
    """A Bus terminal constrained to the node perimeter with collision spacing."""

    def __init__(self, parent: Any = None, paint_func: Any = None) -> None:
        super().__init__(parent, paint_func)
        self.bus_edge_port = True
        self.edge_drag_enabled = False
        self.temporary_connection_handle = False
        self.manual_edge_position = False
        self.edge_position: tuple[float, float] | None = None
        self._dragging_edge = False
        self._drag_before: tuple[float, float] | None = None

    def normalized_position(self) -> tuple[float, float]:
        rect = self.node.boundingRect()
        center = self.pos() + self.boundingRect().center()
        return (
            center.x() / max(rect.width(), 1.0),
            center.y() / max(rect.height(), 1.0),
        )

    def set_normalized_position(
        self, position: tuple[float, float], *, manual: bool = True
    ) -> None:
        rect = self.node.boundingRect()
        point = QtCore.QPointF(
            max(0.0, min(1.0, position[0])) * rect.width(),
            max(0.0, min(1.0, position[1])) * rect.height(),
        )
        self._set_center(self._nearest_edge(point, avoid_collisions=True))
        self.edge_position = self.normalized_position()
        self.manual_edge_position = manual

    def snap_current_to_edge(self) -> None:
        center = self.pos() + self.boundingRect().center()
        self._set_center(self._nearest_edge(center, avoid_collisions=True))
        self.edge_position = self.normalized_position()

    def mousePressEvent(self, event: QtWidgets.QGraphicsSceneMouseEvent) -> None:
        if (
            self.edge_drag_enabled
            and event.button() == QtCore.Qt.MouseButton.LeftButton
            and not self.temporary_connection_handle
        ):
            self._dragging_edge = True
            self._drag_before = self.normalized_position()
            self.node.setFlag(
                QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False
            )
            self.setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QtWidgets.QGraphicsSceneMouseEvent) -> None:
        if not self._dragging_edge:
            super().mouseMoveEvent(event)
            return
        local = self.node.mapFromScene(event.scenePos())
        self._set_center(self._nearest_edge(local, avoid_collisions=True))
        self.edge_position = self.normalized_position()
        self.manual_edge_position = True
        self.node.viewer_node._align_port_labels()
        event.accept()

    def mouseReleaseEvent(self, event: QtWidgets.QGraphicsSceneMouseEvent) -> None:
        if not self._dragging_edge:
            super().mouseReleaseEvent(event)
            return
        before = self._drag_before
        after = self.normalized_position()
        self._dragging_edge = False
        self._drag_before = None
        self.node.setFlag(
            QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True
        )
        self.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
        callback = self.node.viewer_node.port_move_finished
        if callback and before != after:
            callback(self.node.viewer_node, self.name, before, after)
        event.accept()

    def hoverEnterEvent(self, event: QtWidgets.QGraphicsSceneHoverEvent) -> None:
        if self.edge_drag_enabled and not self.temporary_connection_handle:
            self.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event: QtWidgets.QGraphicsSceneHoverEvent) -> None:
        self.unsetCursor()
        super().hoverLeaveEvent(event)

    def _set_center(self, point: QtCore.QPointF) -> None:
        self.setPos(point - self.boundingRect().center())
        self.redraw_connected_pipes()

    def _nearest_edge(
        self, point: QtCore.QPointF, *, avoid_collisions: bool
    ) -> QtCore.QPointF:
        rect = self.node.boundingRect()
        x = max(rect.left(), min(rect.right(), point.x()))
        y = max(rect.top(), min(rect.bottom(), point.y()))
        candidates = (
            (abs(point.x() - rect.left()), QtCore.QPointF(rect.left(), y)),
            (abs(point.x() - rect.right()), QtCore.QPointF(rect.right(), y)),
            (abs(point.y() - rect.top()), QtCore.QPointF(x, rect.top())),
            (abs(point.y() - rect.bottom()), QtCore.QPointF(x, rect.bottom())),
        )
        edge = min(candidates, key=lambda item: item[0])[1]
        scalar = self._point_to_scalar(edge, rect)
        if avoid_collisions:
            scalar = self._separated_scalar(scalar, rect)
        return self._scalar_to_point(scalar, rect)

    @staticmethod
    def _point_to_scalar(point: QtCore.QPointF, rect: QtCore.QRectF) -> float:
        width, height = rect.width(), rect.height()
        distances = (
            (abs(point.y() - rect.top()), point.x() - rect.left()),
            (abs(point.x() - rect.right()), width + point.y() - rect.top()),
            (abs(point.y() - rect.bottom()), width + height + rect.right() - point.x()),
            (abs(point.x() - rect.left()), 2.0 * width + height + rect.bottom() - point.y()),
        )
        return min(distances, key=lambda item: item[0])[1]

    @staticmethod
    def _scalar_to_point(value: float, rect: QtCore.QRectF) -> QtCore.QPointF:
        width, height = rect.width(), rect.height()
        perimeter = max(2.0 * (width + height), 1.0)
        value %= perimeter
        if value <= width:
            return QtCore.QPointF(rect.left() + value, rect.top())
        if value <= width + height:
            return QtCore.QPointF(rect.right(), rect.top() + value - width)
        if value <= 2.0 * width + height:
            return QtCore.QPointF(rect.right() - (value - width - height), rect.bottom())
        return QtCore.QPointF(rect.left(), rect.bottom() - (value - 2.0 * width - height))

    def _separated_scalar(self, wanted: float, rect: QtCore.QRectF) -> float:
        perimeter = max(2.0 * (rect.width() + rect.height()), 1.0)
        occupied = []
        node = self.node.viewer_node
        for port in (*node.input_ports(), *node.output_ports()):
            view = port.view
            if view is self or not getattr(view, "bus_edge_port", False):
                continue
            if getattr(view, "temporary_connection_handle", False):
                continue
            center = view.pos() + view.boundingRect().center()
            occupied.append(self._point_to_scalar(center, rect))
        if not occupied:
            return wanted

        def clearance(value: float) -> float:
            return min(
                min(abs(value - item), perimeter - abs(value - item))
                for item in occupied
            )

        if clearance(wanted) >= BUS_PORT_MIN_DISTANCE:
            return wanted
        step = 2.0
        candidates = [wanted]
        for offset in range(1, int(perimeter / step) + 1):
            candidates.extend(((wanted + offset * step) % perimeter, (wanted - offset * step) % perimeter))
            for candidate in candidates[-2:]:
                if clearance(candidate) >= BUS_PORT_MIN_DISTANCE:
                    return candidate
        return max(candidates, key=clearance)


class ResizeHandle(QtWidgets.QGraphicsItem):
    SIZE = 8.0
    CURSORS = {
        "nw": QtCore.Qt.CursorShape.SizeFDiagCursor,
        "n": QtCore.Qt.CursorShape.SizeVerCursor,
        "ne": QtCore.Qt.CursorShape.SizeBDiagCursor,
        "e": QtCore.Qt.CursorShape.SizeHorCursor,
        "se": QtCore.Qt.CursorShape.SizeFDiagCursor,
        "s": QtCore.Qt.CursorShape.SizeVerCursor,
        "sw": QtCore.Qt.CursorShape.SizeBDiagCursor,
        "w": QtCore.Qt.CursorShape.SizeHorCursor,
    }

    def __init__(self, direction: str, parent: "ReadOnlyNodeItem") -> None:
        super().__init__(parent)
        self.direction = direction
        self._start_scene_pos: QtCore.QPointF | None = None
        self._before: tuple[float, float, float, float] | None = None
        self.setCursor(self.CURSORS[direction])
        self.setZValue(20.0)
        self.setFlag(
            QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations,
            True,
        )
        self.setVisible(False)

    def boundingRect(self) -> QtCore.QRectF:
        half = self.SIZE / 2.0
        return QtCore.QRectF(-half, -half, self.SIZE, self.SIZE)

    def paint(
        self,
        painter: QtGui.QPainter,
        option: QtWidgets.QStyleOptionGraphicsItem,
        widget: QtWidgets.QWidget | None = None,
    ) -> None:
        painter.setPen(QtGui.QPen(QtGui.QColor(42, 128, 210), 1.2))
        painter.setBrush(QtGui.QColor(245, 250, 255))
        painter.drawRect(self.boundingRect())

    def mousePressEvent(self, event: QtWidgets.QGraphicsSceneMouseEvent) -> None:
        if event.button() != QtCore.Qt.MouseButton.LeftButton:
            event.ignore()
            return
        node_view = self.parentItem()
        node = node_view.viewer_node
        self._start_scene_pos = event.scenePos()
        self._before = (*node.pos(), *node.size)
        node_view.setFlag(
            QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False
        )
        viewer = node_view.viewer()
        viewer._node_positions = {}
        setattr(viewer, "_psid_active_resize_handle", self)
        event.accept()

    def mouseMoveEvent(self, event: QtWidgets.QGraphicsSceneMouseEvent) -> None:
        if self._start_scene_pos is None or self._before is None:
            event.ignore()
            return
        delta = event.scenePos() - self._start_scene_pos
        pos, size = self._resized_geometry(delta, event.modifiers())
        self.parentItem().viewer_node.preview_resize(pos, size)
        event.accept()

    def mouseReleaseEvent(self, event: QtWidgets.QGraphicsSceneMouseEvent) -> None:
        if self._before is None:
            event.accept()
            return
        node_view = self.parentItem()
        node = node_view.viewer_node
        after = (*node.pos(), *node.size)
        before = self._before
        self._clear_drag()
        if after != before and node.resize_finished:
            node.resize_finished(node, before, after)
        event.accept()

    def cancel_resize(self) -> None:
        if self._before is None:
            return
        node = self.parentItem().viewer_node
        node.preview_resize(self._before[:2], self._before[2:])
        if self.scene() and self.scene().mouseGrabberItem() is self:
            self.ungrabMouse()
        self._clear_drag()

    def _clear_drag(self) -> None:
        node_view = self.parentItem()
        node_view.setFlag(
            QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True
        )
        viewer = node_view.viewer()
        if getattr(viewer, "_psid_active_resize_handle", None) is self:
            setattr(viewer, "_psid_active_resize_handle", None)
        self._start_scene_pos = None
        self._before = None

    def _resized_geometry(
        self,
        delta: QtCore.QPointF,
        modifiers: QtCore.Qt.KeyboardModifiers,
    ) -> tuple[tuple[float, float], tuple[float, float]]:
        assert self._before is not None
        x, y, width, height = self._before
        left = "w" in self.direction
        right = "e" in self.direction
        top = "n" in self.direction
        bottom = "s" in self.direction
        centered = bool(modifiers & QtCore.Qt.KeyboardModifier.AltModifier)
        keep_ratio = bool(modifiers & QtCore.Qt.KeyboardModifier.ShiftModifier)
        factor = 2.0 if centered else 1.0

        new_width = width + factor * delta.x() * (1 if right else -1 if left else 0)
        new_height = height + factor * delta.y() * (1 if bottom else -1 if top else 0)
        min_width, min_height = self.parentItem().minimum_size

        if keep_ratio:
            scales = []
            if left or right:
                scales.append(new_width / width)
            if top or bottom:
                scales.append(new_height / height)
            scale = max(scales, key=lambda value: abs(value - 1.0))
            scale = max(scale, min_width / width, min_height / height)
            new_width = width * scale
            new_height = height * scale
        else:
            new_width = max(new_width, min_width)
            new_height = max(new_height, min_height)

        center_x = x + width / 2.0
        center_y = y + height / 2.0
        if centered:
            new_x = center_x - new_width / 2.0
            new_y = center_y - new_height / 2.0
        else:
            new_x = x + width - new_width if left else x
            new_y = y + height - new_height if top else y
            if keep_ratio and not (left or right):
                new_x = center_x - new_width / 2.0
            if keep_ratio and not (top or bottom):
                new_y = center_y - new_height / 2.0
        return (new_x, new_y), (new_width, new_height)


class ReadOnlyNodeItem(NodeItem):
    def __init__(self, name: str = "node", parent: Any = None) -> None:
        super().__init__(name, parent)
        self._has_component_icon = False
        self.viewer_node: ViewerNode | None = None
        self.minimum_size = (80.0, 70.0)
        self.resize_handles = {
            direction: ResizeHandle(direction, self)
            for direction in ("nw", "n", "ne", "e", "se", "s", "sw", "w")
        }

    def add_input(
        self, name="input", multi_port=False, display_name=True,
        locked=False, painter_func=None,
    ) -> Any:
        if painter_func is not _paint_bus_port:
            return super().add_input(name, multi_port, display_name, locked, painter_func)
        port = DraggableBusPortItem(self, painter_func)
        port.name = name
        port.port_type = PortTypeEnum.IN.value
        port.multi_connection = multi_port
        port.display_name = display_name
        port.locked = locked
        return self._add_port(port)

    def add_output(
        self, name="output", multi_port=False, display_name=True,
        locked=False, painter_func=None,
    ) -> Any:
        if painter_func is not _paint_bus_port:
            return super().add_output(name, multi_port, display_name, locked, painter_func)
        port = DraggableBusPortItem(self, painter_func)
        port.name = name
        port.port_type = PortTypeEnum.OUT.value
        port.multi_connection = multi_port
        port.display_name = display_name
        port.locked = locked
        return self._add_port(port)

    def set_component_icon(self, path: str | None, available: bool = True) -> None:
        self._has_component_icon = bool(path)
        if path:
            pixmap = QtGui.QPixmap(path).scaled(
                ICON_SIZE,
                ICON_SIZE,
                QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation,
            )
            self._icon_item.setPixmap(pixmap)
            self._icon_item.setOpacity(1.0 if available else 0.42)
            self._icon_item.setVisible(True)
            self.align_icon()
        else:
            self._icon_item.setPixmap(QtGui.QPixmap())
            self._icon_item.setVisible(False)

    def align_icon(self, h_offset: float = 0.0, v_offset: float = 0.0) -> None:
        if not self._has_component_icon:
            super().align_icon(h_offset, v_offset)
            return
        icon = self._icon_item.boundingRect()
        header_height = self._text_item.boundingRect().height() + 4.0
        rect = self.boundingRect()
        x = rect.center().x() - icon.width() / 2.0 + h_offset
        y = header_height + (rect.height() - header_height - icon.height()) / 2.0 + v_offset
        self._icon_item.setPos(x, y)

    def draw_node(self) -> None:
        super().draw_node()
        if not self._has_component_icon:
            self._icon_item.setVisible(False)

    def set_node_size(self, width: float, height: float) -> None:
        self.prepareGeometryChange()
        self._width = max(width, self.minimum_size[0])
        self._height = max(height, self.minimum_size[1])
        header_height = self._text_item.boundingRect().height() + 4.0
        self.align_label()
        self.align_icon(h_offset=2.0, v_offset=1.0)
        self.align_ports(v_offset=header_height)
        self.align_widgets(v_offset=header_height)
        self.update_resize_handles()
        self.update()

    def set_resize_handles_visible(self, visible: bool) -> None:
        for handle in self.resize_handles.values():
            handle.setVisible(visible)

    def update_resize_handles(self) -> None:
        width, height = self._width, self._height
        positions = {
            "nw": (0.0, 0.0),
            "n": (width / 2.0, 0.0),
            "ne": (width, 0.0),
            "e": (width, height / 2.0),
            "se": (width, height),
            "s": (width / 2.0, height),
            "sw": (0.0, height),
            "w": (0.0, height / 2.0),
        }
        for direction, position in positions.items():
            self.resize_handles[direction].setPos(*position)

    def hoverEnterEvent(self, event: QtWidgets.QGraphicsSceneHoverEvent) -> None:
        self.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
        if self.viewer_node and self.viewer_node.bus_connection_handles_enabled:
            viewer = self.viewer()
            viewer._set_temporary_bus_handles_visible(
                self.viewer_node, viewer.compatible_bus_handle_type()
            )
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event: QtWidgets.QGraphicsSceneHoverEvent) -> None:
        self.unsetCursor()
        if self.viewer_node:
            for port in (*self.viewer_node.input_ports(), *self.viewer_node.output_ports()):
                if (
                    getattr(port.view, "temporary_connection_handle", False)
                    and not port.connected_ports()
                ):
                    port.view.setVisible(False)
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event: QtWidgets.QGraphicsSceneMouseEvent) -> None:
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QtWidgets.QGraphicsSceneMouseEvent) -> None:
        self.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QtWidgets.QGraphicsSceneMouseEvent) -> None:
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            viewer = self.viewer()
            if viewer:
                viewer.node_double_clicked.emit(self.id)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class ViewerNode(BaseNode):
    __identifier__ = "psid.viewer"
    NODE_NAME = "PSID component"

    def __init__(self) -> None:
        super().__init__(ReadOnlyNodeItem)
        self.viewer_key = ""
        self.payload: Any = None
        self.runtime: Any = None
        self.search_text = ""
        self.base_color = (80, 100, 120)
        self.orientation = 0
        self.mirror_horizontal = False
        self.mirror_vertical = False
        self.default_size: tuple[float, float] | None = None
        self.custom_size: tuple[float, float] | None = None
        self.resize_finished: Any = None
        self.port_move_finished: Any = None
        self.port_definitions: dict[str, PortDefinition] = {}
        self.editor_added = False
        self.is_bus_node = False
        self.bus_connection_handles_enabled = False
        self.view.viewer_node = self

    def _set_port_label(self, port: Any, label: str) -> None:
        text = (
            self.view.get_input_text_item(port.view)
            if port.type_() == "in"
            else self.view.get_output_text_item(port.view)
        )
        text.setPlainText(label)
        port.view.signal_label = label
        port.view.setToolTip(label)

    def add_actual_input(
        self,
        name: str,
        multi: bool = False,
        label: str = "",
        structural: bool = False,
    ) -> Any:
        port = self.add_input(
            name,
            multi_input=multi,
            display_name=bool(label),
            painter_func=_paint_structural_port if structural else None,
        )
        port.view.structural = structural
        if label:
            self._set_port_label(port, label)
        return port

    def add_actual_output(
        self,
        name: str,
        multi: bool = True,
        label: str = "",
        structural: bool = False,
    ) -> Any:
        port = self.add_output(
            name,
            multi_output=multi,
            display_name=bool(label),
            painter_func=_paint_structural_port if structural else None,
        )
        port.view.structural = structural
        if label:
            self._set_port_label(port, label)
        return port

    def add_bus_input(
        self, name: str, multi: bool = False, *, temporary: bool = False
    ) -> Any:
        port = self.add_input(
            name,
            multi_input=multi,
            display_name=False,
            painter_func=_paint_bus_port,
        )
        port.view.temporary_connection_handle = temporary
        port.view.editor_anchor = temporary
        return port

    def add_bus_output(
        self, name: str, multi: bool = False, *, temporary: bool = False
    ) -> Any:
        port = self.add_output(
            name,
            multi_output=multi,
            display_name=False,
            painter_func=_paint_bus_port,
        )
        port.view.temporary_connection_handle = temporary
        port.view.editor_anchor = temporary
        return port

    def restore_bus_port_positions(self) -> None:
        for port in (*self.input_ports(), *self.output_ports()):
            view = port.view
            if (
                getattr(view, "bus_edge_port", False)
                and view.manual_edge_position
                and view.edge_position is not None
            ):
                view.set_normalized_position(view.edge_position)

    def space_bus_ports(self) -> None:
        for port in (*self.input_ports(), *self.output_ports()):
            view = port.view
            if (
                getattr(view, "bus_edge_port", False)
                and not getattr(view, "temporary_connection_handle", False)
            ):
                view.snap_current_to_edge()

    def position_temporary_bus_handle(self) -> None:
        """Overlay the typed connection handles at one visible right-side point."""
        for port in (*self.input_ports(), *self.output_ports()):
            view = port.view
            if getattr(view, "temporary_connection_handle", False):
                view.set_normalized_position((1.0, 0.5), manual=False)

    def add_boundary_output(self, name: str, target: str, label: str = "") -> Any:
        port = self.add_output(
            name,
            display_name=bool(label),
            painter_func=_paint_boundary_port,
        )
        if label:
            self._set_port_label(port, label)
        port.view.boundary_target = target
        port.view.setToolTip(f"{label}\n{target}" if label else target)
        return port

    @property
    def size(self) -> tuple[float, float]:
        return self.view.boundingRect().width(), self.view.boundingRect().height()

    def capture_default_size(self) -> None:
        self.default_size = self.size
        self.view.minimum_size = self.default_size
        self.view.update_resize_handles()

    def set_size(self, width: float, height: float) -> None:
        self.custom_size = (width, height)
        self.view.set_node_size(width, height)
        self._apply_transform_layout()

    def preview_resize(
        self,
        pos: tuple[float, float],
        size: tuple[float, float],
    ) -> None:
        self.set_property("pos", list(pos), push_undo=False)
        self.set_size(*size)

    def set_orientation(self, degrees: int) -> None:
        self.set_transform_state(
            degrees, self.mirror_horizontal, self.mirror_vertical
        )

    def set_transform_state(
        self,
        degrees: int,
        mirror_horizontal: bool,
        mirror_vertical: bool,
    ) -> None:
        self.orientation = degrees % 360
        self.mirror_horizontal = mirror_horizontal
        self.mirror_vertical = mirror_vertical
        self.view.draw_node()
        if self.custom_size:
            self.view.set_node_size(*self.custom_size)
        self._apply_transform_layout()

    def _apply_transform_layout(self) -> None:
        self.view._text_item.set_locked(True)
        width = self.view.boundingRect().width()
        height = self.view.boundingRect().height()
        inputs = self.input_ports()
        outputs = self.output_ports()
        if self.orientation in (90, 270):
            input_y = -inputs[0].view.boundingRect().height() / 2 if inputs else 0.0
            output_y = height - outputs[0].view.boundingRect().height() / 2 if outputs else 0.0
            if self.orientation == 270:
                input_y, output_y = output_y, input_y
            for index, port in enumerate(inputs, 1):
                port.view.setPos(
                    width * index / (len(inputs) + 1)
                    - port.view.boundingRect().width() / 2,
                    input_y,
                )
            for index, port in enumerate(outputs, 1):
                port.view.setPos(
                    width * index / (len(outputs) + 1)
                    - port.view.boundingRect().width() / 2,
                    output_y,
                )
        elif self.orientation == 180:
            for port in inputs:
                port.view.setX(width - port.view.boundingRect().width() / 2)
            for port in outputs:
                port.view.setX(-port.view.boundingRect().width() / 2)
        if self.mirror_horizontal:
            for port in (*inputs, *outputs):
                port.view.setX(
                    width - port.view.x() - port.view.boundingRect().width()
                )
        if self.mirror_vertical:
            for port in (*inputs, *outputs):
                port.view.setY(
                    height - port.view.y() - port.view.boundingRect().height()
                )
        self.restore_bus_port_positions()
        if self.is_bus_node:
            self.position_temporary_bus_handle()
        icon = self.view._icon_item
        center = icon.boundingRect().center()
        transform = QtGui.QTransform()
        transform.translate(center.x(), center.y())
        transform.rotate(self.orientation)
        transform.scale(
            -1.0 if self.mirror_horizontal else 1.0,
            -1.0 if self.mirror_vertical else 1.0,
        )
        transform.translate(-center.x(), -center.y())
        icon.setRotation(0.0)
        icon.setTransform(transform)
        for port in (*inputs, *outputs):
            port.view.redraw_connected_pipes()
        self._align_port_labels()
        self.view.update_resize_handles()
        self.view.update()

    def _align_port_labels(self) -> None:
        rect = self.view.boundingRect()
        for port in (*self.input_ports(), *self.output_ports()):
            if not port.view.display_name:
                continue
            text = (
                self.view.get_input_text_item(port.view)
                if port.type_() == "in"
                else self.view.get_output_text_item(port.view)
            )
            center = port.view.pos() + port.view.boundingRect().center()
            bounds = text.boundingRect()
            distances = {
                "left": abs(center.x() - rect.left()),
                "right": abs(center.x() - rect.right()),
                "top": abs(center.y() - rect.top()),
                "bottom": abs(center.y() - rect.bottom()),
            }
            side = min(distances, key=distances.get)
            if side == "left":
                position = QtCore.QPointF(center.x() + 7.0, center.y() - bounds.height() / 2.0)
            elif side == "right":
                position = QtCore.QPointF(center.x() - bounds.width() - 7.0, center.y() - bounds.height() / 2.0)
            elif side == "top":
                position = QtCore.QPointF(center.x() - bounds.width() / 2.0, center.y() + 7.0)
            else:
                position = QtCore.QPointF(center.x() - bounds.width() / 2.0, center.y() - bounds.height() - 7.0)
            text.setPos(position)


@dataclass
class BuildResult:
    nodes: dict[str, ViewerNode]


class _GraphBuilder:
    def __init__(self, graph: NodeGraph) -> None:
        self.graph = graph

    def _node(
        self,
        key: str,
        title: str,
        payload: Any,
        runtime: Any,
        color: tuple[int, int, int],
        pos: tuple[float, float],
        search_text: str,
    ) -> ViewerNode:
        node = ViewerNode()
        node.set_color(*color)
        node.viewer_key = key
        node.payload = payload
        node.runtime = runtime
        node.search_text = search_text.casefold()
        node.base_color = color
        self.graph.add_node(node, pos=list(pos), selected=False, push_undo=False)
        node.set_property("name", title, push_undo=False)
        return node

    @staticmethod
    def _connect(
        source: ViewerNode,
        source_port: str,
        target: ViewerNode,
        target_port: str,
        structural: bool = False,
    ) -> None:
        output = source.get_output(source_port)
        output.connect_to(
            target.get_input(target_port), push_undo=False, emit_signal=False
        )
        if structural and output.view.connected_pipes:
            pipe = output.view.connected_pipes[-1]
            pipe.structural = True
            pipe.color = (112, 118, 126, 150)
            pipe.style = PipeEnum.DRAW_TYPE_DASHED.value
            pipe.reset()

    @staticmethod
    def _freeze(nodes: dict[str, ViewerNode]) -> None:
        movable_flag = getattr(
            QtWidgets.QGraphicsItem,
            "ItemIsMovable",
            QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIsMovable,
        )
        for node in nodes.values():
            node.view.draw_node()
            if node.view._has_component_icon and node.size[1] < 84.0:
                node.view.set_node_size(node.size[0], 84.0)
            node.capture_default_size()
            if node.is_bus_node:
                node.space_bus_ports()
            node.view.setAcceptHoverEvents(True)
            node.view.setFlag(movable_flag, True)
            node.view._text_item.set_locked(True)
            for port in (*node.input_ports(), *node.output_ports()):
                port.set_locked(True, connected_ports=False, push_undo=False)


class TopologyGraphBuilder(_GraphBuilder):
    def build(self, project: ExportedProject) -> BuildResult:
        positions = topology_positions(project)
        nodes: dict[str, ViewerNode] = {}
        document: EditorDocument = project.editor_document or EditorDocument()
        deleted = set(document.deleted_keys)
        for bus in sorted(project.buses, key=lambda item: item.number):
            key = f"bus:{bus.number}"
            if key in deleted:
                continue
            color = BUS_COLORS.get(bus.bus_type.upper(), (75, 113, 140))
            if not bus.available:
                color = DISABLED_COLOR
            point = positions[key]
            nodes[key] = self._node(
                key,
                f"{bus.name}  [{bus.bus_type}]",
                bus,
                project.runtime_component(bus.name, "ACBus"),
                color,
                (point.x, point.y),
                f"{bus.name} {bus.number} {bus.bus_type} ACBus",
            )
            nodes[key].is_bus_node = True
            nodes[key].view.set_component_icon(topology_icon_path(bus), bus.available)

        for branch in sorted(project.branches, key=lambda item: item.name):
            key = f"branch:{branch.name}"
            if key in deleted or f"bus:{branch.from_bus}" not in nodes or f"bus:{branch.to_bus}" not in nodes:
                continue
            color = (70, 123, 151) if branch.branch_type == "Line" else (148, 104, 57)
            if not branch.available:
                color = DISABLED_COLOR
            point = positions[key]
            node = self._node(
                key,
                f"{branch.branch_type}\n{branch.name}",
                branch,
                project.runtime_component(branch.name, branch.branch_type),
                color,
                (point.x, point.y),
                f"{branch.name} {branch.branch_type} {branch.from_bus} {branch.to_bus}",
            )
            node.view.set_component_icon(topology_icon_path(branch), branch.available)
            nodes[key] = node
            from_bus = nodes[f"bus:{branch.from_bus}"]
            to_bus = nodes[f"bus:{branch.to_bus}"]
            from_bus_port = f"to:{branch.name}"
            to_bus_port = f"from:{branch.name}"
            from_bus.add_bus_output(from_bus_port)
            node.add_actual_input("from")
            node.add_actual_output("to")
            to_bus.add_bus_input(to_bus_port)
            self._connect(from_bus, from_bus_port, node, "from")
            self._connect(node, "to", to_bus, to_bus_port)

        for injection in sorted(project.injections, key=lambda item: item.name):
            key = f"injection:{injection.name}"
            if key in deleted or f"bus:{injection.bus}" not in nodes:
                continue
            if "load" in injection.injection_type.lower():
                color = (116, 119, 126)
            elif "inverter" in injection.dynamic_type.lower():
                color = (126, 91, 184)
            else:
                color = (182, 119, 48)
            if not injection.available:
                color = DISABLED_COLOR
            point = positions[key]
            dynamic_label = "动态" if injection.is_dynamic else "静态"
            node = self._node(
                key,
                f"{injection.name}\n{dynamic_label} {injection.injection_type}",
                injection,
                project.runtime_component(injection.name, injection.injection_type),
                color,
                (point.x, point.y),
                " ".join(
                    (
                        injection.name,
                        injection.injection_type,
                        injection.dynamic_type,
                        str(injection.bus),
                    )
                ),
            )
            node.view.set_component_icon(topology_icon_path(injection), injection.available)
            nodes[key] = node
            bus_node = nodes[f"bus:{injection.bus}"]
            if "load" in injection.injection_type.lower():
                bus_port = f"to:{injection.name}"
                bus_node.add_bus_output(bus_port)
                node.add_actual_input("bus")
                self._connect(bus_node, bus_port, node, "bus")
            else:
                bus_port = f"from:{injection.name}"
                node.add_actual_output("bus")
                bus_node.add_bus_input(bus_port)
                self._connect(node, "bus", bus_node, bus_port)

        for instance in document.instances:
            node = self._editor_node(instance, project)
            nodes[instance.key] = node
        for connection in document.connections:
            self._connect_editor_connection(connection, nodes)

        self._freeze(nodes)
        return BuildResult(nodes)

    def _editor_node(
        self, instance: EditorInstance, project: ExportedProject
    ) -> ViewerNode:
        colors = {
            "bus": BUS_COLORS["PQ"],
            "line": (70, 123, 151),
            "transformer": (148, 104, 57),
            "generator": (182, 119, 48),
            "inverter": (126, 91, 184),
            "load": (116, 119, 126),
            "source": (182, 119, 48),
            "custom": (30, 152, 138),
        }
        title = instance.name
        if instance.kind == "bus":
            title += f"  [PQ]"
        else:
            title += f"\n{instance.model_ref}"
        available = bool(instance.parameters.get("available", True))
        color = colors.get(instance.kind, colors["custom"])
        node = self._node(
            instance.key,
            title,
            instance,
            instance.parameters,
            color if available else DISABLED_COLOR,
            instance.position,
            f"{instance.name} {instance.kind} {instance.model_ref}",
        )
        icon_path = editor_icon_path(instance.model_ref)
        if icon_path:
            node.view.set_component_icon(icon_path, available)
        node.editor_added = True
        node.port_definitions = {port.port_id: port for port in instance.ports}
        if instance.kind == "bus":
            node.is_bus_node = True
        else:
            for port in instance.ports:
                if instance.kind in {"line", "transformer"}:
                    if port.port_id == "from":
                        node.add_actual_input(port.port_id, multi=False)
                    else:
                        node.add_actual_output(port.port_id, multi=False)
                elif port.role == "sink":
                    node.add_actual_input(port.port_id, multi=False)
                else:
                    node.add_actual_output(port.port_id, multi=False)
        return node

    def _connect_editor_connection(
        self,
        connection: EditorConnection,
        nodes: dict[str, ViewerNode],
    ) -> None:
        first = nodes.get(connection.first_key)
        second = nodes.get(connection.second_key)
        if not first or not second:
            return
        endpoints = ((first, connection.first_port), (second, connection.second_port))
        bus_endpoint = next((item for item in endpoints if item[0].viewer_key.startswith("bus:") or getattr(item[0].payload, "kind", "") == "bus"), None)
        other_endpoint = next((item for item in endpoints if item is not bus_endpoint), None)
        if not bus_endpoint or not other_endpoint:
            return
        bus, _ = bus_endpoint
        other, port_name = other_endpoint
        other_input = other.get_input(port_name)
        if other_input:
            bus_port_name = f"edit-out:{connection.connection_id}"
            if not bus.get_output(bus_port_name):
                bus.add_bus_output(bus_port_name)
            self._connect(bus, bus_port_name, other, port_name)
        else:
            bus_port_name = f"edit-in:{connection.connection_id}"
            if not bus.get_input(bus_port_name):
                bus.add_bus_input(bus_port_name)
            self._connect(other, port_name, bus, bus_port_name)


class DeviceGraphBuilder(_GraphBuilder):
    def build(self, project: ExportedProject, device_identifier: str) -> BuildResult:
        document: EditorDocument = project.editor_document or EditorDocument()
        instance = document.instance_by_key.get(device_identifier)
        branch = None
        if instance:
            prefix = f"editor-device:{instance.instance_id}"
            injection_name = instance.name
            components = list(editor_device_components(project, instance))
        elif device_identifier.startswith("branch:"):
            injection_name = device_identifier.split(":", 1)[1]
            branch = next(
                (value for value in project.branches if value.name == injection_name), None
            )
            prefix = f"branches/{injection_name}"
            components = [
                item
                for item in project.equations
                if item.path == prefix or item.path.startswith(prefix + "/")
            ]
        else:
            injection_name = device_identifier
            prefix = f"injections/{injection_name}"
            components = [
                item
                for item in project.equations
                if item.path == prefix or item.path.startswith(prefix + "/")
            ]
        positions = device_positions([item.path for item in components])
        nodes: dict[str, ViewerNode] = {}
        by_path: dict[str, EquationComponent] = {item.path: item for item in components}
        overrides = {item.component_path: item for item in document.slot_overrides}
        graph_injector = (
            None if instance else project.graph_injector_by_name.get(injection_name)
        )
        for component in sorted(components, key=lambda item: (item.path.count("/"), item.path)):
            override = overrides.get(component.path)
            if override:
                component = replace(
                    component,
                    component_type=override.model_name,
                    raw={**component.raw, "editor_model_ref": override.model_ref, "official_type": component.component_type},
                )
            label = injection_name if component.path == prefix else component.path.rsplit("/", 1)[-1]
            title = f"{label}\n{component.role} · {len(component.states)} states"
            color = ROLE_COLORS.get(component.role, (77, 111, 135))
            point = positions[component.path]
            runtime = (
                editor_component_runtime(instance, component)
                if instance
                else project.runtime_for_equation(component.path)
            )
            if component.role == "custom_dynamic" and not instance:
                runtime = component.raw
            elif component.role == "custom_state":
                runtime = None
            node = self._node(
                component.path,
                title,
                component,
                runtime,
                color,
                (point.x, point.y),
                " ".join(
                    (
                        component.path,
                        component.role,
                        component.component_type,
                        *component.states,
                        *component.entrypoints,
                    )
                ),
            )
            node.view.set_component_icon(None)
            if component.path == prefix:
                node.graph_injector = graph_injector
            nodes[component.path] = node

        for component in components:
            if component.path == prefix:
                continue
            parent = component.path.rsplit("/", 1)[0]
            while parent not in by_path and parent.startswith(prefix):
                parent = parent.rsplit("/", 1)[0]
            if parent in nodes:
                port_name = component.path.removeprefix(parent + "/")
                nodes[parent].add_actual_output(port_name, structural=True)
                nodes[component.path].add_actual_input("parent", structural=True)
                self._connect(
                    nodes[parent], port_name, nodes[component.path], "parent", structural=True
                )

        self._connect_component_contracts(prefix, nodes)

        root = nodes.get(prefix)
        injection = None if instance or branch else next(
            (item for item in project.injections if item.name == injection_name), None
        )
        if root and instance:
            bus_names = {f"bus:{bus.number}": bus.name for bus in project.buses}
            bus_names.update(
                {
                    item.key: item.name
                    for item in document.instances
                    if item.kind == "bus"
                }
            )
            for connection in document.connections:
                if connection.first_key == instance.key:
                    port_id, bus_key = connection.first_port, connection.second_key
                elif connection.second_key == instance.key:
                    port_id, bus_key = connection.second_port, connection.first_key
                else:
                    continue
                definition = next(
                    (port for port in instance.ports if port.port_id == port_id), None
                )
                root.add_boundary_output(
                    f"boundary:{port_id}",
                    f"电网拓扑 / {bus_names.get(bus_key, bus_key)} / {port_id}",
                    definition.name if definition else port_id,
                )
        elif root and branch:
            from_bus = project.bus_by_number[branch.from_bus]
            to_bus = project.bus_by_number[branch.to_bus]
            root.add_boundary_output(
                "boundary:from",
                f"电网拓扑 / {from_bus.name} / from",
                "from V/I",
            )
            root.add_boundary_output(
                "boundary:to",
                f"电网拓扑 / {to_bus.name} / to",
                "to V/I",
            )
        elif root and injection:
            bus = project.bus_by_number[injection.bus]
            root.add_boundary_output(
                f"bus:{bus.number}",
                f"电网拓扑 / {bus.name} / electrical",
                "electrical",
            )

        self._freeze(nodes)
        return BuildResult(nodes)

    def _connect_component_contracts(
        self, prefix: str, nodes: dict[str, ViewerNode]
    ) -> None:
        def connect(source_suffix: str, target_suffix: str, signal: str) -> None:
            source = nodes.get(f"{prefix}/{source_suffix}")
            target = nodes.get(f"{prefix}/{target_suffix}")
            if not source or not target:
                return
            output_name = f"{signal}->{target_suffix}"
            input_name = f"{source_suffix}->{signal}"
            source.add_actual_output(output_name, label=signal)
            target.add_actual_input(input_name, label=signal)
            self._connect(source, output_name, target, input_name)

        generator_contracts = (
            ("shaft", "machine", "δ,ω"),
            ("shaft", "prime_mover", "ω"),
            ("shaft", "pss", "ω"),
            ("prime_mover", "shaft", "τm"),
            ("machine", "shaft", "τe"),
            ("machine", "pss", "τe"),
            ("pss", "avr", "Vpss"),
            ("avr", "machine", "Vf"),
        )
        inverter_contracts = (
            ("dc_source", "converter", "Vdc"),
            ("filter", "freq_estimator", "v_filter"),
            ("freq_estimator", "outer_control", "ω_est"),
            ("filter", "outer_control", "v/i_filter"),
            ("outer_control", "inner_control", "V/I_ref"),
            ("filter", "inner_control", "v/i_filter"),
            ("inner_control", "converter", "md,mq"),
            ("converter", "filter", "Vcnv"),
        )
        contracts = generator_contracts if f"{prefix}/machine" in nodes else inverter_contracts
        for contract in contracts:
            connect(*contract)
