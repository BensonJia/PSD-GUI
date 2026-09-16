from __future__ import annotations

import html
import copy
import json
import re
import shutil
import sys
import tomllib
from dataclasses import asdict, is_dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping

from NodeGraphQt import NodeGraph
from Qt import QtCore, QtGui, QtWidgets

from .graph_builders import (
    DeviceGraphBuilder,
    EdgeNormalNodeViewer,
    ReadOnlyNodeItem,
    ResizeHandle,
    TopologyGraphBuilder,
    ViewerNode,
    editor_device_components,
)
from .loader import ProjectLoadError, ProjectLoader
from .model_library import ModelImplementation, ModelLibrary
from .models import Branch, Bus, EquationComponent, ExportedProject, Injection
from .simulation import (
    BackendManagerDialog,
    BackendStatusWidget,
    JuliaProcess,
    ParameterEditor,
    ParameterExpression,
    SimulationTaskDialog,
    SimulationSettings,
    SimulationSettingsDialog,
    SweepParameter,
    set_value,
    value_at,
)
from .results import DataManagerDialog, ResultDatabase
from .component_tree import ComponentTreePanel
from .custom_models import (
    CustomModel,
    CustomModelManagerDialog,
    CustomModelStore,
    apply_custom_models,
)
from .editor import (
    EditorConnection,
    EditorDocument,
    EditorDocumentStore,
    EditorInstance,
    SlotOverride,
    default_instance_parameters,
    default_ports,
    upgrade_editor_instances,
)


SKIPPED_RUNTIME_KEYS = {
    "__metadata__",
    "internal",
    "ext",
    "services",
    "operation_cost",
    "dynamic_injector",
    "arc",
    "area",
    "load_zone",
}

APPLICATION_NAME = "PowerSimulationDynamics-GUI"
APPLICATION_DISPLAY_NAME = "PSD-GUI"
APPLICATION_ICON = Path(__file__).with_name("Logo.png")
UI_STATE_VERSION = 2


class ModelDialog(QtWidgets.QDialog):
    def __init__(self, title: str, content: str, parent: QtWidgets.QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"数学模型 — {title}")
        self.resize(920, 700)
        layout = QtWidgets.QVBoxLayout(self)
        browser = QtWidgets.QTextBrowser()
        browser.setHtml(content)
        layout.addWidget(browser)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Close
        )
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class ReadOnlyFilter(QtCore.QObject):
    context_menu_requested = QtCore.Signal(str, object)
    resize_mode_cancelled = QtCore.Signal()
    placement_requested = QtCore.Signal(object)
    placement_cancelled = QtCore.Signal()
    delete_requested = QtCore.Signal()

    def __init__(self, viewer: Any, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self.viewer = viewer
        self.viewer.viewport().setMouseTracking(True)
        self.space_pan = False
        self.permanent_pan = False
        self.base_cursor = QtCore.Qt.CursorShape.ArrowCursor
        self.space_pan_last: QtCore.QPointF | None = None
        self.boundary_port: Any = None
        self.boundary_tooltip_pos = QtCore.QPoint()
        self.boundary_timer = QtCore.QTimer(self)
        self.boundary_timer.setSingleShot(True)
        self.boundary_timer.setInterval(1000)
        self.boundary_timer.timeout.connect(self._show_boundary_tooltip)
        self.placement_active = False

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if watched is self.viewer.viewport() and event.type() == QtCore.QEvent.Type.MouseMove:
            self._update_boundary_hover(event)
        elif watched is self.viewer.viewport() and event.type() == QtCore.QEvent.Type.Leave:
            self._clear_boundary_hover()
        if (
            event.type() == QtCore.QEvent.Type.KeyPress
            and event.key() == QtCore.Qt.Key.Key_Escape
        ):
            if self.placement_active:
                self.placement_cancelled.emit()
                event.accept()
                return True
            handle = getattr(self.viewer, "_psid_active_resize_handle", None)
            if handle:
                handle.cancel_resize()
            else:
                self.resize_mode_cancelled.emit()
            event.accept()
            return True
        if (
            self.placement_active
            and watched is self.viewer.viewport()
            and event.type() == QtCore.QEvent.Type.MouseButtonPress
        ):
            if event.button() == QtCore.Qt.MouseButton.RightButton:
                self.placement_cancelled.emit()
            elif event.button() == QtCore.Qt.MouseButton.LeftButton:
                self.placement_requested.emit(
                    self.viewer.mapToScene(event.position().toPoint())
                )
            event.accept()
            return True
        if (
            event.type() == QtCore.QEvent.Type.MouseButtonPress
            and event.button() == QtCore.Qt.MouseButton.LeftButton
        ):
            item = self.viewer.itemAt(event.position().toPoint())
            if isinstance(item, ResizeHandle):
                self.viewer.ALT_state = False
                self.viewer.SHIFT_state = False
        if event.type() == QtCore.QEvent.Type.ContextMenu:
            item = self.viewer.itemAt(event.pos())
            while item and not isinstance(item, ReadOnlyNodeItem):
                item = item.parentItem()
            if not isinstance(item, ReadOnlyNodeItem):
                selected = self.viewer.selected_nodes()
                item = selected[0] if len(selected) == 1 else None
            if isinstance(item, ReadOnlyNodeItem):
                global_pos = event.globalPos()
                if event.reason() == QtGui.QContextMenuEvent.Reason.Keyboard:
                    global_pos = self.viewer.viewport().mapToGlobal(
                        self.viewer.viewport().rect().center()
                    )
                self.context_menu_requested.emit(item.id, global_pos)
            event.accept()
            return True
        if event.type() == QtCore.QEvent.Type.KeyPress and event.key() == QtCore.Qt.Key.Key_Space:
            if not event.isAutoRepeat():
                self.space_pan = True
                self.viewer.viewport().setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
            event.accept()
            return True
        if event.type() == QtCore.QEvent.Type.KeyRelease and event.key() == QtCore.Qt.Key.Key_Space:
            self.space_pan = self.permanent_pan
            self.space_pan_last = None
            if self.permanent_pan:
                self.viewer.viewport().setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
            else:
                self.viewer.viewport().setCursor(self.base_cursor)
            event.accept()
            return True
        if event.type() == QtCore.QEvent.Type.KeyPress and event.key() in {
            QtCore.Qt.Key.Key_Delete,
            QtCore.Qt.Key.Key_Backspace,
        }:
            if not event.isAutoRepeat():
                self.delete_requested.emit()
            event.accept()
            return True
        if event.type() in {
            QtCore.QEvent.Type.MouseButtonPress,
            QtCore.QEvent.Type.MouseButtonRelease,
        }:
            extend = event.modifiers() & (
                QtCore.Qt.KeyboardModifier.ControlModifier
                | QtCore.Qt.KeyboardModifier.MetaModifier
            )
            if extend:
                self.viewer.CTRL_state = False
                self.viewer.SHIFT_state = True
        if (
            event.type() == QtCore.QEvent.Type.MouseButtonPress
            and self.space_pan
            and event.button() == QtCore.Qt.MouseButton.LeftButton
        ):
            self.space_pan_last = event.position()
            self.viewer.viewport().setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return True
        if event.type() == QtCore.QEvent.Type.MouseMove and self.space_pan_last is not None:
            current = event.position()
            previous_scene = self.viewer.mapToScene(self.space_pan_last.toPoint())
            current_scene = self.viewer.mapToScene(current.toPoint())
            delta = previous_scene - current_scene
            self.viewer._set_viewer_pan(delta.x(), delta.y())
            self.space_pan_last = current
            event.accept()
            return True
        if (
            event.type() == QtCore.QEvent.Type.MouseButtonRelease
            and self.space_pan_last is not None
            and event.button() == QtCore.Qt.MouseButton.LeftButton
        ):
            self.space_pan_last = None
            self.viewer.viewport().setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
            event.accept()
            return True
        if event.type() == QtCore.QEvent.Type.NativeGesture:
            if event.gestureType() == QtCore.Qt.NativeGestureType.ZoomNativeGesture:
                self.viewer._set_viewer_zoom(
                    event.value() * 300.0,
                    pos=event.position().toPoint(),
                )
                event.accept()
                return True
        if event.type() == QtCore.QEvent.Type.Wheel:
            pinch = self._pinch_wheel_delta(event)
            if pinch is not None:
                self.viewer._set_viewer_zoom(
                    pinch,
                    pos=event.position().toPoint(),
                )
                event.accept()
                return True
            delta = self._trackpad_delta(event)
            if delta is not None:
                scene = self.viewer._scene_range
                viewport = self.viewer.viewport().size()
                scale_x = scene.width() / max(viewport.width(), 1)
                scale_y = scene.height() / max(viewport.height(), 1)
                self.viewer._set_viewer_pan(
                    -delta.x() * scale_x,
                    -delta.y() * scale_y,
                )
                event.accept()
                return True
        return super().eventFilter(watched, event)

    def _update_boundary_hover(self, event: Any) -> None:
        item = self.viewer.itemAt(event.position().toPoint())
        target = getattr(item, "boundary_target", "")
        if target and item is not self.boundary_port:
            self._clear_boundary_hover()
            self.boundary_port = item
            self.boundary_tooltip_pos = self.viewer.viewport().mapToGlobal(
                event.position().toPoint()
            )
            self.boundary_timer.start()
        elif not target:
            self._clear_boundary_hover()

    def _show_boundary_tooltip(self) -> None:
        if self.boundary_port:
            QtWidgets.QToolTip.showText(
                self.boundary_tooltip_pos,
                f"连接到：{self.boundary_port.boundary_target}",
                self.viewer.viewport(),
            )

    def _clear_boundary_hover(self) -> None:
        self.boundary_timer.stop()
        if self.boundary_port:
            QtWidgets.QToolTip.hideText()
        self.boundary_port = None

    @staticmethod
    def _is_trackpad_wheel(event: Any) -> bool:
        return (
            not event.pixelDelta().isNull()
            or event.phase() != QtCore.Qt.ScrollPhase.NoScrollPhase
        )

    @classmethod
    def _pinch_wheel_delta(cls, event: Any) -> float | None:
        if not event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier:
            return None
        if not cls._is_trackpad_wheel(event):
            return None
        pixel_delta = event.pixelDelta().y()
        return float(pixel_delta * 3.0 if pixel_delta else event.angleDelta().y())

    @classmethod
    def _trackpad_delta(cls, event: Any) -> QtCore.QPointF | None:
        if event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier:
            return None
        pixel_delta = event.pixelDelta()
        if not cls._is_trackpad_wheel(event):
            return None
        if not pixel_delta.isNull():
            return QtCore.QPointF(pixel_delta)
        angle_delta = event.angleDelta()
        return QtCore.QPointF(angle_delta.x() * 0.25, angle_delta.y() * 0.25)


class TransformNodesCommand(QtWidgets.QUndoCommand):
    def __init__(
        self,
        nodes: list[ViewerNode],
        after: list[tuple[int, bool, bool]],
        text: str,
        changed: Callable[[], None],
    ) -> None:
        super().__init__(text)
        self.nodes = nodes
        self.before = [
            (node.orientation, node.mirror_horizontal, node.mirror_vertical)
            for node in nodes
        ]
        self.after = after
        self.changed = changed

    def undo(self) -> None:
        for node, state in zip(self.nodes, self.before):
            node.set_transform_state(*state)
        self.changed()

    def redo(self) -> None:
        for node, state in zip(self.nodes, self.after):
            node.set_transform_state(*state)
        self.changed()


class ResizeNodesCommand(QtWidgets.QUndoCommand):
    def __init__(
        self,
        nodes: list[ViewerNode],
        before: list[tuple[float, float, float, float]],
        after: list[tuple[float, float, float, float]],
        text: str,
        changed: Callable[[], None],
    ) -> None:
        super().__init__(text)
        self.nodes = nodes
        self.before = before
        self.after = after
        self.changed = changed

    @staticmethod
    def _apply(
        nodes: list[ViewerNode],
        states: list[tuple[float, float, float, float]],
    ) -> None:
        for node, (x, y, width, height) in zip(nodes, states):
            node.preview_resize((x, y), (width, height))

    def undo(self) -> None:
        self._apply(self.nodes, self.before)
        self.changed()

    def redo(self) -> None:
        self._apply(self.nodes, self.after)
        self.changed()


class ParameterEditCommand(QtWidgets.QUndoCommand):
    def __init__(
        self,
        runtimes: list[dict[str, Any]],
        path: tuple[str, ...],
        value: float,
        changed: Callable[[], None],
    ) -> None:
        super().__init__(f"修改参数 {'.'.join(path)}")
        self.runtimes = runtimes
        self.path = path
        self.before = [value_at(runtime, path) for runtime in runtimes]
        self.value = value
        self.changed = changed

    def undo(self) -> None:
        for runtime, value in zip(self.runtimes, self.before):
            set_value(runtime, self.path, value)
        self.changed()

    def redo(self) -> None:
        for runtime in self.runtimes:
            set_value(runtime, self.path, self.value)
        self.changed()


class EditorDocumentCommand(QtWidgets.QUndoCommand):
    def __init__(
        self,
        before: EditorDocument,
        after: EditorDocument,
        text: str,
        apply: Callable[[EditorDocument], None],
    ) -> None:
        super().__init__(text)
        self.before = before
        self.after = after
        self.apply = apply

    def undo(self) -> None:
        self.apply(self.before)

    def redo(self) -> None:
        self.apply(self.after)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APPLICATION_NAME)
        self.setWindowIcon(QtGui.QIcon(str(APPLICATION_ICON)))
        self.resize(1440, 900)
        self.project: ExportedProject | None = None
        self.current_device: str | None = None
        self.current_view_key: str | None = None
        self.resize_node_key: str | None = None
        self.layout_states: dict[
            str, dict[str, tuple[float, float, float, float, int, bool, bool]]
        ] = {}
        self.skip_layout_capture = False
        self.nodes: dict[str, ViewerNode] = {}
        self.editor_document = EditorDocument()
        self.placement_descriptor: dict[str, Any] | None = None
        self.edit_tool = "select"
        self._shortcut_help: list[
            tuple[str, str, QtGui.QAction | tuple[QtGui.QKeySequence, ...]]
        ] = []
        self._syncing_editor = False
        self.model_library = ModelLibrary()
        self.simulation_settings = SimulationSettings()
        self.parameter_sweeps: dict[
            tuple[tuple[tuple[str | int, ...], ...], tuple[str, ...]], SweepParameter
        ] = {}
        self.active_task: dict[str, Any] | None = None
        saved_database = QtCore.QSettings().value("results/database_path", "")
        self.result_database = ResultDatabase(saved_database or None)
        self.julia = JuliaProcess(self)
        self.julia.phase_changed.connect(self._simulation_phase)
        self.julia.log_received.connect(self._simulation_log)
        self.julia.completed.connect(self._simulation_finished)
        self.julia.state_changed.connect(self._backend_state_changed)
        self._prepare_on_ready = False
        self._last_backend_log = ""

        undo_stack = QtWidgets.QUndoStack(self)
        self.graph = NodeGraph(
            undo_stack=undo_stack,
            viewer=EdgeNormalNodeViewer(undo_stack=undo_stack),
        )
        self.graph.set_acyclic(False)
        self.graph.disable_context_menu(True)
        self.graph.node_selected.connect(self._show_node_details)
        self.graph.node_selection_changed.connect(self._selection_changed)
        self.graph.node_double_clicked.connect(self._node_double_clicked)
        self.graph.port_connected.connect(self._port_connected)
        self.graph.port_disconnected.connect(self._port_disconnected)
        self.graph.property_changed.connect(self._graph_property_changed)
        viewer = self.graph.viewer()
        self.read_only_filter = ReadOnlyFilter(viewer, self)
        self.read_only_filter.context_menu_requested.connect(self._show_node_menu)
        self.read_only_filter.resize_mode_cancelled.connect(self._escape_canvas)
        self.read_only_filter.placement_requested.connect(self._place_instance)
        self.read_only_filter.placement_cancelled.connect(self._cancel_placement)
        self.read_only_filter.delete_requested.connect(self._delete_from_canvas)
        viewer.installEventFilter(self.read_only_filter)
        viewer.viewport().installEventFilter(self.read_only_filter)

        self.canvas_stack = QtWidgets.QStackedWidget()
        self.canvas_stack.addWidget(self._welcome_widget())
        self.canvas_stack.addWidget(self.graph.widget)

        self.details = QtWidgets.QTextBrowser()
        self.details.setOpenExternalLinks(False)
        self.details.setMinimumWidth(340)
        self.details.setHtml(self._welcome_details())

        self.parameters = ParameterEditor()
        self.parameters.expression_lookup = self._parameter_expression
        self.parameters.expression_requested.connect(self._parameter_expression_changed)
        self.components = ComponentTreePanel()
        self.components.selection_requested.connect(self._component_tree_selected)
        self.components.activate_requested.connect(self._activate_component_key)
        self.components.add_requested.connect(self._add_component_requested)
        self.run_log = QtWidgets.QPlainTextEdit()
        self.run_log.setReadOnly(True)
        self.setCentralWidget(self.canvas_stack)
        self._build_docks()
        self._build_toolbar()
        self._build_edit_toolbar()
        self._build_menus()
        self.backend_manager = BackendManagerDialog(self.julia, self)
        self.backend_manager.prepare_requested.connect(self.prepare_current_network)
        self.backend_manager.rebuild_requested.connect(self.rebuild_current_network)
        self.backend_status = BackendStatusWidget(self.julia, self)
        self.backend_status.clicked.connect(self.show_backend_manager)
        self.statusBar().addPermanentWidget(self.backend_status)
        self.statusBar().showMessage("请选择一个导出的项目目录")
        self.data_manager = DataManagerDialog(self.result_database, self)
        self.data_manager.database_changed.connect(self._database_changed)
        self.custom_model_manager = CustomModelManagerDialog(self)
        self.custom_model_manager.models_changed.connect(self._custom_models_changed)
        self.custom_model_manager.editing_changed.connect(
            self.edit_mode_action.setChecked
        )
        self.custom_model_manager.locate_requested.connect(self._locate_model_usage)
        self.custom_model_manager.instantiate_requested.connect(
            self._instantiate_requested
        )
        settings = QtCore.QSettings()
        geometry = settings.value("main/geometry")
        dock_state = settings.value("main/dock_state")
        if geometry:
            self.restoreGeometry(geometry)
        if dock_state:
            self.restoreState(dock_state, UI_STATE_VERSION)
        self.edit_toolbar.show()
        self.setCorner(
            QtCore.Qt.Corner.TopLeftCorner,
            QtCore.Qt.DockWidgetArea.LeftDockWidgetArea,
        )
        self.setCorner(
            QtCore.Qt.Corner.TopRightCorner,
            QtCore.Qt.DockWidgetArea.RightDockWidgetArea,
        )
        self.drawing_dock.setVisible(
            self.edit_mode_action.isChecked() and bool(self.project)
        )

    @staticmethod
    def _primary_shortcut(key: str, shift: bool = False) -> QtGui.QKeySequence:
        # QKeySequence's portable Ctrl is the platform primary modifier: Qt
        # renders and dispatches it as Command on macOS and Ctrl elsewhere.
        return QtGui.QKeySequence(f"Ctrl+{'Shift+' if shift else ''}{key}")

    @staticmethod
    def _standard_shortcuts(
        key: QtGui.QKeySequence.StandardKey,
    ) -> tuple[QtGui.QKeySequence, ...]:
        bindings = tuple(QtGui.QKeySequence.keyBindings(key))
        return bindings[:1] or (QtGui.QKeySequence(key),)

    def _bind_shortcut(
        self,
        action: QtGui.QAction,
        shortcuts: QtGui.QKeySequence | tuple[QtGui.QKeySequence, ...],
        category: str,
        label: str,
        context: QtCore.Qt.ShortcutContext | None = None,
    ) -> None:
        sequences = shortcuts if isinstance(shortcuts, tuple) else (shortcuts,)
        action.setShortcuts(list(sequences))
        if context is not None:
            action.setShortcutContext(context)
        rendered = " / ".join(
            item.toString(QtGui.QKeySequence.SequenceFormat.NativeText)
            for item in sequences
        )
        if rendered:
            action.setToolTip(f"{label}（{rendered}）")
        self._shortcut_help.append((category, label, action))

    def _record_shortcut(
        self, category: str, label: str, *shortcuts: str
    ) -> None:
        self._shortcut_help.append(
            (category, label, tuple(QtGui.QKeySequence(item) for item in shortcuts))
        )

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self.project:
            choice = QtWidgets.QMessageBox.question(
                self,
                "保存项目",
                f"是否在关闭前保存项目“{self.project.root.name}”？",
                QtWidgets.QMessageBox.StandardButton.Save
                | QtWidgets.QMessageBox.StandardButton.Discard
                | QtWidgets.QMessageBox.StandardButton.Cancel,
                QtWidgets.QMessageBox.StandardButton.Save,
            )
            if choice == QtWidgets.QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
            if choice == QtWidgets.QMessageBox.StandardButton.Save:
                try:
                    self._save_editor_document(show_message=False)
                except OSError as error:
                    QtWidgets.QMessageBox.critical(
                        self, "无法保存项目", str(error)
                    )
                    event.ignore()
                    return
        settings = QtCore.QSettings()
        settings.setValue("main/geometry", self.saveGeometry())
        settings.setValue("main/dock_state", self.saveState(UI_STATE_VERSION))
        if self.active_task:
            self.result_database.finish_task(self.active_task["task_id"], "cancelled", "应用已关闭")
        self.julia.shutdown()
        super().closeEvent(event)

    def _build_docks(self) -> None:
        options = (
            QtWidgets.QMainWindow.DockOption.AnimatedDocks
            | QtWidgets.QMainWindow.DockOption.AllowTabbedDocks
            | QtWidgets.QMainWindow.DockOption.ForceTabbedDocks
        )
        grouped = getattr(QtWidgets.QMainWindow.DockOption, "GroupedDragging", None)
        if grouped is not None:
            options |= grouped
        self.setDockOptions(options)
        allowed = (
            QtCore.Qt.DockWidgetArea.LeftDockWidgetArea
            | QtCore.Qt.DockWidgetArea.RightDockWidgetArea
            | QtCore.Qt.DockWidgetArea.BottomDockWidgetArea
        )

        def create(
            name: str,
            title: str,
            widget: QtWidgets.QWidget,
            area: QtCore.Qt.DockWidgetArea = QtCore.Qt.DockWidgetArea.RightDockWidgetArea,
        ) -> QtWidgets.QDockWidget:
            dock = QtWidgets.QDockWidget(title, self)
            dock.setObjectName(name)
            dock.setAllowedAreas(allowed)
            dock.setWidget(widget)
            self.addDockWidget(area, dock)
            return dock

        self.component_dock = create(
            "panel.components",
            "组件",
            self.components,
            QtCore.Qt.DockWidgetArea.LeftDockWidgetArea,
        )
        self.info_dock = create("panel.information", "信息", self.details)
        self.parameter_dock = create("panel.parameters", "参数", self.parameters)
        self.run_dock = create("panel.run", "运行", self.run_log)
        self.tabifyDockWidget(self.info_dock, self.parameter_dock)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.BottomDockWidgetArea, self.run_dock)
        self.info_dock.raise_()

    @QtCore.Slot()
    def _reset_dock_layout(self) -> None:
        self.setCorner(
            QtCore.Qt.Corner.TopLeftCorner,
            QtCore.Qt.DockWidgetArea.LeftDockWidgetArea,
        )
        self.setCorner(
            QtCore.Qt.Corner.TopRightCorner,
            QtCore.Qt.DockWidgetArea.RightDockWidgetArea,
        )
        for dock in (
            self.component_dock,
            self.info_dock,
            self.parameter_dock,
            self.run_dock,
            self.drawing_dock,
        ):
            dock.setFloating(False)
            dock.show()
        self.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, self.info_dock)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, self.parameter_dock)
        self.tabifyDockWidget(self.info_dock, self.parameter_dock)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.BottomDockWidgetArea, self.run_dock)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.LeftDockWidgetArea, self.component_dock)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.TopDockWidgetArea, self.drawing_dock)
        self.drawing_dock.setVisible(
            self.edit_mode_action.isChecked() and bool(self.project)
        )
        self.info_dock.raise_()

    def _show_run_panel(self) -> None:
        self.run_dock.show()
        self.run_dock.raise_()

    def _append_colored_run_log(self, message: str, color: str) -> None:
        cursor = self.run_log.textCursor()
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
        text_format = QtGui.QTextCharFormat()
        text_format.setForeground(QtGui.QColor(color))
        cursor.insertText(message + "\n", text_format)
        self.run_log.setTextCursor(cursor)
        self.run_log.ensureCursorVisible()

    def _welcome_widget(self) -> QtWidgets.QWidget:
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)
        layout.addStretch()
        title = QtWidgets.QLabel(f"{APPLICATION_NAME}\n项目图形化展示与编辑器")
        title.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        font = title.font()
        font.setPointSize(24)
        font.setBold(True)
        title.setFont(font)
        hint = QtWidgets.QLabel("打开包含 manifest.toml 的导出项目目录")
        hint.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        button = QtWidgets.QPushButton("打开项目…")
        button.setFixedWidth(180)
        button.clicked.connect(self.open_project)
        row = QtWidgets.QHBoxLayout()
        row.addStretch()
        row.addWidget(button)
        row.addStretch()
        layout.addWidget(title)
        layout.addSpacing(18)
        layout.addWidget(hint)
        layout.addSpacing(18)
        layout.addLayout(row)
        layout.addStretch()
        return widget

    def _build_toolbar(self) -> None:
        toolbar = QtWidgets.QToolBar("项目")
        toolbar.setObjectName("toolbar.project")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        self.new_project_action = QtGui.QAction("新建项目…", self)
        self._bind_shortcut(
            self.new_project_action,
            self._standard_shortcuts(QtGui.QKeySequence.StandardKey.New),
            "项目",
            "新建项目",
        )
        self.new_project_action.triggered.connect(self.new_project)
        toolbar.addAction(self.new_project_action)

        self.open_action = QtGui.QAction("打开项目", self)
        self._bind_shortcut(
            self.open_action,
            self._standard_shortcuts(QtGui.QKeySequence.StandardKey.Open),
            "项目",
            "打开项目",
        )
        self.open_action.triggered.connect(self.open_project)
        toolbar.addAction(self.open_action)

        self.precompile_action = QtGui.QAction("预编译", self)
        self.precompile_action.triggered.connect(self.precompile_backend)
        toolbar.addAction(self.precompile_action)

        self.simulation_settings_action = QtGui.QAction("仿真设置…", self)
        self.simulation_settings_action.triggered.connect(self.edit_simulation_settings)
        self.simulation_settings_action.setEnabled(False)

        self.run_simulation_action = QtGui.QAction("运行仿真", self)
        self.run_simulation_action.triggered.connect(self.run_simulation)
        self.run_simulation_action.setEnabled(False)
        toolbar.addAction(self.run_simulation_action)

        self.stop_simulation_action = QtGui.QAction("终止仿真", self)
        self.stop_simulation_action.triggered.connect(self._cancel_simulation)
        self.stop_simulation_action.setEnabled(False)
        toolbar.addAction(self.stop_simulation_action)
        self.data_manager_action = QtGui.QAction("数据管理器", self)
        self.data_manager_action.triggered.connect(self.show_data_manager)
        toolbar.addAction(self.data_manager_action)
        self.edit_mode_action = QtGui.QAction("编辑模式", self)
        self.edit_mode_action.setCheckable(True)
        self.edit_mode_action.setEnabled(False)
        self.edit_mode_action.setToolTip("启用模型结构编辑；布局拖动不受此开关影响")
        self.edit_mode_action.toggled.connect(self._edit_mode_changed)
        self.edit_mode_action.toggled.connect(self.components.edit_mode.setChecked)
        self.components.edit_mode.toggled.connect(self.edit_mode_action.setChecked)
        self.custom_models_action = QtGui.QAction("模型管理器…", self)
        self.custom_models_action.setEnabled(False)
        self.custom_models_action.triggered.connect(self.show_custom_model_manager)
        toolbar.addSeparator()

        self.home_action = QtGui.QAction("返回拓扑", self)
        self.home_action.triggered.connect(self.show_topology)
        self.home_action.setEnabled(False)
        toolbar.addAction(self.home_action)

        self.up_action = QtGui.QAction("上一级", self)
        self.up_action.triggered.connect(self.show_topology)
        self.up_action.setEnabled(False)
        toolbar.addAction(self.up_action)

        self.graph.viewer().qaction_for_undo().setShortcut(QtGui.QKeySequence())
        self.graph.viewer().qaction_for_redo().setShortcut(QtGui.QKeySequence())
        self.undo_action = self.graph.undo_stack().createUndoAction(self, "撤销")
        self._bind_shortcut(
            self.undo_action,
            self._standard_shortcuts(QtGui.QKeySequence.StandardKey.Undo),
            "项目",
            "撤销",
        )
        self.redo_action = self.graph.undo_stack().createRedoAction(self, "重做")
        redo_shortcuts = (
            (QtGui.QKeySequence("Ctrl+Shift+Z"),)
            if sys.platform == "darwin"
            else (QtGui.QKeySequence("Ctrl+Y"), QtGui.QKeySequence("Ctrl+Shift+Z"))
        )
        self._bind_shortcut(
            self.redo_action,
            redo_shortcuts,
            "项目",
            "重做",
        )
        self._build_node_actions()

        self.reset_layout_action = QtGui.QAction("重置布局", self)
        self.reset_layout_action.setEnabled(False)
        self.reset_layout_action.triggered.connect(self.reset_layout)

        self.fit_action = QtGui.QAction("适应画布", self)
        self.fit_action.triggered.connect(self._fit_all_nodes)
        self._bind_shortcut(
            self.fit_action,
            QtGui.QKeySequence("Shift+F"),
            "视图",
            "适应全部网络",
            QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut,
        )
        self.graph.widget.addAction(self.fit_action)
        self.signal_labels_action = QtGui.QAction("端口信号", self)
        self.signal_labels_action.setCheckable(True)
        self.signal_labels_action.setChecked(True)
        self.signal_labels_action.setToolTip("显示或隐藏模块圆点旁的端口信号名称")
        self.signal_labels_action.toggled.connect(self._set_signal_labels_visible)
        toolbar.addAction(self.signal_labels_action)
        toolbar.addSeparator()

        self.breadcrumb = QtWidgets.QLabel("尚未打开项目")
        self.breadcrumb.setMinimumWidth(300)
        toolbar.addWidget(self.breadcrumb)
        spacer = QtWidgets.QWidget()
        spacer.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        toolbar.addWidget(spacer)
        toolbar.addWidget(QtWidgets.QLabel("搜索："))
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("名称、编号、类型或状态")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(270)
        self.search.returnPressed.connect(self.search_nodes)
        self.search.textChanged.connect(self._restore_search_when_empty)
        toolbar.addWidget(self.search)
        self.search_action = QtGui.QAction("搜索", self)
        self.search_action.triggered.connect(self._focus_search)
        self._bind_shortcut(
            self.search_action,
            self._standard_shortcuts(QtGui.QKeySequence.StandardKey.Find),
            "项目",
            "搜索",
        )
        self.addAction(self.search_action)

        self.shortcut_help_action = QtGui.QAction("快捷键…", self)
        self._bind_shortcut(
            self.shortcut_help_action,
            QtGui.QKeySequence("F1"),
            "项目",
            "快捷键说明",
        )
        self.shortcut_help_action.triggered.connect(self._show_shortcut_help)

        self.save_as_action = QtGui.QAction("项目另存为…", self)
        self._bind_shortcut(
            self.save_as_action,
            self._standard_shortcuts(QtGui.QKeySequence.StandardKey.SaveAs),
            "项目",
            "项目另存为",
        )
        self.save_as_action.setEnabled(False)
        self.save_as_action.triggered.connect(self.save_project_as)
        self.close_project_action = QtGui.QAction("关闭项目", self)
        self.close_project_action.setEnabled(False)
        self.close_project_action.triggered.connect(self.close_project)

        self.prepare_network_action = QtGui.QAction("准备当前网络", self)
        self.prepare_network_action.setEnabled(False)
        self.prepare_network_action.triggered.connect(self.prepare_current_network)
        self.rebuild_network_action = QtGui.QAction("重新构建网络…", self)
        self.rebuild_network_action.setEnabled(False)
        self.rebuild_network_action.triggered.connect(self.rebuild_current_network)
        self.backend_manager_action = QtGui.QAction("后端管理器…", self)
        self.backend_manager_action.triggered.connect(self.show_backend_manager)

    def _build_edit_toolbar(self) -> None:
        self.edit_toolbar = QtWidgets.QToolBar("绘图工具")
        self.edit_toolbar.setObjectName("drawing-tools.content")
        self.edit_toolbar.setMovable(False)
        self.edit_toolbar.setFloatable(False)
        self.drawing_dock = QtWidgets.QDockWidget("绘图工具", self)
        self.drawing_dock.setObjectName("panel.drawing-tools")
        self.drawing_dock.setAllowedAreas(
            QtCore.Qt.DockWidgetArea.TopDockWidgetArea
            | QtCore.Qt.DockWidgetArea.LeftDockWidgetArea
        )
        self.drawing_dock.setFeatures(
            QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QtWidgets.QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        self.drawing_dock.setWidget(self.edit_toolbar)
        self.edit_toolbar.show()
        self.drawing_dock.dockLocationChanged.connect(
            lambda area: self.edit_toolbar.setOrientation(
                QtCore.Qt.Orientation.Vertical
                if area == QtCore.Qt.DockWidgetArea.LeftDockWidgetArea
                else QtCore.Qt.Orientation.Horizontal
            )
        )
        self.setCorner(
            QtCore.Qt.Corner.TopLeftCorner,
            QtCore.Qt.DockWidgetArea.LeftDockWidgetArea,
        )
        self.addDockWidget(
            QtCore.Qt.DockWidgetArea.TopDockWidgetArea, self.drawing_dock
        )

        self.edit_modes = QtGui.QActionGroup(self)
        self.edit_modes.setExclusive(True)
        self.select_tool_action = QtGui.QAction("选择", self)
        self.pan_tool_action = QtGui.QAction("平移", self)
        self.connect_tool_action = QtGui.QAction("连接", self)
        for action in (self.select_tool_action, self.pan_tool_action, self.connect_tool_action):
            action.setCheckable(True)
            self.edit_modes.addAction(action)
            self.edit_toolbar.addAction(action)
        self.select_tool_action.setChecked(True)
        self.select_tool_action.triggered.connect(lambda: self._set_edit_tool("select"))
        self.pan_tool_action.triggered.connect(lambda: self._set_edit_tool("pan"))
        self.connect_tool_action.triggered.connect(lambda: self._set_edit_tool("connect"))
        canvas_context = QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut
        for action, key, label in (
            (self.select_tool_action, "V", "选择工具"),
            (self.pan_tool_action, "H", "平移工具"),
            (self.connect_tool_action, "W", "连接工具"),
        ):
            self._bind_shortcut(
                action, QtGui.QKeySequence(key), "绘图工具", label, canvas_context
            )
            self.graph.widget.addAction(action)

        self.edit_toolbar.addSeparator()
        self.disconnect_action = QtGui.QAction("断开", self)
        self.disconnect_action.triggered.connect(self._disconnect_selected_pipes)
        self._bind_shortcut(
            self.disconnect_action,
            QtGui.QKeySequence("X"),
            "绘图工具",
            "断开选中连接",
            canvas_context,
        )
        self.graph.widget.addAction(self.disconnect_action)
        self.edit_toolbar.addAction(self.disconnect_action)
        self.delete_component_action = QtGui.QAction("删除", self)
        delete_shortcuts = list(
            self._standard_shortcuts(QtGui.QKeySequence.StandardKey.Delete)
        )
        if sys.platform == "darwin":
            for key in (QtGui.QKeySequence("Backspace"), QtGui.QKeySequence("Delete")):
                if key not in delete_shortcuts:
                    delete_shortcuts.append(key)
        self._bind_shortcut(
            self.delete_component_action,
            tuple(delete_shortcuts),
            "元件",
            "删除选中内容",
            canvas_context,
        )
        self.graph.widget.addAction(self.delete_component_action)
        self.delete_component_action.triggered.connect(self._delete_selected_components)
        self.edit_toolbar.addAction(self.delete_component_action)
        self.auto_route_action = QtGui.QAction("刷新布线", self)
        self.auto_route_action.triggered.connect(self._redraw_pipes)
        self.edit_toolbar.addAction(self.auto_route_action)
        self.snap_action = QtGui.QAction("吸附网格", self)
        self.snap_action.setCheckable(True)
        self.snap_action.setChecked(True)
        self._bind_shortcut(
            self.snap_action,
            QtGui.QKeySequence("G"),
            "绘图工具",
            "开关网格吸附",
            canvas_context,
        )
        self.graph.widget.addAction(self.snap_action)
        self.edit_toolbar.addAction(self.snap_action)
        self.edit_toolbar.addSeparator()
        self.validate_action = QtGui.QAction("校验", self)
        self.validate_action.triggered.connect(
            lambda checked=False: self._validate_editor()
        )
        self._bind_shortcut(
            self.validate_action,
            QtGui.QKeySequence("F7"),
            "项目",
            "校验模型连接",
            canvas_context,
        )
        self.graph.widget.addAction(self.validate_action)
        self.edit_toolbar.addAction(self.validate_action)
        self.save_editor_action = QtGui.QAction("保存", self)
        self.save_editor_action.setEnabled(False)
        self._bind_shortcut(
            self.save_editor_action,
            self._standard_shortcuts(QtGui.QKeySequence.StandardKey.Save),
            "项目",
            "保存图形编辑",
        )
        self.save_editor_action.triggered.connect(
            lambda checked=False: self._save_editor_document()
        )
        self.edit_toolbar.addAction(self.save_editor_action)
        self.edit_tool_label = QtWidgets.QLabel(" 选择 ")
        self.edit_toolbar.addWidget(self.edit_tool_label)
        self.drawing_dock.hide()

        self.add_component_action = QtGui.QAction("添加器件…", self.graph.widget)
        self.add_component_action.triggered.connect(self._show_add_component_menu)
        self._bind_shortcut(
            self.add_component_action,
            QtGui.QKeySequence("A"),
            "绘图工具",
            "添加器件",
            canvas_context,
        )
        self.graph.widget.addAction(self.add_component_action)
        self._record_shortcut("绘图工具", "临时平移", "Space")
        self._record_shortcut("绘图工具", "取消当前操作", "Esc")

    def _build_menus(self) -> None:
        project_menu = self.menuBar().addMenu("项目")
        project_menu.addAction(self.new_project_action)
        project_menu.addAction(self.open_action)
        project_menu.addSeparator()
        project_menu.addAction(self.save_editor_action)
        project_menu.addAction(self.save_as_action)
        project_menu.addSeparator()
        project_menu.addAction(self.close_project_action)

        edit_menu = self.menuBar().addMenu("编辑")
        edit_menu.addAction(self.undo_action)
        edit_menu.addAction(self.redo_action)
        edit_menu.addSeparator()
        edit_menu.addAction(self.select_all_action)
        edit_menu.addAction(self.delete_component_action)
        edit_menu.addSeparator()
        edit_menu.addAction(self.reset_layout_action)
        edit_menu.addAction(self.custom_models_action)

        simulation_menu = self.menuBar().addMenu("仿真")
        simulation_menu.addAction(self.precompile_action)
        simulation_menu.addAction(self.simulation_settings_action)
        simulation_menu.addSeparator()
        simulation_menu.addAction(self.prepare_network_action)
        simulation_menu.addAction(self.rebuild_network_action)
        simulation_menu.addSeparator()
        simulation_menu.addAction(self.run_simulation_action)
        simulation_menu.addAction(self.stop_simulation_action)
        simulation_menu.addSeparator()
        simulation_menu.addAction(self.backend_manager_action)
        simulation_menu.addAction(self.data_manager_action)

        view_menu = self.menuBar().addMenu("视图")
        for dock in (self.component_dock, self.info_dock, self.parameter_dock, self.run_dock):
            view_menu.addAction(dock.toggleViewAction())
        view_menu.addAction(self.drawing_dock.toggleViewAction())
        view_menu.addSeparator()
        view_menu.addAction(self.home_action)
        view_menu.addAction(self.up_action)
        view_menu.addAction(self.fit_action)
        view_menu.addAction(self.signal_labels_action)
        view_menu.addSeparator()
        reset = view_menu.addAction("恢复默认界面布局")
        reset.triggered.connect(self._reset_dock_layout)

        help_menu = self.menuBar().addMenu("帮助")
        help_menu.addAction(self.shortcut_help_action)

    def _build_node_actions(self) -> None:
        shortcut_context = QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut
        self.rotate_clockwise_action = QtGui.QAction(
            "顺时针旋转 90°", self.graph.widget
        )
        self._bind_shortcut(
            self.rotate_clockwise_action,
            self._primary_shortcut("R"),
            "元件",
            "顺时针旋转 90°",
            shortcut_context,
        )
        self.rotate_clockwise_action.triggered.connect(
            lambda: self._rotate_selected(90)
        )

        self.rotate_counterclockwise_action = QtGui.QAction(
            "逆时针旋转 90°", self.graph.widget
        )
        self._bind_shortcut(
            self.rotate_counterclockwise_action,
            self._primary_shortcut("R", shift=True),
            "元件",
            "逆时针旋转 90°",
            shortcut_context,
        )
        self.rotate_counterclockwise_action.triggered.connect(
            lambda: self._rotate_selected(-90)
        )

        self.mirror_horizontal_action = QtGui.QAction("左右镜像", self.graph.widget)
        self.mirror_horizontal_action.triggered.connect(
            lambda: self._mirror_selected(horizontal=True)
        )

        self.mirror_vertical_action = QtGui.QAction("上下镜像", self.graph.widget)
        self.mirror_vertical_action.triggered.connect(
            lambda: self._mirror_selected(horizontal=False)
        )

        self.reset_orientation_action = QtGui.QAction("重置方向", self.graph.widget)
        self._bind_shortcut(
            self.reset_orientation_action,
            QtGui.QKeySequence("0"),
            "元件",
            "重置方向",
            shortcut_context,
        )
        self.reset_orientation_action.triggered.connect(self._reset_selected_orientation)

        self.reset_size_action = QtGui.QAction("重置大小", self.graph.widget)
        self._bind_shortcut(
            self.reset_size_action,
            QtGui.QKeySequence("Shift+0"),
            "元件",
            "重置大小",
            shortcut_context,
        )
        self.reset_size_action.triggered.connect(self._reset_selected_size)

        self.focus_nodes_action = QtGui.QAction("聚焦到元件", self.graph.widget)
        self._bind_shortcut(
            self.focus_nodes_action,
            QtGui.QKeySequence("F"),
            "视图",
            "聚焦选中元件",
            shortcut_context,
        )
        self.focus_nodes_action.triggered.connect(self._focus_selected_nodes)

        self.open_node_action = QtGui.QAction("打开元件", self.graph.widget)
        self._bind_shortcut(
            self.open_node_action,
            QtGui.QKeySequence("Return"),
            "元件",
            "打开元件/进入层级",
            shortcut_context,
        )
        self.open_node_action.triggered.connect(self._open_selected_node)

        self.show_model_action = QtGui.QAction("展示模型", self.graph.widget)
        self._bind_shortcut(
            self.show_model_action,
            QtGui.QKeySequence("M"),
            "元件",
            "展示数学模型",
            shortcut_context,
        )
        self.show_model_action.triggered.connect(self._show_selected_model)

        self.resize_mode_action = QtGui.QAction("调整元件大小", self.graph.widget)
        self._bind_shortcut(
            self.resize_mode_action,
            QtGui.QKeySequence("E"),
            "元件",
            "进入/退出尺寸调整",
            shortcut_context,
        )
        self.resize_mode_action.triggered.connect(self._toggle_resize_mode)

        self.select_all_action = QtGui.QAction("全选画布元件", self.graph.widget)
        self._bind_shortcut(
            self.select_all_action,
            self._standard_shortcuts(QtGui.QKeySequence.StandardKey.SelectAll),
            "元件",
            "全选画布元件",
            shortcut_context,
        )
        self.select_all_action.triggered.connect(self._select_all_nodes)

        self.zoom_in_action = QtGui.QAction("放大", self.graph.widget)
        self._bind_shortcut(
            self.zoom_in_action,
            (QtGui.QKeySequence("+"), QtGui.QKeySequence("=")),
            "视图",
            "放大",
            shortcut_context,
        )
        self.zoom_in_action.triggered.connect(lambda: self._zoom_canvas(120.0))
        self.zoom_out_action = QtGui.QAction("缩小", self.graph.widget)
        self._bind_shortcut(
            self.zoom_out_action,
            QtGui.QKeySequence("-"),
            "视图",
            "缩小",
            shortcut_context,
        )
        self.zoom_out_action.triggered.connect(lambda: self._zoom_canvas(-120.0))
        self.reset_zoom_action = QtGui.QAction("恢复 100% 缩放", self.graph.widget)
        self._bind_shortcut(
            self.reset_zoom_action,
            QtGui.QKeySequence("1"),
            "视图",
            "恢复 100% 缩放",
            shortcut_context,
        )
        self.reset_zoom_action.triggered.connect(self._reset_canvas_zoom)

        self.nudge_actions: list[QtGui.QAction] = []
        directions = (
            ("左移", "Left", -1.0, 0.0),
            ("右移", "Right", 1.0, 0.0),
            ("上移", "Up", 0.0, -1.0),
            ("下移", "Down", 0.0, 1.0),
        )
        for label, key, dx, dy in directions:
            for prefix, distance, suffix in (("", 1.0, "1 px"), ("Shift+", 20.0, "一个网格")):
                action = QtGui.QAction(f"{label} {suffix}", self.graph.widget)
                self._bind_shortcut(
                    action,
                    QtGui.QKeySequence(f"{prefix}{key}"),
                    "元件",
                    f"{label} {suffix}",
                    shortcut_context,
                )
                action.triggered.connect(
                    lambda checked=False, x=dx * distance, y=dy * distance: self._nudge_selected(x, y)
                )
                self.nudge_actions.append(action)

        for action in (
            self.rotate_clockwise_action,
            self.rotate_counterclockwise_action,
            self.mirror_horizontal_action,
            self.mirror_vertical_action,
            self.reset_orientation_action,
            self.reset_size_action,
            self.focus_nodes_action,
            self.open_node_action,
            self.show_model_action,
            self.resize_mode_action,
            self.select_all_action,
            self.zoom_in_action,
            self.zoom_out_action,
            self.reset_zoom_action,
            *self.nudge_actions,
        ):
            self.graph.widget.addAction(action)
        self._update_node_actions()

    @QtCore.Slot()
    def new_project(self) -> None:
        if self._project_switch_blocked():
            return
        destination = self._choose_project_destination("新建项目", "未命名项目")
        if not destination:
            return
        try:
            destination.mkdir(parents=True)
            (destination / "equations").mkdir()
            (destination / "manifest.toml").write_text(
                'schema_version = 1\n'
                'model_type = "PowerSimulationsDynamics.ResidualModel"\n'
                'frequency_reference_type = "PowerSimulationsDynamics.ReferenceBus"\n',
                encoding="utf-8",
            )
            (destination / "topology.toml").write_text(
                "buses = []\nbranches = []\ninjections = []\n",
                encoding="utf-8",
            )
            (destination / "graph.toml").write_text(
                "variable_count = 0\ninjectors = []\n",
                encoding="utf-8",
            )
            (destination / "equations/index.toml").write_text(
                "components = []\n",
                encoding="utf-8",
            )
            EditorDocumentStore.save(destination, EditorDocument())
            self._load_project(destination)
        except (OSError, ProjectLoadError) as error:
            QtWidgets.QMessageBox.critical(self, "无法新建项目", str(error))
            return
        self.edit_mode_action.setChecked(True)
        self.statusBar().showMessage(f"已新建项目：{destination}", 5000)

    def _choose_project_destination(
        self, title: str, suggested_name: str
    ) -> Path | None:
        start = str(self.project.root.parent if self.project else Path.cwd())
        parent = QtWidgets.QFileDialog.getExistingDirectory(
            self, f"{title}—选择保存位置", start
        )
        if not parent:
            return None
        name, accepted = QtWidgets.QInputDialog.getText(
            self, title, "项目名称：", text=suggested_name
        )
        name = name.strip()
        if not accepted or not name:
            return None
        if Path(name).name != name or name in {".", ".."}:
            QtWidgets.QMessageBox.warning(self, title, "项目名称不能包含路径分隔符。")
            return None
        destination = Path(parent).expanduser().resolve() / name
        if destination.exists():
            QtWidgets.QMessageBox.warning(self, title, f"目标已存在：{destination}")
            return None
        return destination

    def _project_switch_blocked(self) -> bool:
        if not self.active_task and not self.julia.busy:
            return False
        QtWidgets.QMessageBox.information(
            self, "后端任务运行中", "请先终止或等待当前任务完成后再切换项目。"
        )
        return True

    @QtCore.Slot()
    def open_project(self) -> None:
        if self._project_switch_blocked():
            return
        start = str(self.project.root.parent if self.project else Path.cwd())
        directory = QtWidgets.QFileDialog.getExistingDirectory(
            self, "选择 PowerSimulationsDynamics 导出项目", start
        )
        if not directory:
            return
        try:
            self._load_project(Path(directory))
        except ProjectLoadError as error:
            QtWidgets.QMessageBox.critical(self, "无法打开项目", str(error))
            return

    def _load_project(self, directory: Path) -> None:
        project = ProjectLoader.load(directory)
        self.editor_document, migrated = upgrade_editor_instances(
            project.editor_document or EditorDocument()
        )
        if migrated:
            EditorDocumentStore.save(project.root, self.editor_document)
            project = replace(project, editor_document=self.editor_document)
        self.project = project
        self.custom_model_manager.set_project(project)
        self.components.set_project(project)
        model_type = str(project.manifest.get("model_type", "ResidualModel")).rsplit(".", 1)[-1]
        if model_type in {"ResidualModel", "MassMatrixModel"}:
            self.simulation_settings.model = model_type
            self.simulation_settings.solver = (
                "IDA" if model_type == "ResidualModel" else "Rodas4"
            )
        frequency = str(
            project.manifest.get("frequency_reference_type", "ReferenceBus")
        ).rsplit(".", 1)[-1]
        if frequency in {"ReferenceBus", "ConstantFrequency"}:
            self.simulation_settings.frequency_reference = frequency
        self.parameter_sweeps.clear()
        self.data_manager.set_topology(project)
        self.backend_manager.set_project(
            project.root,
            self._effective_fingerprint(),
        )
        self._prepare_on_ready = self.editor_document == EditorDocument()
        self.current_device = None
        self.current_view_key = None
        self.layout_states.clear()
        self.skip_layout_capture = True
        self.canvas_stack.setCurrentIndex(1)
        self.show_topology()
        self._backend_state_changed(self.julia.state, self.julia.state_message)
        self.statusBar().showMessage(f"已打开：{project.root}")
        if self.julia.state == "Ready" and self._prepare_on_ready:
            QtCore.QTimer.singleShot(0, self.prepare_current_network)

    @QtCore.Slot()
    def save_project_as(self) -> None:
        if not self.project or self._project_switch_blocked():
            return
        destination = self._choose_project_destination(
            "项目另存为", f"{self.project.root.name}-副本"
        )
        if not destination:
            return
        source = self.project.root.resolve()
        if destination.is_relative_to(source):
            QtWidgets.QMessageBox.warning(
                self, "项目另存为", "新项目不能保存在当前项目目录内。"
            )
            return
        self._save_editor_document(show_message=False)
        try:
            shutil.copytree(
                source,
                destination,
                ignore=shutil.ignore_patterns("compiled", "__pycache__", ".DS_Store"),
            )
            self._load_project(destination)
        except (OSError, ProjectLoadError) as error:
            QtWidgets.QMessageBox.critical(self, "项目另存为失败", str(error))
            return
        self.statusBar().showMessage(f"项目已另存为：{destination}", 5000)

    @QtCore.Slot()
    def close_project(self) -> None:
        if not self.project or self._project_switch_blocked():
            return
        self.edit_mode_action.setChecked(False)
        self.skip_layout_capture = True
        self._clear_graph()
        self.project = None
        self.editor_document = EditorDocument()
        self.current_device = None
        self.current_view_key = None
        self.layout_states.clear()
        self.parameter_sweeps.clear()
        self.components.set_project(None)
        self.data_manager.set_topology(None)
        self.backend_manager.set_project(None)
        self.custom_model_manager.hide()
        self.canvas_stack.setCurrentIndex(0)
        self.details.setHtml(self._welcome_details())
        self.parameters.set_nodes([])
        self.breadcrumb.setText("尚未打开项目")
        self.home_action.setEnabled(False)
        self.up_action.setEnabled(False)
        self.reset_layout_action.setEnabled(False)
        self._backend_state_changed(self.julia.state, self.julia.state_message)
        self.statusBar().showMessage("请选择一个导出的项目目录")

    @QtCore.Slot()
    def precompile_backend(self) -> None:
        if self.julia.busy:
            return
        self.run_log.clear()
        self._show_run_panel()
        self.statusBar().showMessage("正在提交 Julia 环境预编译…")
        self.julia.precompile()

    @QtCore.Slot()
    def edit_simulation_settings(self) -> None:
        if not self.project or self.julia.busy:
            return
        dialog = SimulationSettingsDialog(self.simulation_settings, self)
        dialog.exec()

    @QtCore.Slot()
    def run_simulation(self) -> None:
        if not self.project or not self.project.system or self.julia.busy:
            return
        if not self._editor_backend_ready():
            return
        sweeps = list(self.parameter_sweeps.values())
        dialog = SimulationTaskDialog(
            self.project.root.name, sweeps, self.result_database.path, self
        )
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        self.run_log.clear()
        self._show_run_panel()
        task_id, task_name = self.result_database.create_task(
            self.project,
            dialog.name.text().strip(),
            dialog.combination_mode,
            len(dialog.cases),
            asdict(self.simulation_settings),
        )
        self.active_task = {
            "task_id": task_id,
            "name": task_name,
            "sweeps": sweeps,
            "cases": dialog.cases,
            "next": 0,
            "run_id": None,
            "cancelled": False,
            "failures": 0,
            "successes": 0,
            "waiting": False,
        }
        self.data_manager.change_button.setEnabled(False)
        self._backend_state_changed(self.julia.state, self.julia.state_message)
        self.run_log.appendPlainText(f"[任务] {task_name} · 共 {len(dialog.cases)} 个实例")
        self._start_next_case()

    @QtCore.Slot()
    def prepare_current_network(self) -> None:
        if not self.project or not self.project.system or self.julia.busy:
            return
        if not self._editor_backend_ready():
            return
        self._prepare_on_ready = False
        self.run_log.clear()
        self._show_run_panel()
        self.statusBar().showMessage("正在准备当前网络计算图…")
        self.julia.prepare_network(
            self.project.root,
            self.project.system,
            self.simulation_settings,
            tuple(self.project.custom_models),
            self.editor_document,
        )

    @QtCore.Slot()
    def rebuild_current_network(self) -> None:
        if not self.project or not self.project.system or self.julia.busy:
            return
        if not self._editor_backend_ready():
            return
        if QtWidgets.QMessageBox.question(
            self,
            "重新构建计算图",
            "将覆盖应用缓存中当前网络指纹对应的计算图。\n导出项目不会被修改。是否继续？",
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self.run_log.clear()
        self._show_run_panel()
        self.julia.rebuild_network(
            self.project.root,
            self.project.system,
            self.simulation_settings,
            tuple(self.project.custom_models),
            self.editor_document,
        )

    @QtCore.Slot()
    def show_backend_manager(self) -> None:
        self.backend_manager.show()
        self.backend_manager.raise_()
        self.backend_manager.activateWindow()

    @QtCore.Slot()
    def show_data_manager(self) -> None:
        self.data_manager.refresh()
        self.data_manager.show()
        self.data_manager.raise_()
        self.data_manager.activateWindow()

    @QtCore.Slot()
    def show_custom_model_manager(self) -> None:
        if not self.project:
            return
        self.custom_model_manager.set_project(self.project)
        self.custom_model_manager.end_instantiation()
        self.custom_model_manager.set_editing_enabled(
            self.edit_mode_action.isChecked() and not self.julia.busy and not self.active_task
        )
        self.custom_model_manager.show()
        self.custom_model_manager.raise_()
        self.custom_model_manager.activateWindow()

    @QtCore.Slot(bool)
    def _edit_mode_changed(self, enabled: bool) -> None:
        self.custom_models_action.setEnabled(
            bool(self.project)
        )
        if hasattr(self, "custom_model_manager"):
            self.custom_model_manager.set_editing_enabled(
                enabled and not self.julia.busy and not self.active_task
            )
        self.statusBar().showMessage(
            "模型结构编辑已启用" if enabled else "模型结构编辑已关闭", 2500
        )
        self.drawing_dock.setVisible(enabled and bool(self.project))
        self.drawing_dock.toggleViewAction().setEnabled(enabled and bool(self.project))
        if not enabled:
            self._cancel_placement()
        self._apply_editability()

    def _apply_editability(self) -> None:
        enabled = (
            self.edit_mode_action.isChecked()
            and not self.julia.busy
            and not self.active_task
        )
        connect_enabled = enabled and self.edit_tool == "connect"
        if not connect_enabled:
            for node in self.nodes.values():
                temporary_inputs = [
                    port
                    for port in node.input_ports()
                    if getattr(port.view, "temporary_connection_handle", False)
                ]
                temporary_outputs = [
                    port
                    for port in node.output_ports()
                    if getattr(port.view, "temporary_connection_handle", False)
                ]
                if not temporary_inputs and not temporary_outputs:
                    continue
                size = node.size
                node.set_port_deletion_allowed(True)
                for port in temporary_inputs:
                    port.set_locked(False, connected_ports=False, push_undo=False)
                    node.delete_input(port)
                for port in temporary_outputs:
                    port.set_locked(False, connected_ports=False, push_undo=False)
                    node.delete_output(port)
                node.set_port_deletion_allowed(False)
                node.view.set_node_size(*size)
                node._apply_transform_layout()
        if connect_enabled and self.current_view_key == "topology":
            for node in self.nodes.values():
                is_bus = isinstance(node.payload, Bus) or getattr(node.payload, "kind", "") == "bus"
                if not is_bus:
                    continue
                size = node.size
                names = ("__edit_in", "__edit_out")
                if not node.get_input(names[0]):
                    node.add_bus_input(names[0], multi=True, temporary=True)
                if not node.get_output(names[1]):
                    node.add_bus_output(names[1], multi=True, temporary=True)
                node.view.set_node_size(*size)
                node._apply_transform_layout()
                node.bus_connection_handles_enabled = True
        for node in self.nodes.values():
            node.bus_connection_handles_enabled = bool(
                connect_enabled and node.is_bus_node
            )
            for port in (*node.input_ports(), *node.output_ports()):
                editor_port = (
                    node.editor_added and not node.is_bus_node
                ) or bool(getattr(port.view, "editor_anchor", False))
                port.set_locked(not (connect_enabled and editor_port), connected_ports=False, push_undo=False)
                if getattr(port.view, "editor_anchor", False):
                    # Connection handles are transient affordances. They appear only
                    # while the pointer is over a Bus and never become real terminals.
                    port.view.setVisible(bool(port.connected_ports()))
                if getattr(port.view, "bus_edge_port", False):
                    port.view.edge_drag_enabled = bool(
                        enabled
                        and self.edit_tool in {"select", "connect"}
                        and not getattr(port.view, "temporary_connection_handle", False)
                    )
        self.edit_toolbar.setEnabled(enabled)
        self.select_tool_action.setEnabled(bool(self.project))
        self.pan_tool_action.setEnabled(bool(self.project))
        for action in (
            self.connect_tool_action,
            self.disconnect_action,
            self.delete_component_action,
            self.snap_action,
            self.validate_action,
            self.save_editor_action,
            self.add_component_action,
        ):
            action.setEnabled(enabled)

    def _set_edit_tool(self, tool: str) -> None:
        self._cancel_placement()
        self.edit_tool = tool
        self.read_only_filter.permanent_pan = tool == "pan"
        self.read_only_filter.space_pan = tool == "pan"
        labels = {"select": "选择", "pan": "平移（拖动画布）", "connect": "连接（从端口拖到 Bus）"}
        self.edit_tool_label.setText(f" {labels[tool]} ")
        cursor = (
            QtCore.Qt.CursorShape.OpenHandCursor
            if tool == "pan"
            else QtCore.Qt.CursorShape.CrossCursor
            if tool == "connect"
            else QtCore.Qt.CursorShape.ArrowCursor
        )
        self.read_only_filter.base_cursor = cursor
        self.graph.viewer().viewport().setCursor(cursor)
        self._apply_editability()

    @QtCore.Slot()
    def _show_add_component_menu(self) -> None:
        if not self.project or not self.edit_mode_action.isChecked() or self.julia.busy:
            self.statusBar().showMessage("请先启用编辑模式并等待后端空闲", 3000)
            return
        menu = QtWidgets.QMenu(self)
        choices = (
            ("Bus", "bus"),
            ("线路或变压器", "branch"),
            ("发电机", "generator"),
            ("逆变器", "inverter"),
            ("负荷", "load"),
            ("电源", "source"),
            ("自定义整体器件", "custom"),
        )
        for label, kind in choices:
            menu.addAction(label, lambda checked=False, value=kind: self._add_component_requested(value))
        menu.exec(QtGui.QCursor.pos())

    @QtCore.Slot()
    def _delete_from_canvas(self) -> None:
        if self.delete_component_action.isEnabled():
            self.delete_component_action.trigger()

    @QtCore.Slot()
    def _escape_canvas(self) -> None:
        if self.resize_node_key:
            self._deactivate_resize_mode()
            return
        if self.edit_tool != "select":
            self.select_tool_action.setChecked(True)
            self._set_edit_tool("select")
            return
        for node in self._selected_nodes():
            node.view.setSelected(False)
        self._selection_changed([], [])

    @QtCore.Slot(str)
    def _add_component_requested(self, kind: str) -> None:
        if not self.project or not self.edit_mode_action.isChecked() or self.julia.busy:
            self.statusBar().showMessage("请先启用编辑模式并等待后端空闲", 3000)
            return
        if self.current_view_key != "topology":
            self.show_topology()
        self.custom_model_manager.set_project(self.project)
        self.custom_model_manager.set_editing_enabled(True)
        self.custom_model_manager.begin_instantiation(kind)

    @QtCore.Slot(object)
    def _instantiate_requested(self, descriptor: dict[str, Any]) -> None:
        if descriptor.get("kind") == "internal":
            self._replace_internal_slot(descriptor)
            return
        descriptor = dict(descriptor)
        self.placement_descriptor = descriptor
        self.read_only_filter.placement_active = True
        self.read_only_filter.base_cursor = QtCore.Qt.CursorShape.CrossCursor
        self.graph.viewer().viewport().setCursor(QtCore.Qt.CursorShape.CrossCursor)
        self.edit_tool_label.setText(f" 放置 {descriptor.get('name') or descriptor.get('model_ref')} · 单击画布，Esc 取消 ")
        self.statusBar().showMessage("单击画布放置器件；右键或 Esc 取消")

    @QtCore.Slot(object)
    def _place_instance(self, point: QtCore.QPointF) -> None:
        if not self.project or not self.placement_descriptor:
            return
        descriptor = self.placement_descriptor
        kind = str(descriptor["kind"])
        model_ref = str(descriptor["model_ref"])
        if kind == "branch":
            kind = "transformer" if "transformer" in model_ref.casefold() else "line"
        elif kind == "injection":
            kind = {
                "DynamicGenerator": "generator",
                "DynamicInverter": "inverter",
                "StandardLoad": "load",
                "Source": "source",
            }.get(model_ref, "custom")
        if self.snap_action.isChecked():
            x, y = round(point.x() / 20.0) * 20.0, round(point.y() / 20.0) * 20.0
        else:
            x, y = point.x(), point.y()
        existing_names = {
            *(item.name for item in self.project.buses),
            *(item.name for item in self.project.branches),
            *(item.name for item in self.project.injections),
            *(item.name for item in self.editor_document.instances),
        }
        base_name = str(descriptor.get("name") or model_ref)
        number = 1
        name = base_name
        while name in existing_names:
            number += 1
            name = f"{base_name}-{number}"
        bus_number = None
        if kind == "bus":
            used = [item.number for item in self.project.buses] + [
                item.bus_number for item in self.editor_document.instances if item.bus_number is not None
            ]
            bus_number = max(used, default=0) + 1
            name = f"Bus{bus_number}"
        ports = tuple(descriptor.get("ports") or default_ports(kind))
        instance = EditorInstance.new(
            name,
            kind,
            model_ref,
            ports,
            (x, y),
            parameters={
                **default_instance_parameters(model_ref),
                "available": True,
                **dict(descriptor.get("parameters", {})),
            },
            bus_number=bus_number,
            backend_supported=bool(descriptor.get("backend_supported", True)),
        )
        before = self.editor_document
        after = before.with_instance(instance)
        self._cancel_placement()
        self.graph.undo_stack().push(
            EditorDocumentCommand(before, after, f"添加 {name}", self._apply_editor_document)
        )
        QtCore.QTimer.singleShot(0, lambda: self._activate_component_key(instance.key))

    @QtCore.Slot()
    def _cancel_placement(self) -> None:
        self.placement_descriptor = None
        self.read_only_filter.placement_active = False
        if hasattr(self, "edit_tool_label"):
            self.edit_tool_label.setText(" 选择 ")
        if hasattr(self, "graph"):
            cursor = (
                QtCore.Qt.CursorShape.OpenHandCursor
                if self.edit_tool == "pan"
                else QtCore.Qt.CursorShape.CrossCursor
                if self.edit_tool == "connect"
                else QtCore.Qt.CursorShape.ArrowCursor
            )
            self.read_only_filter.base_cursor = cursor
            self.graph.viewer().viewport().setCursor(cursor)

    def _apply_editor_document(self, document: EditorDocument, rebuild: bool = True) -> None:
        if not self.project:
            return
        self.editor_document = document
        self.project = replace(self.project, editor_document=document)
        EditorDocumentStore.save(self.project.root, document)
        self.components.set_project(self.project)
        self.backend_manager.set_project(self.project.root, self._effective_fingerprint())
        self._prepare_on_ready = False
        if rebuild:
            current_device = self.current_device
            self.skip_layout_capture = True
            self._syncing_editor = True
            try:
                self.show_device(current_device) if current_device else self.show_topology()
            finally:
                self._syncing_editor = False
        self._apply_editability()

    @QtCore.Slot()
    def _save_editor_document(self, show_message: bool = True) -> None:
        if not self.project:
            return
        EditorDocumentStore.save(self.project.root, self.editor_document)
        if show_message:
            self.statusBar().showMessage("编辑模型已保存到 .psid_gui/model.json", 3500)

    @QtCore.Slot(object, object)
    def _port_connected(self, input_port: Any, output_port: Any) -> None:
        if self._syncing_editor or not self.project or not self.edit_mode_action.isChecked():
            return
        input_node = input_port.node()
        output_node = output_port.node()
        endpoints = (input_node, output_node)
        buses = [node for node in endpoints if isinstance(node.payload, Bus) or getattr(node.payload, "kind", "") == "bus"]
        editor_nodes = [node for node in endpoints if node.editor_added and getattr(node.payload, "kind", "") != "bus"]
        if len(buses) != 1 or len(editor_nodes) != 1:
            self._syncing_editor = True
            input_port.disconnect_from(output_port, push_undo=False)
            self._syncing_editor = False
            self.statusBar().showMessage("拓扑连接必须位于一个 Bus 与一个新增器件端口之间", 4000)
            return
        editor_node = editor_nodes[0]
        editor_port = input_port if input_node is editor_node else output_port
        definition = editor_node.port_definitions.get(editor_port.name())
        if definition and definition.domain != "AC_BUS":
            self._syncing_editor = True
            input_port.disconnect_from(output_port, push_undo=False)
            self._syncing_editor = False
            self.statusBar().showMessage(f"{definition.domain} 端口不能连接到 AC Bus", 4000)
            return
        connection = EditorConnection.new(
            output_node.viewer_key,
            output_port.name(),
            input_node.viewer_key,
            input_port.name(),
        )
        # Rebuild replaces the transient Bus handle with a dedicated terminal
        # belonging to this actual connection.
        self._apply_editor_document(self.editor_document.with_connection(connection), rebuild=True)
        self._validate_editor(show_dialog=False)

    @QtCore.Slot(object, object)
    def _port_disconnected(self, input_port: Any, output_port: Any) -> None:
        if self._syncing_editor or not self.project or not self.edit_mode_action.isChecked():
            return
        endpoints = (
            (output_port.node().viewer_key, output_port.name()),
            (input_port.node().viewer_key, input_port.name()),
        )
        editor_endpoint = next(
            (
                endpoint
                for endpoint, port in ((endpoints[0], output_port), (endpoints[1], input_port))
                if port.node().editor_added and getattr(port.node().payload, "kind", "") != "bus"
            ),
            None,
        )
        if editor_endpoint:
            after = replace(
                self.editor_document,
                connections=tuple(
                    item
                    for item in self.editor_document.connections
                    if (item.first_key, item.first_port) != editor_endpoint
                    and (item.second_key, item.second_port) != editor_endpoint
                ),
            )
        else:
            after = self.editor_document.without_connection(*endpoints[0], *endpoints[1])
        if after != self.editor_document:
            # Rebuild removes the now-unused dedicated Bus terminal.
            self._apply_editor_document(after, rebuild=True)

    @QtCore.Slot(object, str, object)
    def _graph_property_changed(self, node: ViewerNode, name: str, value: Any) -> None:
        if self._syncing_editor or name != "pos" or not self.edit_mode_action.isChecked():
            return
        position = tuple(float(item) for item in value)
        document = self.editor_document
        instance = document.instance_by_key.get(node.viewer_key)
        if instance:
            document = document.replace_instance(replace(instance, position=position))
        geometry = dict(document.geometry)
        geometry[node.viewer_key] = (
            *position,
            *node.size,
            node.orientation,
            node.mirror_horizontal,
            node.mirror_vertical,
        )
        self._apply_editor_document(replace(document, geometry=geometry), rebuild=False)

    def _disconnect_selected_pipes(self) -> None:
        if not self.edit_mode_action.isChecked():
            return
        selected = list(self.graph.viewer().selected_pipes())
        removable_endpoints: set[tuple[str, str]] = set()
        for pipe in selected:
            for port in (pipe.input_port, pipe.output_port):
                if not port:
                    continue
                node = getattr(port.node, "viewer_node", None)
                if (
                    node
                    and node.editor_added
                    and not node.is_bus_node
                ):
                    removable_endpoints.add((node.viewer_key, port.name))
        if not removable_endpoints:
            self.statusBar().showMessage(
                "请选择新增器件与 Bus 之间的可编辑连线", 3500
            )
            return
        before = self.editor_document
        after = replace(
            before,
            connections=tuple(
                connection
                for connection in before.connections
                if (connection.first_key, connection.first_port) not in removable_endpoints
                and (connection.second_key, connection.second_port) not in removable_endpoints
            ),
        )
        if after == before:
            self.statusBar().showMessage("选中连线不属于可编辑拓扑", 3500)
            return
        self.graph.undo_stack().push(
            EditorDocumentCommand(
                before,
                after,
                f"断开 {len(before.connections) - len(after.connections)} 条连接",
                self._apply_editor_document,
            )
        )

    def _delete_selected_components(self) -> None:
        if not self.edit_mode_action.isChecked():
            return
        self._disconnect_selected_pipes()
        if self.current_device:
            return
        selected = self._selected_nodes()
        if not selected:
            return
        before = self.editor_document
        after = before
        for node in selected:
            after = after.without_key(node.viewer_key, baseline=not node.editor_added)
            if isinstance(node.payload, Bus) and self.project:
                for branch in self.project.branches:
                    if node.payload.number in {branch.from_bus, branch.to_bus}:
                        after = after.without_key(f"branch:{branch.name}", baseline=True)
                for injection in self.project.injections:
                    if injection.bus == node.payload.number:
                        after = after.without_key(f"injection:{injection.name}", baseline=True)
        self.graph.undo_stack().push(
            EditorDocumentCommand(before, after, f"删除 {len(selected)} 个器件", self._apply_editor_document)
        )

    def _redraw_pipes(self) -> None:
        for pipe in self.graph.viewer().all_pipes():
            pipe.draw_path(pipe.input_port, pipe.output_port)

    def _validate_editor(self, show_dialog: bool = True) -> bool:
        if not self.project:
            return False
        issues = self.editor_document.validate(self.project)
        errors = [item for item in issues if item.severity == "error"]
        if show_dialog:
            self._show_run_panel()
            if errors:
                for item in errors:
                    self._append_colored_run_log(
                        f"[拓扑校验] 错误：{item.message}", "#d94141"
                    )
            else:
                self._append_colored_run_log(
                    "[拓扑校验] 通过：拓扑结构完整，可以准备计算图。",
                    "#24a148",
                )
            QtWidgets.QMessageBox.information(
                self,
                "模型校验",
                "结构校验通过，可以准备计算图。" if not errors else "发现以下问题：\n\n" + "\n".join(f"• {item.message}" for item in errors),
            )
        self.statusBar().showMessage(
            "结构校验通过" if not errors else f"结构不完整：{len(errors)} 个错误",
            3500,
        )
        return not errors

    def _editor_backend_ready(self) -> bool:
        if not self._validate_editor(show_dialog=False):
            QtWidgets.QMessageBox.warning(self, "无法准备网络", "请先修复画布中的结构校验错误。")
            return False
        if self.editor_document.deleted_keys or self.editor_document.slot_overrides:
            QtWidgets.QMessageBox.warning(
                self,
                "编辑模型尚未物化",
                "当前版本已支持新增单端口设备和自定义多端口器件进入 Julia 网络，"
                "但删除已有器件及替换内部槽位尚未物化。\n"
                "请撤销这些操作后再准备网络，避免仿真忽略编辑内容。",
            )
            return False
        return True

    @QtCore.Slot(object)
    def _custom_models_changed(self, models: tuple[CustomModel, ...]) -> None:
        if not self.project:
            return
        previous_fingerprint = self._effective_fingerprint()
        current_device = self.current_device
        self.project = apply_custom_models(self.project, tuple(models))
        self.custom_model_manager.set_project(self.project)
        self.components.set_project(self.project)
        self.parameter_sweeps.clear()
        self.backend_manager.set_project(
            self.project.root,
            self._effective_fingerprint(),
        )
        structure_changed = previous_fingerprint != self._effective_fingerprint()
        self._prepare_on_ready = structure_changed
        if current_device and (
            not structure_changed
            or any(item.static_injection == current_device for item in models)
        ):
            self.show_device(current_device)
        else:
            self.show_topology()
        self.statusBar().showMessage(
            "自定义模型已保存；计算图需要重新准备"
            if structure_changed
            else "项目派生模型已保存；尚未装配，不影响当前计算图",
            5000,
        )
        if structure_changed and self.julia.state == "Ready":
            QtCore.QTimer.singleShot(0, self.prepare_current_network)

    def _effective_fingerprint(self) -> str:
        if not self.project:
            return ""
        base = str(self.project.manifest.get("fingerprint", ""))
        executable = tuple(
            model for model in self.project.custom_models if model.model_kind == "equation"
        )
        parts = [base]
        if executable:
            parts.append(CustomModelStore.fingerprint(executable)[:12])
        if self.editor_document != EditorDocument():
            parts.append(self.editor_document.fingerprint()[:12])
        return ":".join(parts)

    @QtCore.Slot(str)
    def _locate_model_usage(self, identifier: str) -> None:
        if not self.project:
            return
        injection = next(
            (item for item in self.project.injections if item.name == identifier),
            None,
        )
        if injection:
            self._activate_component_key(f"injection:{injection.name}")
            self.custom_model_manager.hide()
            return
        equation = next(
            (
                item
                for item in self.project.equations
                if item.component_type.rsplit(".", 1)[-1] == identifier
            ),
            None,
        )
        if equation and equation.path.startswith("injections/"):
            self._activate_component_key(equation.path)
            self.custom_model_manager.hide()

    @QtCore.Slot(object)
    def _database_changed(self, database: ResultDatabase) -> None:
        if self.active_task:
            QtWidgets.QMessageBox.warning(
                self, "数据库已切换", "当前任务仍写入启动任务时使用的数据库。"
            )
            self.data_manager.database = self.result_database
            self.data_manager.path_label.setText(str(self.result_database.path))
            return
        self.result_database = database
        QtCore.QSettings().setValue("results/database_path", str(database.path))

    @QtCore.Slot(str, str)
    def _backend_state_changed(self, state: str, message: str) -> None:
        log_line = f"[后端 {state}] {message}"
        if log_line != self._last_backend_log:
            self.run_log.appendPlainText(log_line)
            self._last_backend_log = log_line
        running = state == "Busy"
        has_system = bool(self.project and self.project.system)
        has_project = self.project is not None
        task_active = self.active_task is not None
        self.precompile_action.setEnabled(not running and not task_active)
        can_switch_project = not running and not task_active
        self.new_project_action.setEnabled(can_switch_project)
        self.open_action.setEnabled(can_switch_project)
        self.save_editor_action.setEnabled(has_project and not running and not task_active)
        self.save_as_action.setEnabled(has_project and can_switch_project)
        self.close_project_action.setEnabled(has_project and can_switch_project)
        self.simulation_settings_action.setEnabled(has_system and not running and not task_active)
        self.run_simulation_action.setEnabled(has_system and not running and not task_active)
        self.prepare_network_action.setEnabled(has_system and not running and not task_active)
        self.rebuild_network_action.setEnabled(has_system and not running and not task_active)
        self.stop_simulation_action.setEnabled(
            running and self.julia.current_job == "RUN"
        )
        self.parameters.setEnabled(not running and not task_active)
        self.custom_models_action.setEnabled(
            has_project
        )
        edit_available = has_project and not running and not task_active
        self.edit_mode_action.setEnabled(edit_available)
        self.components.edit_mode.setEnabled(edit_available)
        self.drawing_dock.toggleViewAction().setEnabled(
            has_project and self.edit_mode_action.isChecked()
        )
        if hasattr(self, "custom_model_manager"):
            self.custom_model_manager.set_editing_enabled(
                self.edit_mode_action.isChecked() and not running and not task_active
            )
        if hasattr(self, "edit_toolbar"):
            self._apply_editability()
        if state == "Ready" and self._prepare_on_ready:
            QtCore.QTimer.singleShot(0, self.prepare_current_network)
        if state == "Ready" and self.active_task and self.active_task.get("waiting"):
            self.active_task["waiting"] = False
            QtCore.QTimer.singleShot(0, self._start_next_case)

    @QtCore.Slot(str, str)
    def _simulation_phase(self, phase: str, message: str) -> None:
        labels = {
            "precompile": "预编译",
            "load": "载入系统",
            "cache_hit": "计算图缓存命中",
            "cache_miss": "计算图缓存未命中",
            "cache_saved": "计算图已缓存",
            "initialize": "初始化仿真",
            "run": "运行仿真",
            "export": "导出数据",
            "complete": "完成",
            "cancel": "终止中",
            "cancelled": "已终止",
            "error": "失败",
        }
        self.run_log.appendPlainText(f"[{labels.get(phase, phase)}] {message}")
        self.statusBar().showMessage(message)

    @QtCore.Slot(str)
    def _simulation_log(self, message: str) -> None:
        self.run_log.appendPlainText(message)

    @QtCore.Slot(bool, str)
    def _simulation_finished(self, success: bool, message: str) -> None:
        self.statusBar().showMessage(message, 10000)
        self.run_log.appendPlainText(message)
        if self.active_task and self.active_task.get("run_id") is not None:
            task = self.active_task
            run_id = task["run_id"]
            if success:
                try:
                    result_file = self.julia.info.get("result_file", "")
                    metadata_file = self.julia.info.get("metadata_file", "")
                    metadata: dict[str, Any] = {}
                    if metadata_file and Path(metadata_file).is_file():
                        with Path(metadata_file).open("rb") as stream:
                            metadata = tomllib.load(stream)
                    self.result_database.complete_run(run_id, result_file, metadata)
                except (OSError, ValueError, KeyError) as error:
                    success = False
                    message = f"结果写入数据库失败：{error}"
                    self.result_database.fail_run(run_id, message)
            else:
                status = "cancelled" if task["cancelled"] else "failed"
                self.result_database.fail_run(run_id, message, status)
            if not success:
                task["failures"] += 1
            else:
                task["successes"] += 1
            task["run_id"] = None
            task["next"] += 1
            self.data_manager.refresh()
            if task["cancelled"]:
                self._finish_active_task("cancelled", message)
            elif task["next"] >= len(task["cases"]):
                status = "completed" if not task["failures"] else (
                    "failed" if not task["successes"] else "partial"
                )
                self._finish_active_task(status, message)
            elif self.julia.state == "Ready":
                QtCore.QTimer.singleShot(0, self._start_next_case)
            elif self.julia.state == "Offline":
                self._finish_active_task("partial" if task["successes"] else "failed", message)
            else:
                task["waiting"] = True
            return
        if success:
            QtWidgets.QMessageBox.information(self, "Julia 任务完成", message)
        elif "已终止" not in message:
            QtWidgets.QMessageBox.critical(self, "Julia 任务失败", message)

    @QtCore.Slot(object, object, float)
    def _edit_parameters(
        self,
        nodes: list[ViewerNode],
        path: tuple[str, ...],
        value: float,
    ) -> None:
        runtimes = [node.runtime for node in nodes if isinstance(node.runtime, dict)]
        if not runtimes:
            return
        if path == ("base_power",) and any(
            isinstance(node.payload, EquationComponent)
            and node.payload.role == "custom_dynamic"
            for node in nodes
        ) and value <= 0:
            self.statusBar().showMessage("设备基准功率必须大于零", 4000)
            self.parameters.set_nodes(nodes)
            return
        self.graph.undo_stack().push(
            ParameterEditCommand(runtimes, path, value, self._parameter_changed)
        )

    @staticmethod
    def _identity_path(
        root: Any, target: Any, prefix: tuple[str | int, ...] = ()
    ) -> tuple[str | int, ...] | None:
        if root is target:
            return prefix
        if isinstance(root, dict):
            for key, value in root.items():
                found = MainWindow._identity_path(value, target, (*prefix, key))
                if found is not None:
                    return found
        elif isinstance(root, list):
            for index, value in enumerate(root):
                found = MainWindow._identity_path(value, target, (*prefix, index))
                if found is not None:
                    return found
        return None

    @staticmethod
    def _value_for_mixed_path(root: Any, path: tuple[str | int, ...]) -> Any:
        current = root
        for key in path:
            current = current[key]
        return current

    def _sweep_key(
        self, nodes: list[ViewerNode], path: tuple[str, ...]
    ) -> tuple[tuple[tuple[str | int, ...], ...], tuple[str, ...]] | None:
        if not self.project or not self.project.system:
            return None
        target_paths = []
        for node in nodes:
            if not isinstance(node.runtime, dict):
                continue
            custom = self._custom_model(node)
            if isinstance(node.payload, EditorInstance):
                target_paths.append(("__editor__", node.payload.instance_id))
                continue
            if (
                custom
                and isinstance(node.payload, EquationComponent)
                and node.payload.role == "custom_dynamic"
            ):
                target_paths.append(("__custom__", custom.model_id))
                continue
            target = self._identity_path(self.project.system, node.runtime)
            if target is None:
                return None
            target_paths.append(target)
        return (tuple(sorted(target_paths, key=repr)), tuple(path)) if target_paths else None

    def _parameter_expression(
        self, nodes: list[ViewerNode], path: tuple[str, ...]
    ) -> ParameterExpression | None:
        key = self._sweep_key(nodes, path)
        item = self.parameter_sweeps.get(key) if key else None
        return item.expression if item else None

    @QtCore.Slot(object, object, object)
    def _parameter_expression_changed(
        self,
        nodes: list[ViewerNode],
        path: tuple[str, ...],
        expression: ParameterExpression,
    ) -> None:
        key = self._sweep_key(nodes, path)
        if key is None and not expression.is_sweep:
            self._edit_parameters(nodes, path, expression.values[0])
            return
        if key is None:
            return
        if expression.is_sweep:
            labels = [self._node_identifier(node) for node in nodes]
            label = (
                f"{labels[0]}.{'.'.join(path)}"
                if len(labels) == 1
                else f"{len(labels)} 个联动元件.{'.'.join(path)}"
            )
            self.parameter_sweeps[key] = SweepParameter(
                target_paths=key[0],
                parameter_path=tuple(path),
                label=label,
                expression=expression,
            )
            self.statusBar().showMessage(
                f"已加入参数组：{label}（{len(expression.values)} 点）", 4000
            )
            self.parameters.set_nodes(nodes)
            return
        self.parameter_sweeps.pop(key, None)
        self._edit_parameters(nodes, path, expression.values[0])

    @QtCore.Slot()
    def _cancel_simulation(self) -> None:
        if self.active_task:
            self.active_task["cancelled"] = True
            if self.active_task.get("run_id") is None:
                self._finish_active_task("cancelled", "任务已取消")
                return
        self.julia.terminate()

    def _start_next_case(self) -> None:
        task = self.active_task
        if not task or not self.project or not self.project.system:
            return
        if task["cancelled"]:
            self._finish_active_task("cancelled", "任务已取消")
            return
        case_index = task["next"]
        if case_index >= len(task["cases"]):
            status = "completed" if not task["failures"] else (
                "failed" if not task["successes"] else "partial"
            )
            self._finish_active_task(status)
            return
        system = copy.deepcopy(self.project.system)
        editor_document = copy.deepcopy(self.editor_document)
        custom_models = list(self.project.custom_models)
        changed: dict[str, float] = {}
        case = task["cases"][case_index]
        for sweep_index, value in case.items():
            sweep: SweepParameter = task["sweeps"][sweep_index]
            for target_path in sweep.target_paths:
                if target_path and target_path[0] == "__custom__":
                    custom_models = self._custom_models_with_value(
                        custom_models, str(target_path[1]), sweep.parameter_path, value
                    )
                elif target_path and target_path[0] == "__editor__":
                    instance = next(
                        item
                        for item in editor_document.instances
                        if item.instance_id == str(target_path[1])
                    )
                    set_value(instance.parameters, sweep.parameter_path, value)
                else:
                    runtime = self._value_for_mixed_path(system, target_path)
                    set_value(runtime, sweep.parameter_path, value)
            changed[sweep.label] = value
        recorded_parameters: Any = system
        if custom_models or editor_document != EditorDocument():
            recorded_parameters = {
                "system": system,
                "custom_models": [model.to_dict() for model in custom_models],
                "editor_model": editor_document.to_dict(),
            }
        task["run_id"] = self.result_database.begin_run(
            task["task_id"], case_index + 1, recorded_parameters, changed
        )
        total = len(task["cases"])
        values = ", ".join(f"{name}={value:.12g}" for name, value in changed.items())
        self.run_log.appendPlainText(
            f"[实例 {case_index + 1}/{total}] {values or '基准参数'}"
        )
        self.statusBar().showMessage(f"正在运行第 {case_index + 1}/{total} 个实例")
        try:
            self.julia.run(
                self.project.root,
                system,
                self.simulation_settings,
                tuple(custom_models),
                editor_document,
            )
        except RuntimeError as error:
            self.result_database.fail_run(task["run_id"], str(error))
            task["failures"] += 1
            task["run_id"] = None
            task["next"] += 1
            task["waiting"] = True

    def _finish_active_task(self, status: str, error: str = "") -> None:
        task = self.active_task
        if not task:
            return
        self.result_database.finish_task(task["task_id"], status, error if status != "completed" else "")
        name = task["name"]
        completed = task["successes"]
        total = len(task["cases"])
        self.active_task = None
        self.data_manager.change_button.setEnabled(True)
        self._backend_state_changed(self.julia.state, self.julia.state_message)
        self.data_manager.refresh()
        message = f"仿真任务“{name}”已{ {'completed':'完成', 'partial':'部分完成', 'failed':'失败', 'cancelled':'取消'}.get(status, status) }（{completed}/{total}）"
        self.statusBar().showMessage(message, 10000)
        self.run_log.appendPlainText(f"[任务] {message}")
        if status == "completed":
            QtWidgets.QMessageBox.information(self, "仿真任务完成", message)
        elif status == "partial":
            QtWidgets.QMessageBox.warning(self, "仿真任务部分完成", message)
        elif status == "failed":
            QtWidgets.QMessageBox.critical(self, "仿真任务失败", message)

    def _parameter_changed(self) -> None:
        self._persist_custom_runtime_parameters()
        selected = self._selected_nodes()
        if self.project and any(
            isinstance(node.payload, EditorInstance)
            or (
                isinstance(node.payload, EquationComponent)
                and node.payload.path.startswith("editor-device:")
            )
            for node in selected
        ):
            EditorDocumentStore.save(self.project.root, self.editor_document)
            self.components.set_project(self.project)
        self.parameters.set_nodes(selected)
        if selected:
            self.details.setHtml(self._node_details(selected[0]))
        self.statusBar().showMessage("参数已修改，将在下次仿真初始化时生效", 4000)

    @staticmethod
    def _custom_models_with_value(
        models: list[CustomModel],
        model_id: str,
        path: tuple[str, ...],
        value: float,
    ) -> list[CustomModel]:
        updated = list(models)
        for index, model in enumerate(updated):
            if model.model_id != model_id:
                continue
            if path == ("base_power",):
                updated[index] = model.__class__(
                    **{**model.__dict__, "base_power": value}
                )
            elif len(path) == 1:
                parameters = tuple(
                    parameter.__class__(parameter.name, value, parameter.unit)
                    if parameter.name == path[0]
                    else parameter
                    for parameter in model.parameters
                )
                updated[index] = model.__class__(
                    **{**model.__dict__, "parameters": parameters}
                )
            break
        return updated

    def _persist_custom_runtime_parameters(self) -> None:
        if not self.project:
            return
        models = list(self.project.custom_models)
        changed = False
        for node in self.nodes.values():
            model = self._custom_model(node)
            if not model or not isinstance(node.runtime, dict):
                continue
            if (
                not isinstance(node.payload, EquationComponent)
                or node.payload.role != "custom_dynamic"
                or node.payload.path.startswith("editor-device:")
            ):
                continue
            values = node.runtime
            updated = self._custom_models_with_value(
                models, model.model_id, ("base_power",), float(values["base_power"])
            )
            for parameter in model.parameters:
                updated = self._custom_models_with_value(
                    updated, model.model_id, (parameter.name,), float(values[parameter.name])
                )
            models = updated
            changed = True
        if changed:
            CustomModelStore.save(self.project.root, tuple(models))
            self.project = replace(self.project, custom_models=tuple(models))

    @QtCore.Slot()
    def show_topology(self) -> None:
        if not self.project:
            return
        self._clear_graph()
        self.nodes = TopologyGraphBuilder(self.graph).build(self.project).nodes
        self.current_view_key = "topology"
        self._restore_layout()
        self.current_device = None
        self.home_action.setEnabled(False)
        self.up_action.setEnabled(False)
        self.reset_layout_action.setEnabled(True)
        self.breadcrumb.setText(f"{self.project.root.name}  /  电网拓扑")
        self.details.setHtml(self._project_details(self.project))
        self.parameters.set_nodes([])
        self._finish_graph()

    def show_device(self, device_identifier: str) -> None:
        if not self.project:
            return
        self._clear_graph()
        result = DeviceGraphBuilder(self.graph).build(self.project, device_identifier)
        if not result.nodes:
            self.show_topology()
            return
        self.nodes = result.nodes
        self.current_view_key = f"device:{device_identifier}"
        self._restore_layout()
        self.current_device = device_identifier
        self.home_action.setEnabled(True)
        self.up_action.setEnabled(True)
        self.reset_layout_action.setEnabled(True)
        instance = self.editor_document.instance_by_key.get(device_identifier)
        branch = (
            next(
                (
                    item
                    for item in self.project.branches
                    if device_identifier == f"branch:{item.name}"
                ),
                None,
            )
            if not instance
            else None
        )
        display_name = instance.name if instance else branch.name if branch else device_identifier
        self.breadcrumb.setText(
            f"{self.project.root.name}  /  电网拓扑  /  {display_name}"
        )
        root_key = (
            f"editor-device:{instance.instance_id}"
            if instance
            else f"branches/{branch.name}"
            if branch
            else f"injections/{device_identifier}"
        )
        root = self.nodes.get(root_key)
        if root:
            self.details.setHtml(self._node_details(root))
        self._finish_graph()

    @QtCore.Slot()
    def reset_layout(self) -> None:
        if not self.project:
            return
        if self.current_view_key:
            self.layout_states.pop(self.current_view_key, None)
        if self.edit_mode_action.isChecked() and (
            self.editor_document.geometry or self.editor_document.port_positions
        ):
            geometry = {
                key: value
                for key, value in self.editor_document.geometry.items()
                if key not in self.nodes
            }
            port_positions = {
                key: value
                for key, value in self.editor_document.port_positions.items()
                if key.rsplit("|", 1)[0] not in self.nodes
            }
            self.editor_document = replace(
                self.editor_document,
                geometry=geometry,
                port_positions=port_positions,
            )
            self.project = replace(self.project, editor_document=self.editor_document)
            EditorDocumentStore.save(self.project.root, self.editor_document)
        self.skip_layout_capture = True
        if self.current_device:
            self.show_device(self.current_device)
        else:
            self.show_topology()
        self.statusBar().showMessage("已恢复自动布局", 3000)

    def _clear_graph(self) -> None:
        self.search.clear()
        self.resize_node_key = None
        self.read_only_filter._clear_boundary_hover()
        if not self.skip_layout_capture and self.current_view_key and self.nodes:
            self.layout_states[self.current_view_key] = {
                key: (
                    *node.pos(),
                    *node.size,
                    node.orientation,
                    node.mirror_horizontal,
                    node.mirror_vertical,
                )
                for key, node in self.nodes.items()
            }
        self.skip_layout_capture = False
        for node in self.graph.all_nodes():
            for port in (*node.input_ports(), *node.output_ports()):
                port.set_locked(False, connected_ports=False, push_undo=False)
        self.nodes = {}
        if self._syncing_editor:
            self.graph.delete_nodes(self.graph.all_nodes(), push_undo=False)
        else:
            self.graph.clear_session()

    def _restore_layout(self) -> None:
        if not self.current_view_key:
            return
        states = {
            **self.layout_states.get(self.current_view_key, {}),
            **{
                key: state
                for key, state in self.editor_document.geometry.items()
                if key in self.nodes
            },
        }
        for key, state in states.items():
            node = self.nodes.get(key)
            if node:
                node.set_property("pos", list(state[:2]), push_undo=False)
                node.set_size(*state[2:4])
                node.set_transform_state(*state[4:])
        for storage_key, position in self.editor_document.port_positions.items():
            if "|" not in storage_key:
                continue
            node_key, port_name = storage_key.rsplit("|", 1)
            node = self.nodes.get(node_key)
            if not node:
                continue
            port = node.get_input(port_name) or node.get_output(port_name)
            if port and getattr(port.view, "bus_edge_port", False):
                port.view.set_normalized_position(position)
        for node in self.nodes.values():
            if node.is_bus_node:
                node._align_port_labels()

    def _finish_graph(self) -> None:
        for node in self.nodes.values():
            node.resize_finished = self._resize_finished
            node.port_move_finished = self._bus_port_moved
        if not self._syncing_editor:
            self.graph.undo_stack().clear()
        self.graph.fit_to_selection()
        self._set_signal_labels_visible(self.signal_labels_action.isChecked())
        self._apply_editability()
        self._update_node_actions()

    @QtCore.Slot(bool)
    def _set_signal_labels_visible(self, visible: bool) -> None:
        for node in self.nodes.values():
            for port in (*node.input_ports(), *node.output_ports()):
                if not getattr(port.view, "signal_label", ""):
                    continue
                port.view.display_name = visible
                text = (
                    node.view.get_input_text_item(port.view)
                    if port.type_() == "in"
                    else node.view.get_output_text_item(port.view)
                )
                text.setVisible(visible and not node.view._proxy_mode)
            node.view.update()

    @QtCore.Slot(object)
    def _show_node_details(self, node: ViewerNode) -> None:
        self.details.setHtml(self._node_details(node))
        self.parameters.set_nodes([node])
        self.components.select_keys([node.viewer_key])

    @QtCore.Slot(list, list)
    def _selection_changed(self, selected: list[Any], deselected: list[Any]) -> None:
        if self.resize_node_key and not any(
            node.viewer_key == self.resize_node_key for node in self._selected_nodes()
        ):
            self.resize_node_key = None
        self.parameters.set_nodes(self._selected_nodes())
        self.components.select_keys(
            [node.viewer_key for node in self._selected_nodes()]
        )
        self._update_node_actions()

    @QtCore.Slot(object)
    def _component_tree_selected(self, keys: list[str]) -> None:
        visible = [self.nodes[key] for key in keys if key in self.nodes]
        if not visible:
            return
        selected = set(visible)
        for node in self.graph.all_nodes():
            node.view.setSelected(node in selected)
        self.parameters.set_nodes(visible)
        self.details.setHtml(self._node_details(visible[0]))
        self._update_node_actions()

    @QtCore.Slot(str)
    def _activate_component_key(self, key: str) -> None:
        if not self.project:
            return
        if key.startswith(("bus:", "branch:", "injection:", "editor:")):
            if self.current_view_key != "topology":
                self.show_topology()
        elif key.startswith("injections/"):
            parts = key.split("/")
            if len(parts) < 2:
                return
            view_key = f"device:{parts[1]}"
            if self.current_view_key != view_key:
                self.show_device(parts[1])
        elif key.startswith("branches/"):
            branch_name = key.split("/", 1)[1].split("/", 1)[0]
            identifier = f"branch:{branch_name}"
            view_key = f"device:{identifier}"
            if self.current_view_key != view_key:
                self.show_device(identifier)
        elif key.startswith("editor-device:"):
            instance_key = "editor:" + key.split(":", 1)[1].split("/", 1)[0]
            view_key = f"device:{instance_key}"
            if self.current_view_key != view_key:
                self.show_device(instance_key)
        node = self.nodes.get(key)
        if not node:
            return
        for other in self.graph.selected_nodes():
            other.view.setSelected(False)
        node.view.setSelected(True)
        self.graph.center_on([node])
        self._show_node_details(node)
        self.component_dock.show()
        self.component_dock.raise_()
        self.components.highlight(key)
        self.activateWindow()

    @QtCore.Slot(object)
    def _node_double_clicked(self, node: ViewerNode) -> None:
        for other in self.graph.selected_nodes():
            if other is not node:
                other.view.setSelected(False)
        node.view.setSelected(True)
        self.resize_node_key = node.viewer_key
        self._show_node_details(node)
        self._update_node_actions()

    @QtCore.Slot()
    def _deactivate_resize_mode(self) -> None:
        self.resize_node_key = None
        self._update_node_actions()

    @QtCore.Slot(str, object)
    def _show_node_menu(self, node_id: str, global_pos: Any) -> None:
        node = self.graph.get_node_by_id(node_id)
        if not isinstance(node, ViewerNode):
            return
        if not node.selected():
            for other in self.graph.selected_nodes():
                other.view.setSelected(False)
            node.view.setSelected(True)
        self._show_node_details(node)
        self._update_node_actions()
        selected = self._selected_nodes()
        count = len(selected)
        self.rotate_clockwise_action.setText(
            f"顺时针旋转所选 {count} 个元件 90°"
            if count > 1
            else "顺时针旋转 90°"
        )
        self.rotate_counterclockwise_action.setText(
            f"逆时针旋转所选 {count} 个元件 90°"
            if count > 1
            else "逆时针旋转 90°"
        )
        self.mirror_horizontal_action.setText(
            f"左右镜像所选 {count} 个元件" if count > 1 else "左右镜像"
        )
        self.mirror_vertical_action.setText(
            f"上下镜像所选 {count} 个元件" if count > 1 else "上下镜像"
        )

        menu = QtWidgets.QMenu(self)
        open_action = self.open_node_action
        model_action = self.show_model_action
        menu.addAction(open_action)
        menu.addAction(model_action)
        replace_action = menu.addAction("替换内部模型…")
        category = self._internal_category(node)
        replace_action.setEnabled(
            self.edit_mode_action.isChecked() and bool(self.current_device) and bool(category)
        )
        replace_action.triggered.connect(
            lambda: self.custom_model_manager.begin_instantiation(
                "internal", node.viewer_key, category
            )
        )
        edit_custom_action = menu.addAction("编辑自定义模型…")
        edit_custom_action.setEnabled(
            self.edit_mode_action.isChecked() and bool(self._custom_model(node))
        )
        edit_custom_action.triggered.connect(lambda: self._edit_custom_model(node))
        add_custom_action = menu.addAction("添加自定义动态模型…")
        add_custom_action.setEnabled(
            self.edit_mode_action.isChecked()
            and isinstance(node.payload, Injection)
            and not node.payload.is_dynamic
        )
        add_custom_action.triggered.connect(lambda: self._add_custom_model(node))
        menu.setDefaultAction(open_action if open_action.isEnabled() else model_action)
        menu.addSeparator()
        menu.addAction(self.rotate_clockwise_action)
        menu.addAction(self.rotate_counterclockwise_action)
        menu.addAction(self.mirror_horizontal_action)
        menu.addAction(self.mirror_vertical_action)
        menu.addAction(self.reset_orientation_action)
        menu.addAction(self.reset_size_action)
        menu.addSeparator()
        menu.addAction(self.focus_nodes_action)
        copy_action = menu.addAction("复制元件名称/路径")
        copy_action.triggered.connect(lambda: self._copy_node_identifier(node))
        if self.edit_mode_action.isChecked() and not self.current_device:
            menu.addSeparator()
            menu.addAction(self.delete_component_action)
        menu.exec(global_pos)
        self._update_node_actions()

    def _internal_category(self, node: ViewerNode) -> str:
        if not isinstance(node.payload, EquationComponent):
            return ""
        model_type = node.payload.component_type.rsplit(".", 1)[-1]
        if model_type in self.custom_model_manager.ACTIVE_CONTROLS:
            return "有功控制"
        if model_type in self.custom_model_manager.REACTIVE_CONTROLS:
            return "无功控制"
        return {
            "machine": "同步机",
            "shaft": "轴系",
            "avr": "励磁系统",
            "governor": "调速器",
            "pss": "电力系统稳定器",
            "converter": "变流器",
            "dc_source": "直流侧",
            "filter": "滤波器",
            "frequency_estimator": "频率估计器",
            "inner_control": "内环控制器",
            "output_current_limiter": "输出电流限幅器",
        }.get(node.payload.role, "")

    def _replace_internal_slot(self, descriptor: dict[str, Any]) -> None:
        if not self.project:
            return
        path = str(descriptor.get("component_path", ""))
        node = self.nodes.get(path)
        category = self._internal_category(node) if node else ""
        if not node or not category or category != descriptor.get("base_category"):
            self.statusBar().showMessage("模型接口与当前槽位不兼容", 4000)
            return
        model_ref = str(descriptor["model_ref"])
        model_name = model_ref
        if descriptor.get("source") == "instantiate_custom":
            model = next(
                (item for item in self.project.custom_models if item.model_id == model_ref),
                None,
            )
            if not model or not model.is_derived or model.base_category != category:
                self.statusBar().showMessage("派生模型没有继承当前槽位接口", 4000)
                return
            model_name = model.name
        override = SlotOverride(path, model_ref, model_name, category)
        before = self.editor_document
        after = before.with_slot_override(override)
        self.graph.undo_stack().push(
            EditorDocumentCommand(before, after, f"替换 {path.rsplit('/', 1)[-1]}", self._apply_editor_document)
        )

    def _selected_nodes(self) -> list[ViewerNode]:
        return [
            node for node in self.graph.selected_nodes() if isinstance(node, ViewerNode)
        ]

    def _is_openable(self, node: ViewerNode) -> bool:
        if not self.project:
            return False
        if isinstance(node.payload, Injection):
            prefix = f"injections/{node.payload.name}"
            return node.payload.is_dynamic and any(
                item.path == prefix for item in self.project.equations
            )
        if isinstance(node.payload, EditorInstance):
            return bool(editor_device_components(self.project, node.payload))
        if isinstance(node.payload, Branch):
            prefix = f"branches/{node.payload.name}"
            return any(item.path == prefix for item in self.project.equations)
        return False

    def _open_node(self, node: ViewerNode) -> None:
        if self._is_openable(node):
            self.show_device(
                node.viewer_key
                if isinstance(node.payload, (EditorInstance, Branch))
                else node.payload.name
            )

    @QtCore.Slot()
    def _open_selected_node(self) -> None:
        nodes = self._selected_nodes()
        if len(nodes) == 1:
            self._open_node(nodes[0])

    @staticmethod
    def _node_model_type(node: ViewerNode) -> str:
        payload = node.payload
        if isinstance(payload, EquationComponent):
            return payload.component_type
        if isinstance(payload, Injection):
            return payload.dynamic_type or payload.injection_type
        if isinstance(payload, EditorInstance):
            return payload.model_ref
        if isinstance(payload, Branch):
            return payload.branch_type
        return ""

    def _models_for_node(self, node: ViewerNode) -> tuple[ModelImplementation, ...]:
        return self.model_library.lookup(self._node_model_type(node))

    def _custom_model(self, node: ViewerNode) -> CustomModel | None:
        if not self.project:
            return None
        payload = node.payload
        if isinstance(payload, EditorInstance):
            return next(
                (
                    item
                    for item in self.project.custom_models
                    if item.model_id == payload.model_ref
                ),
                None,
            )
        if isinstance(payload, EquationComponent) and payload.path.startswith("editor-device:"):
            instance_id = payload.path.split(":", 1)[1].split("/", 1)[0]
            instance = self.editor_document.instance_by_key.get(f"editor:{instance_id}")
            return next(
                (
                    item
                    for item in self.project.custom_models
                    if instance and item.model_id == instance.model_ref
                ),
                None,
            )
        injection_name = (
            payload.name
            if isinstance(payload, Injection)
            else payload.path.split("/")[1]
            if isinstance(payload, EquationComponent) and payload.path.startswith("injections/")
            else ""
        )
        return next(
            (item for item in self.project.custom_models if item.static_injection == injection_name),
            None,
        )

    def _edit_custom_model(self, node: ViewerNode) -> None:
        model = self._custom_model(node)
        if not model:
            return
        self.show_custom_model_manager()
        for row in range(self.custom_model_manager.list.count()):
            item = self.custom_model_manager.list.item(row)
            if item.data(QtCore.Qt.ItemDataRole.UserRole) == model.model_id:
                self.custom_model_manager.list.setCurrentRow(row)
                self.custom_model_manager.edit_selected()
                break

    def _add_custom_model(self, node: ViewerNode) -> None:
        if not self.project or not isinstance(node.payload, Injection):
            return
        self.custom_model_manager.set_project(self.project)
        self.custom_model_manager.create_for(node.payload.name)

    def _show_model(self, node: ViewerNode) -> None:
        custom = self._custom_model(node)
        if custom:
            dialog = ModelDialog(
                self._node_identifier(node), self._custom_model_html(custom), self
            )
            dialog.exec()
            return
        models = self._models_for_node(node)
        if not models:
            return
        dialog = ModelDialog(
            self._node_identifier(node), self._model_html(node, models), self
        )
        dialog.exec()

    @QtCore.Slot()
    def _show_selected_model(self) -> None:
        nodes = self._selected_nodes()
        if len(nodes) == 1:
            self._show_model(nodes[0])

    @staticmethod
    def _custom_model_html(model: CustomModel) -> str:
        state_rows = "".join(
            f"<tr><td>{html.escape(state.name)}</td><td>{state.mass:g} · d({html.escape(state.name)})/dt = {html.escape(state.rhs)}</td><td>{state.initial:g}</td></tr>"
            for state in model.states
        )
        parameters = "".join(
            f"<tr><td>{html.escape(item.name)}</td><td>{item.value:g}</td><td>{html.escape(item.unit)}</td></tr>"
            for item in model.parameters
        )
        return (
            "<style>body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;}table{border-collapse:collapse;width:100%;}td,th{padding:6px;border-bottom:1px solid #7775;text-align:left;}code{font-size:14px}</style>"
            f"<h2>{html.escape(model.name)}</h2><p>{html.escape(model.description)}</p>"
            f"<p><b>绑定：</b>{html.escape(model.static_injection)}　<b>初始化：</b>{html.escape(model.initialization)}</p>"
            f"<h3>微分方程</h3><table><tr><th>状态</th><th>M·ẋ = f(x,u)</th><th>初值/初猜</th></tr>{state_rows}</table>"
            f"<h3>参数</h3><table>{parameters or '<tr><td>无</td></tr>'}</table>"
            f"<h3>网络电流输出</h3><p><code>i_r = {html.escape(model.outputs['i_r'])}</code><br><code>i_i = {html.escape(model.outputs['i_i'])}</code></p>"
        )

    def _model_html(
        self,
        node: ViewerNode,
        models: tuple[ModelImplementation, ...] | None = None,
    ) -> str:
        models = models if models is not None else self._models_for_node(node)
        model_type = self._node_model_type(node)
        states = node.payload.states if isinstance(node.payload, EquationComponent) else ()
        sections = [
            f"<h2>{html.escape(self._node_identifier(node))}</h2>",
            f"<p><b>导出类型：</b><code>{html.escape(model_type)}</code></p>",
            "<p><b>统一形式：</b> M(x) · ẋ = f(x, u)。以下内容由当前库的 "
            "设备方程入口实现生成；质量矩阵系数由对应 "
            "<code>mass_matrix_*_entries!</code> 实现定义。</p>",
        ]
        if states:
            sections.append(
                "<p><b>该组件导出的状态顺序：</b>"
                + ", ".join(
                    f"x<sub>{i}</sub> = {html.escape(state)}"
                    for i, state in enumerate(states, 1)
                )
                + "</p>"
            )
        for implementation in models:
            sections.append(
                f"<hr><h3>{html.escape(implementation.model_type)}"
                f" <small>（{html.escape(implementation.category)}）</small></h3>"
                f"<p><b>方程入口：</b><code>{html.escape(implementation.entrypoint)}</code><br>"
                f"<b>源码：</b><code>{html.escape(implementation.source)}</code></p>"
            )
            if implementation.derivatives:
                equations = [
                    self._display_equation(
                        item, states if implementation.model_type == model_type else ()
                    )
                    for item in implementation.derivatives
                ]
                sections.append(
                    "<h4>微分方程右端</h4><pre>"
                    + html.escape("\n".join(equations))
                    + "</pre>"
                )
            else:
                sections.append("<p><i>该实现没有动态状态方程。</i></p>")
            if implementation.mass_matrix:
                sections.append(
                    "<h4>质量矩阵非默认项</h4><pre>"
                    + html.escape("\n".join(implementation.mass_matrix))
                    + "</pre>"
                )
            if implementation.algebraic:
                sections.append(
                    "<h4>代数变量与端口输出</h4><pre>"
                    + html.escape("\n".join(implementation.algebraic))
                    + "</pre>"
                )
        return "".join(sections)

    @staticmethod
    def _display_equation(equation: str, states: tuple[str, ...]) -> str:
        match = re.match(
            r"[A-Za-z_]+_ode\[(?:local_ix\[)?(\d+)\]?\]\s*=\s*(.*)",
            equation,
        )
        if not match:
            return equation
        index = int(match.group(1))
        state = states[index - 1] if 0 < index <= len(states) else f"x{index}"
        return f"d({state})/dt = {match.group(2)}"

    def _rotate_selected(self, degrees: int) -> None:
        nodes = self._selected_nodes()
        if not nodes:
            return
        after = [
            (
                (node.orientation + degrees) % 360,
                node.mirror_horizontal,
                node.mirror_vertical,
            )
            for node in nodes
        ]
        direction = "顺时针" if degrees > 0 else "逆时针"
        self.graph.undo_stack().push(
            TransformNodesCommand(
                nodes, after, f"{direction}旋转元件", self._transform_changed
            )
        )

    def _mirror_selected(self, horizontal: bool) -> None:
        nodes = self._selected_nodes()
        if not nodes:
            return
        after = [
            (
                node.orientation,
                not node.mirror_horizontal if horizontal else node.mirror_horizontal,
                node.mirror_vertical if horizontal else not node.mirror_vertical,
            )
            for node in nodes
        ]
        label = "左右" if horizontal else "上下"
        self.graph.undo_stack().push(
            TransformNodesCommand(
                nodes, after, f"{label}镜像元件", self._transform_changed
            )
        )

    @QtCore.Slot()
    def _reset_selected_orientation(self) -> None:
        nodes = self._selected_nodes()
        if not nodes or not any(
            node.orientation or node.mirror_horizontal or node.mirror_vertical
            for node in nodes
        ):
            return
        self.graph.undo_stack().push(
            TransformNodesCommand(
                nodes,
                [(0, False, False)] * len(nodes),
                "重置元件方向",
                self._transform_changed,
            )
        )

    def _transform_changed(self) -> None:
        self._store_selected_geometry()
        self._update_node_actions()
        selected = self._selected_nodes()
        if selected:
            self._show_node_details(selected[0])

    def _resize_finished(
        self,
        node: ViewerNode,
        before: tuple[float, float, float, float],
        after: tuple[float, float, float, float],
    ) -> None:
        self.graph.undo_stack().push(
            ResizeNodesCommand(
                [node], [before], [after], "调整元件大小", self._geometry_changed
            )
        )

    def _bus_port_moved(
        self,
        node: ViewerNode,
        port_name: str,
        before: tuple[float, float],
        after: tuple[float, float],
    ) -> None:
        if not self.project or not self.edit_mode_action.isChecked():
            return
        previous = self.editor_document.with_port_position(
            node.viewer_key, port_name, before
        )
        updated = self.editor_document.with_port_position(
            node.viewer_key, port_name, after
        )
        self.graph.undo_stack().push(
            EditorDocumentCommand(
                previous,
                updated,
                "移动 Bus 端口",
                lambda document: self._apply_bus_port_layout(
                    document, node.viewer_key, port_name
                ),
            )
        )

    def _apply_bus_port_layout(
        self, document: EditorDocument, node_key: str, port_name: str
    ) -> None:
        """Persist and redraw one Bus port without rebuilding the graph."""
        if not self.project:
            return
        self.editor_document = document
        self.project = replace(self.project, editor_document=document)
        EditorDocumentStore.save(self.project.root, document)
        node = self.nodes.get(node_key)
        if not node:
            return
        port = node.get_input(port_name) or node.get_output(port_name)
        position = document.port_positions.get(f"{node_key}|{port_name}")
        if port and position and getattr(port.view, "bus_edge_port", False):
            port.view.set_normalized_position(position)
            node._align_port_labels()

    @QtCore.Slot()
    def _reset_selected_size(self) -> None:
        nodes = [
            node
            for node in self._selected_nodes()
            if node.default_size and node.size != node.default_size
        ]
        if not nodes:
            return
        before = [(*node.pos(), *node.size) for node in nodes]
        after = []
        for node in nodes:
            x, y = node.pos()
            width, height = node.size
            default_width, default_height = node.default_size
            after.append(
                (
                    x + (width - default_width) / 2.0,
                    y + (height - default_height) / 2.0,
                    default_width,
                    default_height,
                )
            )
        self.graph.undo_stack().push(
            ResizeNodesCommand(
                nodes, before, after, "重置元件大小", self._geometry_changed
            )
        )

    def _geometry_changed(self) -> None:
        self._store_selected_geometry()
        self._update_node_actions()
        selected = self._selected_nodes()
        if selected:
            self._show_node_details(selected[0])

    @QtCore.Slot()
    def _toggle_resize_mode(self) -> None:
        nodes = self._selected_nodes()
        if len(nodes) != 1:
            return
        self.resize_node_key = (
            None if self.resize_node_key == nodes[0].viewer_key else nodes[0].viewer_key
        )
        self._update_node_actions()

    def _nudge_selected(self, dx: float, dy: float) -> None:
        nodes = self._selected_nodes()
        if not nodes:
            return
        before = [(*node.pos(), *node.size) for node in nodes]
        after = [
            (x + dx, y + dy, width, height)
            for x, y, width, height in before
        ]
        self.graph.undo_stack().push(
            ResizeNodesCommand(nodes, before, after, "移动元件", self._geometry_changed)
        )

    @QtCore.Slot()
    def _select_all_nodes(self) -> None:
        for node in self.nodes.values():
            node.view.setSelected(True)
        self._selection_changed([], [])

    @QtCore.Slot()
    def _fit_all_nodes(self) -> None:
        if self.nodes:
            self.graph.viewer().zoom_to_nodes([node.view for node in self.nodes.values()])

    def _zoom_canvas(self, amount: float) -> None:
        self.graph.viewer()._set_viewer_zoom(
            amount, pos=self.graph.viewer().viewport().rect().center()
        )

    @QtCore.Slot()
    def _reset_canvas_zoom(self) -> None:
        viewer = self.graph.viewer()
        center = None
        if self.nodes:
            center = viewer._combined_rect(
                [node.view for node in self.nodes.values()]
            ).center()
        viewer.reset_zoom(center)

    def _store_selected_geometry(self) -> None:
        if not self.project or self._syncing_editor or not self.edit_mode_action.isChecked():
            return
        document = self.editor_document
        geometry = dict(document.geometry)
        for node in self._selected_nodes():
            geometry[node.viewer_key] = (
                *node.pos(),
                *node.size,
                node.orientation,
                node.mirror_horizontal,
                node.mirror_vertical,
            )
            instance = document.instance_by_key.get(node.viewer_key)
            if instance:
                document = document.replace_instance(
                    replace(instance, position=tuple(float(item) for item in node.pos()))
                )
        self._apply_editor_document(replace(document, geometry=geometry), rebuild=False)

    @QtCore.Slot()
    def _focus_selected_nodes(self) -> None:
        nodes = self._selected_nodes()
        if nodes:
            self._focus_nodes(nodes)

    @QtCore.Slot()
    def _focus_search(self) -> None:
        self.search.setFocus(QtCore.Qt.FocusReason.ShortcutFocusReason)
        self.search.selectAll()

    @QtCore.Slot()
    def _show_shortcut_help(self) -> None:
        sections: dict[str, list[tuple[str, str]]] = {}
        for category, label, source in self._shortcut_help:
            shortcuts = source.shortcuts() if isinstance(source, QtGui.QAction) else source
            rendered = " / ".join(
                sequence.toString(QtGui.QKeySequence.SequenceFormat.NativeText)
                for sequence in shortcuts
                if not sequence.isEmpty()
            )
            sections.setdefault(category, []).append((label, rendered))
        body = "".join(
            f"<h3>{html.escape(category)}</h3><table>"
            + "".join(
                f"<tr><td>{html.escape(label)}</td><td><code>{html.escape(keys)}</code></td></tr>"
                for label, keys in rows
            )
            + "</table>"
            for category, rows in sections.items()
        )
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("快捷键")
        dialog.resize(620, 680)
        layout = QtWidgets.QVBoxLayout(dialog)
        browser = QtWidgets.QTextBrowser()
        browser.setHtml(
            "<style>table{border-collapse:collapse;width:100%;}"
            "td{padding:6px 10px;border-bottom:1px solid #7775;}"
            "td:last-child{text-align:right;}code{font-size:14px;}</style>"
            "<h2>编辑器快捷键</h2><p>单字母、方向键和删除键仅在画布获得焦点时生效。</p>"
            + body
        )
        layout.addWidget(browser)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Close
        )
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def _focus_nodes(self, nodes: list[ViewerNode]) -> None:
        viewer = self.graph.viewer()
        rect = viewer._combined_rect([node.view for node in nodes])
        margin_x = max(180.0, rect.width() * 0.8)
        margin_y = max(140.0, rect.height() * 1.5)
        rect.adjust(-margin_x, -margin_y, margin_x, margin_y)
        viewer._scene_range = rect
        viewer._update_scene()

    def _copy_node_identifier(self, node: ViewerNode) -> None:
        value = self._node_identifier(node)
        QtWidgets.QApplication.clipboard().setText(value)
        self.statusBar().showMessage(f"已复制：{value}", 3000)

    @staticmethod
    def _node_identifier(node: ViewerNode) -> str:
        payload = node.payload
        return payload.path if isinstance(payload, EquationComponent) else getattr(
            payload, "name", node.viewer_key
        )

    def _update_node_actions(self) -> None:
        if not hasattr(self, "rotate_clockwise_action"):
            return
        selected = self._selected_nodes()
        enabled = bool(selected)
        self.rotate_clockwise_action.setEnabled(enabled)
        self.rotate_counterclockwise_action.setEnabled(enabled)
        self.mirror_horizontal_action.setEnabled(enabled)
        self.mirror_vertical_action.setEnabled(enabled)
        self.reset_orientation_action.setEnabled(
            any(
                node.orientation or node.mirror_horizontal or node.mirror_vertical
                for node in selected
            )
        )
        self.reset_size_action.setEnabled(
            any(node.default_size and node.size != node.default_size for node in selected)
        )
        self.focus_nodes_action.setEnabled(enabled)
        one = selected[0] if len(selected) == 1 else None
        self.open_node_action.setEnabled(bool(one and self._is_openable(one)))
        self.show_model_action.setEnabled(
            bool(one and (self._models_for_node(one) or self._custom_model(one)))
        )
        self.resize_mode_action.setEnabled(one is not None)
        self.select_all_action.setEnabled(bool(self.nodes))
        for action in self.nudge_actions:
            action.setEnabled(enabled)
        for action in (
            self.zoom_in_action,
            self.zoom_out_action,
            self.reset_zoom_action,
        ):
            action.setEnabled(bool(self.nodes))
        self.rotate_clockwise_action.setText("顺时针旋转 90°")
        self.rotate_counterclockwise_action.setText("逆时针旋转 90°")
        self.mirror_horizontal_action.setText("左右镜像")
        self.mirror_vertical_action.setText("上下镜像")
        resize_node = (
            selected[0]
            if len(selected) == 1
            and selected[0].viewer_key == self.resize_node_key
            else None
        )
        for node in self.nodes.values():
            node.view.set_resize_handles_visible(node is resize_node)

    @QtCore.Slot()
    def search_nodes(self) -> None:
        query = self.search.text().strip().casefold()
        self._restore_node_colors()
        if not query:
            return
        matches = [node for node in self.nodes.values() if query in node.search_text]
        for node in matches:
            node.set_property("color", (224, 170, 45, 255), push_undo=False)
            node.view.setSelected(True)
        if matches:
            self.graph.center_on(matches)
            self.details.setHtml(self._node_details(matches[0]))
            self.statusBar().showMessage(f"找到 {len(matches)} 个匹配节点", 3000)
        else:
            self.statusBar().showMessage("没有匹配节点", 3000)

    @QtCore.Slot(str)
    def _restore_search_when_empty(self, text: str) -> None:
        if not text:
            self._restore_node_colors()

    def _restore_node_colors(self) -> None:
        for node in self.nodes.values():
            node.set_property("color", (*node.base_color, 255), push_undo=False)
            node.view.setSelected(False)

    @staticmethod
    def _welcome_details() -> str:
        return """
        <h2>项目详情</h2>
        <p>打开导出的项目后，这里会显示项目摘要和所选节点的结构、状态、方程与运行参数。</p>
        """

    @staticmethod
    def _project_details(project: ExportedProject) -> str:
        manifest = project.manifest
        document = project.editor_document or EditorDocument()
        rows = {
            "项目目录": str(project.root),
            "Schema": manifest.get("schema_version"),
            "模型": manifest.get("model_type"),
            "频率参考": manifest.get("frequency_reference_type"),
            "指纹": manifest.get("fingerprint"),
            "母线": len(project.buses),
            "支路": len(project.branches),
            "注入设备": len(project.injections),
            "动态设备": sum(item.is_dynamic for item in project.injections),
            "变量数": project.graph.get("variable_count"),
            "运行参数": "已载入 system.json" if project.system else "无运行参数快照",
            "编辑新增器件": len(document.instances),
            "编辑连接": len(document.connections),
            "内部模型替换": len(document.slot_overrides),
        }
        environment = manifest.get("environment", {})
        return _page("项目摘要", rows, "环境", environment)

    def _node_details(self, node: ViewerNode) -> str:
        payload = node.payload
        runtime = node.runtime
        if isinstance(payload, Bus):
            title = f"母线：{payload.name}"
            structural = payload.raw
        elif isinstance(payload, Branch):
            title = f"支路：{payload.name}"
            structural = payload.raw
        elif isinstance(payload, Injection):
            title = f"注入设备：{payload.name}"
            structural = payload.raw
        elif isinstance(payload, EquationComponent):
            title = payload.path
            structural = {
                "role": payload.role,
                "type": payload.component_type,
                "states": payload.states,
                "entrypoints": payload.entrypoints,
                "source": payload.source,
            }
            graph_injector = getattr(node, "graph_injector", None)
            if graph_injector:
                structural = {**structural, **graph_injector}
            elif self.project and payload.path.startswith("injections/"):
                root_name = payload.path.split("/")[1]
                graph_injector = self.project.graph_injector_by_name.get(root_name, {})
                global_indices = {
                    item.get("state"): item.get("index")
                    for item in graph_injector.get("global_state_indices", [])
                }
                structural["global_state_indices"] = {
                    state: global_indices[state]
                    for state in payload.states
                    if state in global_indices
                }
        else:
            title = node.name()
            structural = asdict(payload) if is_dataclass(payload) else payload

        runtime_title = "运行参数" if runtime else "运行参数"
        runtime_data: Any = _clean_runtime(runtime) if runtime else "无运行参数快照"
        return _page(title, structural, runtime_title, runtime_data)


def _clean_runtime(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _clean_runtime(item)
            for key, item in value.items()
            if key not in SKIPPED_RUNTIME_KEYS
        }
    if isinstance(value, list):
        return [_clean_runtime(item) for item in value]
    return value


def _page(title: str, first: Any, second_title: str, second: Any) -> str:
    return (
        "<style>body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;}"
        "table{border-collapse:collapse;width:100%;}td{padding:5px 7px;vertical-align:top;"
        "border-bottom:1px solid rgba(119,119,119,0.27);}td:first-child{font-weight:600;width:38%;}"
        "code{word-break:break-all;}</style>"
        f"<h2>{html.escape(title)}</h2>{_html_value(first)}"
        f"<h3>{html.escape(second_title)}</h3>{_html_value(second)}"
    )


def _html_value(value: Any) -> str:
    if isinstance(value, Mapping):
        rows = "".join(
            f"<tr><td>{html.escape(str(key))}</td><td>{_html_value(item)}</td></tr>"
            for key, item in value.items()
        )
        return f"<table>{rows}</table>"
    if isinstance(value, (list, tuple)):
        if all(not isinstance(item, (dict, list, tuple)) for item in value):
            return html.escape(", ".join(str(item) for item in value)) or "—"
        return "<br>".join(_html_value(item) for item in value)
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (str, int, float)):
        return html.escape(str(value))
    return html.escape(json.dumps(value, ensure_ascii=False, default=str))
