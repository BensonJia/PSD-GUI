from __future__ import annotations

from typing import Any

from Qt import QtCore, QtGui, QtWidgets

from .graph_builders import (
    editor_device_components,
    editor_icon_path,
    topology_icon_path,
)
from .models import EquationComponent, ExportedProject


KEY_ROLE = QtCore.Qt.ItemDataRole.UserRole
KIND_ROLE = QtCore.Qt.ItemDataRole.UserRole + 1
SEARCH_ROLE = QtCore.Qt.ItemDataRole.UserRole + 2
AVAILABLE_ROLE = QtCore.Qt.ItemDataRole.UserRole + 3
ADD_KIND_ROLE = QtCore.Qt.ItemDataRole.UserRole + 4


class ComponentTreePanel(QtWidgets.QWidget):
    """Project component hierarchy synchronized with the active graph view."""

    activate_requested = QtCore.Signal(str)
    selection_requested = QtCore.Signal(object)
    add_requested = QtCore.Signal(str)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.project: ExportedProject | None = None
        self.items: dict[str, QtWidgets.QTreeWidgetItem] = {}

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        filters = QtWidgets.QHBoxLayout()
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("搜索名称、类型、编号、状态或路径")
        self.search.setClearButtonEnabled(True)
        self.filter = QtWidgets.QComboBox()
        self.filter.addItems(("全部组件", "拓扑组件", "动态组件", "不可用组件", "自定义组件"))
        filters.addWidget(self.search, 1)
        filters.addWidget(self.filter)
        layout.addLayout(filters)

        options = QtWidgets.QHBoxLayout()
        self.follow_canvas = QtWidgets.QCheckBox("跟随画布")
        self.follow_canvas.setChecked(True)
        self.edit_mode = QtWidgets.QCheckBox("编辑模式")
        self.edit_mode.setEnabled(False)
        self.edit_mode.setToolTip("启用模型结构编辑；布局拖动不受此开关影响")
        options.addWidget(self.follow_canvas)
        options.addWidget(self.edit_mode)
        options.addStretch()
        layout.addLayout(options)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(("组件", "类型 / 连接"))
        self.tree.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.tree.setHorizontalScrollMode(
            QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.tree, 1)

        self.search.textChanged.connect(self._apply_filter)
        self.filter.currentIndexChanged.connect(self._apply_filter)
        self.tree.itemSelectionChanged.connect(self._selection_changed)
        self.tree.itemDoubleClicked.connect(self._activated)
        self.tree.customContextMenuRequested.connect(self._context_menu)

    def set_project(self, project: ExportedProject | None) -> None:
        self.project = project
        self.items.clear()
        self.tree.clear()
        if not project:
            return
        root = self._item(None, project.root.name, "PowerSimulationsDynamics 项目", "group")
        root.setExpanded(True)
        document = project.editor_document
        additions = tuple(document.instances) if document else ()
        deleted = set(document.deleted_keys) if document else set()
        bus_labels = {f"bus:{item.number}": item.name for item in project.buses}
        bus_labels.update(
            {
                item.key: item.name
                for item in additions
                if item.kind == "bus"
            }
        )
        buses = self._item(root, f"母线 ({len(project.buses) + sum(item.kind == 'bus' for item in additions)})", "", "group", add_kind="bus")
        branches = self._item(root, f"支路 ({len(project.branches) + sum(item.kind in {'line', 'transformer'} for item in additions)})", "", "group", add_kind="branch")
        injections = self._item(root, f"注入设备 ({len(project.injections) + sum(item.kind not in {'bus', 'line', 'transformer'} for item in additions)})", "", "group", add_kind="injection")

        for bus in sorted(project.buses, key=lambda value: value.number):
            if f"bus:{bus.number}" in deleted:
                continue
            item = self._item(
                buses,
                f"{bus.name} [{bus.bus_type}]",
                f"#{bus.number} · {bus.raw.get('base_voltage', '')} kV",
                "bus",
                f"bus:{bus.number}",
                bus.available,
                f"{bus.name} {bus.number} {bus.bus_type} {bus.raw}",
            )
            self._set_icon(item, topology_icon_path(bus))
        for branch in sorted(project.branches, key=lambda value: value.name.casefold()):
            if f"branch:{branch.name}" in deleted:
                continue
            item = self._item(
                branches,
                branch.name,
                f"{branch.branch_type} · {branch.from_bus} → {branch.to_bus}",
                "branch",
                f"branch:{branch.name}",
                branch.available,
                f"{branch.name} {branch.branch_type} {branch.from_bus} {branch.to_bus} {branch.raw}",
            )
            self._set_icon(item, topology_icon_path(branch))
            prefix = f"branches/{branch.name}"
            components = sorted(
                (
                    component
                    for component in project.equations
                    if component.path == prefix
                    or component.path.startswith(prefix + "/")
                ),
                key=lambda component: (component.path.count("/"), component.path),
            )
            component_items: dict[str, QtWidgets.QTreeWidgetItem] = {}
            for component in components:
                parent_path = component.path.rsplit("/", 1)[0]
                parent_item = component_items.get(parent_path, item)
                label = (
                    component.component_type.rsplit(".", 1)[-1]
                    if component.path == prefix
                    else component.path.rsplit("/", 1)[-1]
                )
                component_item = self._item(
                    parent_item,
                    label,
                    f"{component.role} · {component.component_type.rsplit('.', 1)[-1]}",
                    "dynamic",
                    component.path,
                    branch.available,
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
                component_items[component.path] = component_item

        for injection in sorted(project.injections, key=lambda value: value.name.casefold()):
            if f"injection:{injection.name}" in deleted:
                continue
            injection_item = self._item(
                injections,
                injection.name,
                f"{injection.injection_type} · Bus {injection.bus}",
                "injection",
                f"injection:{injection.name}",
                injection.available,
                f"{injection.name} {injection.injection_type} {injection.dynamic_type} {injection.bus} {injection.raw}",
            )
            self._set_icon(injection_item, topology_icon_path(injection))
            prefix = f"injections/{injection.name}"
            components = sorted(
                (
                    item
                    for item in project.equations
                    if item.role != "custom_state"
                    and (item.path == prefix or item.path.startswith(prefix + "/"))
                ),
                key=lambda value: (value.path.count("/"), value.path),
            )
            component_items: dict[str, QtWidgets.QTreeWidgetItem] = {}
            for component in components:
                parent_path = component.path.rsplit("/", 1)[0]
                parent = component_items.get(parent_path, injection_item)
                label = (
                    component.component_type.rsplit(".", 1)[-1]
                    if component.path == prefix
                    else component.path.rsplit("/", 1)[-1]
                )
                component_item = self._item(
                    parent,
                    label,
                    f"{component.role} · {component.component_type.rsplit('.', 1)[-1]}",
                    "custom" if component.role == "custom_dynamic" else "dynamic",
                    component.path,
                    injection.available,
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
                component_item.setToolTip(0, component.path)
                component_items[component.path] = component_item
        for instance in additions:
            parent = buses if instance.kind == "bus" else branches if instance.kind in {"line", "transformer"} else injections
            detail = f"{instance.model_ref} · 编辑新增"
            if instance.kind == "bus" and instance.bus_number is not None:
                detail += f" · #{instance.bus_number}"
            elif document:
                connected = []
                for connection in document.connections:
                    if connection.first_key == instance.key:
                        connected.append(bus_labels.get(connection.second_key, ""))
                    elif connection.second_key == instance.key:
                        connected.append(bus_labels.get(connection.first_key, ""))
                connected = [value for value in connected if value]
                if connected:
                    detail += " · " + " / ".join(connected)
            item = self._item(
                parent,
                instance.name,
                detail,
                "custom",
                instance.key,
                bool(instance.parameters.get("available", True)),
                f"{instance.name} {instance.kind} {instance.model_ref}",
            )
            item.setForeground(0, QtGui.QColor("#2389d7"))
            icon_path = editor_icon_path(instance.model_ref)
            if icon_path:
                self._set_icon(item, icon_path)
            components = editor_device_components(project, instance)
            component_items: dict[str, QtWidgets.QTreeWidgetItem] = {}
            prefix = f"editor-device:{instance.instance_id}"
            for component in components:
                parent_path = component.path.rsplit("/", 1)[0]
                parent_item = component_items.get(parent_path, item)
                label = (
                    component.component_type.rsplit(".", 1)[-1]
                    if component.path == prefix
                    else component.path.rsplit("/", 1)[-1]
                )
                component_item = self._item(
                    parent_item,
                    label,
                    f"{component.role} · {component.component_type.rsplit('.', 1)[-1]}",
                    "custom" if component.role.startswith("custom_") else "dynamic",
                    component.path,
                    bool(instance.parameters.get("available", True)),
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
                component_items[component.path] = component_item
        self._apply_filter()

    def _item(
        self,
        parent: QtWidgets.QTreeWidgetItem | None,
        label: str,
        detail: str,
        kind: str,
        key: str = "",
        available: bool = True,
        search: str = "",
        add_kind: str = "",
    ) -> QtWidgets.QTreeWidgetItem:
        owner = parent if parent is not None else self.tree.invisibleRootItem()
        item = QtWidgets.QTreeWidgetItem(owner, (label, detail))
        item.setData(0, KEY_ROLE, key)
        item.setData(0, KIND_ROLE, kind)
        item.setData(0, SEARCH_ROLE, f"{label} {detail} {search}".casefold())
        item.setData(0, AVAILABLE_ROLE, available)
        item.setData(0, ADD_KIND_ROLE, add_kind)
        if key:
            self.items[key] = item
        if not available:
            for column in range(2):
                item.setForeground(column, QtGui.QColor("#858585"))
            item.setToolTip(1, "available/status = false")
        elif kind == "custom":
            for column in range(2):
                item.setForeground(column, QtGui.QColor("#2389d7"))
        return item

    @staticmethod
    def _set_icon(item: QtWidgets.QTreeWidgetItem, path: str | None) -> None:
        if path:
            item.setIcon(0, QtGui.QIcon(path))

    def _matches_kind(self, item: QtWidgets.QTreeWidgetItem) -> bool:
        selected = self.filter.currentText()
        kind = str(item.data(0, KIND_ROLE) or "")
        if selected == "全部组件":
            return True
        if selected == "拓扑组件":
            return kind in {"bus", "branch", "injection"}
        if selected == "动态组件":
            return kind in {"dynamic", "custom"}
        if selected == "不可用组件":
            return kind != "group" and not bool(item.data(0, AVAILABLE_ROLE))
        return kind == "custom"

    def _apply_filter(self, *_: Any) -> None:
        query = self.search.text().strip().casefold()

        def update(item: QtWidgets.QTreeWidgetItem) -> bool:
            child_visible = any(update(item.child(index)) for index in range(item.childCount()))
            own = self._matches_kind(item) and (
                not query or query in str(item.data(0, SEARCH_ROLE) or "")
            )
            visible = own or child_visible
            item.setHidden(not visible)
            if query and child_visible:
                item.setExpanded(True)
            return visible

        root = self.tree.invisibleRootItem()
        for index in range(root.childCount()):
            update(root.child(index))

    def _selected_keys(self) -> list[str]:
        return [
            str(item.data(0, KEY_ROLE))
            for item in self.tree.selectedItems()
            if item.data(0, KEY_ROLE)
        ]

    def _selection_changed(self) -> None:
        keys = self._selected_keys()
        if keys:
            self.selection_requested.emit(keys)

    def _activated(self, item: QtWidgets.QTreeWidgetItem, _column: int) -> None:
        key = str(item.data(0, KEY_ROLE) or "")
        if key:
            self.activate_requested.emit(key)
        else:
            item.setExpanded(not item.isExpanded())

    def select_keys(self, keys: list[str]) -> None:
        if not self.follow_canvas.isChecked():
            return
        blocker = QtCore.QSignalBlocker(self.tree)
        self.tree.clearSelection()
        current: QtWidgets.QTreeWidgetItem | None = None
        for key in keys:
            item = self.items.get(key)
            if not item:
                continue
            item.setSelected(True)
            current = current or item
            parent = item.parent()
            while parent:
                parent.setExpanded(True)
                parent = parent.parent()
        if current:
            self.tree.setCurrentItem(current)
            self.tree.scrollToItem(current, QtWidgets.QAbstractItemView.ScrollHint.PositionAtCenter)
        del blocker

    def highlight(self, key: str) -> None:
        checked = self.follow_canvas.isChecked()
        self.follow_canvas.setChecked(True)
        self.select_keys([key])
        self.follow_canvas.setChecked(checked)

    def _context_menu(self, position: QtCore.QPoint) -> None:
        item = self.tree.itemAt(position)
        if not item:
            return
        key = str(item.data(0, KEY_ROLE) or "")
        menu = QtWidgets.QMenu(self)
        if key:
            open_action = menu.addAction("打开并定位")
            open_action.triggered.connect(lambda: self.activate_requested.emit(key))
            copy_action = menu.addAction("复制组件名称/路径")
            copy_action.triggered.connect(
                lambda: QtWidgets.QApplication.clipboard().setText(key)
            )
        else:
            add_kind = str(item.data(0, ADD_KIND_ROLE) or "")
            if add_kind:
                add = menu.addAction("添加器件…")
                add.triggered.connect(lambda: self.add_requested.emit(add_kind))
                menu.addSeparator()
            menu.addAction("展开此层级", lambda: item.setExpanded(True))
            menu.addAction("折叠此层级", lambda: item.setExpanded(False))
        menu.exec(self.tree.viewport().mapToGlobal(position))
