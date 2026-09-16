from __future__ import annotations

import os
import sys
import unittest
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from Qt import QtCore, QtGui, QtTest, QtWidgets
    from psid_graph_viewer.graph_builders import (
        BUS_PORT_MIN_DISTANCE,
        EdgeNormalPipeItem,
        topology_icon_name,
    )
    from psid_graph_viewer.custom_models import CustomModel, inherited_model_template
    from psid_graph_viewer.window import MainWindow, ReadOnlyFilter
    from psid_graph_viewer.editor import (
        EditorConnection,
        EditorDocument,
        EditorInstance,
        PortDefinition,
        default_instance_parameters,
        default_ports,
    )
except ImportError:
    QtCore = None
    QtGui = None
    QtTest = None
    QtWidgets = None
    MainWindow = None
    ReadOnlyFilter = None
    EdgeNormalPipeItem = None

from psid_graph_viewer.loader import ProjectLoader
from psid_graph_viewer.models import EquationComponent


SAMPLE = Path(__file__).resolve().parents[1] / "gfm_compiled_network"


@unittest.skipIf(QtWidgets is None, "Qt binding is not installed")
class GuiSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self) -> None:
        self.window = MainWindow()
        self.window.project = replace(
            ProjectLoader.load(SAMPLE), editor_document=EditorDocument()
        )
        self.window.canvas_stack.setCurrentIndex(1)
        self.window.show_topology()
        self.app.processEvents()

    def tearDown(self) -> None:
        with patch.object(
            QtWidgets.QMessageBox,
            "question",
            return_value=QtWidgets.QMessageBox.StandardButton.Discard,
        ):
            self.window.close()

    def expected_topology_node_count(self) -> int:
        project = self.window.project
        document = project.editor_document or self.window.editor_document
        baseline = {
            *(f"bus:{item.number}" for item in project.buses),
            *(f"branch:{item.name}" for item in project.branches),
            *(f"injection:{item.name}" for item in project.injections),
        }
        return len((baseline - set(document.deleted_keys)) | set(document.instance_by_key))

    def test_topology_search_and_device_drill_down(self) -> None:
        self.assertEqual(len(self.window.nodes), self.expected_topology_node_count())
        dynamic = self.window.nodes["injection:generator-2-1"]
        movable = dynamic.view.GraphicsItemFlag.ItemIsMovable
        self.assertTrue(bool(dynamic.view.flags() & movable))
        self.assertTrue(all(port.locked() for port in (*dynamic.input_ports(), *dynamic.output_ports())))
        self.window.search.setText("ActivePowerDroop")
        self.window.search_nodes()
        self.assertTrue(dynamic.selected())
        self.window.show_device("generator-2-1")
        self.assertEqual(len(self.window.nodes), 9)
        self.assertIn(
            "injections/generator-2-1/outer_control/active_power_control",
            self.window.nodes,
        )
        self.window.show_topology()
        self.assertEqual(len(self.window.nodes), self.expected_topology_node_count())

    def test_added_dynamic_devices_and_custom_whole_model_can_open(self) -> None:
        generator = EditorInstance.new(
            "G-added",
            "generator",
            "DynamicGenerator",
            default_ports("generator"),
            (0.0, 0.0),
            default_instance_parameters("DynamicGenerator"),
        )
        inverter = EditorInstance.new(
            "I-added",
            "inverter",
            "DynamicInverter",
            default_ports("inverter"),
            (200.0, 0.0),
            default_instance_parameters("DynamicInverter"),
        )
        custom_model = replace(
            CustomModel.new(),
            name="CustomRouter",
            model_kind="topology",
            ports=(PortDefinition("grid", "电网侧"),),
        )
        custom = EditorInstance.new(
            "Router-added",
            "custom",
            custom_model.model_id,
            custom_model.ports,
            (400.0, 0.0),
            {"base_power": 100.0},
        )
        document = EditorDocument(
            instances=(generator, inverter, custom),
            connections=(
                EditorConnection.new(generator.key, "terminal", "bus:1", "__edit_in"),
                EditorConnection.new(inverter.key, "terminal", "bus:2", "__edit_in"),
                EditorConnection.new(custom.key, "grid", "bus:3", "__edit_in"),
            ),
        )
        self.window.editor_document = document
        self.window.project = replace(
            self.window.project,
            editor_document=document,
            custom_models=(custom_model,),
        )
        self.window.components.set_project(self.window.project)
        self.window.show_topology()

        for instance in (generator, inverter, custom):
            self.assertTrue(self.window._is_openable(self.window.nodes[instance.key]))

        generator_node = self.window.nodes[generator.key]
        generator_node.view.setSelected(True)
        self.window._update_node_actions()
        self.assertTrue(self.window.open_node_action.isEnabled())
        self.window._open_node(generator_node)
        self.assertIn(f"editor-device:{generator.instance_id}/machine", self.window.nodes)
        self.assertIn(f"editor-device:{generator.instance_id}/avr", self.window.nodes)
        self.window.show_device(inverter.key)
        self.assertIn(
            f"editor-device:{inverter.instance_id}/outer_control/active_power_control",
            self.window.nodes,
        )
        self.assertIn(f"editor-device:{inverter.instance_id}/filter", self.window.nodes)
        self.window.show_device(custom.key)
        self.assertIn(f"editor-device:{custom.instance_id}/states/x", self.window.nodes)

        self.assertIn(
            f"editor-device:{generator.instance_id}/machine",
            self.window.components.items,
        )

    def test_component_icons_only_appear_on_topology(self) -> None:
        expected = {
            "bus:1": "busbar",
            "branch:Bus 5-Bus 4-i_1": "cable",
            "branch:Bus 2-Bus 7-i_8": "transformer_2w",
            "injection:generator-1-1": "synchronous_generator",
            "injection:generator-2-1": "generic_ac_source",
            "injection:load51": "load",
        }
        for key, icon in expected.items():
            node = self.window.nodes[key]
            self.assertEqual(topology_icon_name(node.payload), icon)
            self.assertTrue(node.view._has_component_icon)
            self.assertFalse(node.view._icon_item.pixmap().isNull())

        self.window.show_device("generator-2-1")
        self.assertTrue(self.window.nodes)
        self.assertTrue(
            all(not node.view._has_component_icon for node in self.window.nodes.values())
        )
        self.assertTrue(
            all(node.view._icon_item.pixmap().isNull() for node in self.window.nodes.values())
        )

    def test_internal_signal_labels_and_structural_pipes(self) -> None:
        self.window.show_device("generator-2-1")
        converter = self.window.nodes["injections/generator-2-1/converter"]
        labels = {
            getattr(port.view, "signal_label", "")
            for port in (*converter.input_ports(), *converter.output_ports())
        }
        self.assertEqual(labels - {""}, {"Vdc", "md,mq", "Vcnv"})
        for port in (*converter.input_ports(), *converter.output_ports()):
            if getattr(port.view, "signal_label", ""):
                self.assertTrue(port.view.display_name)

        self.window.signal_labels_action.setChecked(False)
        self.assertTrue(
            all(
                not port.view.display_name
                for port in (*converter.input_ports(), *converter.output_ports())
                if getattr(port.view, "signal_label", "")
            )
        )
        self.window.signal_labels_action.setChecked(True)

        root = self.window.nodes["injections/generator-2-1"]
        structural = [
            pipe
            for port in root.output_ports()
            for pipe in port.view.connected_pipes
            if getattr(pipe, "structural", False)
        ]
        self.assertEqual(len(structural), 6)
        self.assertTrue(all(not pipe._dir_pointer.isVisible() for pipe in structural))
        self.assertTrue(
            all(
                getattr(port.view, "structural", False)
                for port in root.output_ports()
                if port.name() != "bus:2"
            )
        )
        self.assertFalse(
            any(
                getattr(port.view, "structural", False)
                for port in (*converter.input_ports(), *converter.output_ports())
                if getattr(port.view, "signal_label", "")
            )
        )

        converter.set_orientation(90)
        for port in (*converter.input_ports(), *converter.output_ports()):
            label = getattr(port.view, "signal_label", "")
            if not label:
                continue
            text = (
                converter.view.get_input_text_item(port.view)
                if port.type_() == "in"
                else converter.view.get_output_text_item(port.view)
            )
            self.assertEqual(text.toPlainText(), label)

    def test_dynamic_branch_can_open_with_endpoint_labels(self) -> None:
        branch = self.window.project.branches[0]
        dynamic_branch = replace(branch, raw={**branch.raw, "dynamic": True})
        root_path = f"branches/{branch.name}"
        equation = EquationComponent(
            root_path,
            "dynamic_branch",
            "DynamicBranch",
            ("ir", "ii"),
            ("mdl_branch_ode!",),
            "models/dynline_model.jl",
            {},
        )
        self.window.project = replace(
            self.window.project,
            branches=(dynamic_branch, *self.window.project.branches[1:]),
            equations=(*self.window.project.equations, equation),
        )
        self.window.components.set_project(self.window.project)
        self.window.show_topology()
        node = self.window.nodes[f"branch:{branch.name}"]
        self.assertTrue(self.window._is_openable(node))
        self.window._open_node(node)
        root = self.window.nodes[root_path]
        self.assertEqual(
            {getattr(port.view, "signal_label", "") for port in root.output_ports()},
            {"from V/I", "to V/I"},
        )

    def test_missing_icon_assets_do_not_interrupt_topology_build(self) -> None:
        with patch(
            "psid_graph_viewer.graph_builders.ICON_DIRECTORY",
            Path("/path/that/does/not/exist"),
        ):
            self.window.show_topology()
        self.assertEqual(len(self.window.nodes), self.expected_topology_node_count())
        self.assertTrue(
            all(not node.view._has_component_icon for node in self.window.nodes.values())
        )

    def test_backend_status_and_manager_are_accessible_from_status_bar(self) -> None:
        self.assertEqual(self.window.backend_status.button.text(), "Offline")
        self.window.julia._set_state("Ready", "Julia 后端已就绪")
        self.assertEqual(self.window.backend_status.button.text(), "Ready")
        self.assertIn("已就绪", self.window.backend_status.toolTip())
        self.window.backend_manager.set_project(
            SAMPLE,
            str(self.window.project.manifest["fingerprint"]),
        )
        self.window.show_backend_manager()
        self.app.processEvents()
        self.assertTrue(self.window.backend_manager.isVisible())
        self.assertEqual(self.window.backend_manager.project_label.text(), str(SAMPLE))
        self.assertTrue(self.window.backend_manager.prepare_button.isEnabled())
        self.window.backend_manager.close()

    def test_panels_are_native_docks_and_data_manager_opens(self) -> None:
        docks = (
            self.window.component_dock,
            self.window.info_dock,
            self.window.parameter_dock,
            self.window.run_dock,
        )
        allowed = (
            QtCore.Qt.DockWidgetArea.LeftDockWidgetArea
            | QtCore.Qt.DockWidgetArea.RightDockWidgetArea
            | QtCore.Qt.DockWidgetArea.BottomDockWidgetArea
        )
        self.assertTrue(all(isinstance(dock, QtWidgets.QDockWidget) for dock in docks))
        self.assertTrue(all(dock.allowedAreas() == allowed for dock in docks))
        self.window.show_data_manager()
        self.app.processEvents()
        self.assertTrue(self.window.data_manager.isVisible())
        self.window.data_manager.close()

    def test_component_tree_lists_project_and_activates_canvas_nodes(self) -> None:
        panel = self.window.components
        panel.set_project(self.window.project)
        self.assertIn("bus:1", panel.items)
        self.assertIn("branch:Bus 5-Bus 4-i_1", panel.items)
        self.assertIn("injection:generator-2-1", panel.items)
        component = "injections/generator-2-1/outer_control/active_power_control"
        self.assertIn(component, panel.items)

        self.window._activate_component_key(component)
        self.app.processEvents()
        self.assertEqual(self.window.current_view_key, "device:generator-2-1")
        self.assertTrue(self.window.nodes[component].selected())
        self.assertTrue(panel.items[component].isSelected())
        self.assertFalse(self.window.component_dock.isFloating())

        self.window._locate_model_usage("ActivePowerDroop")
        self.app.processEvents()
        selected = panel.tree.selectedItems()
        self.assertEqual(len(selected), 1)
        self.assertEqual(
            selected[0].data(0, QtCore.Qt.ItemDataRole.UserRole), component
        )

    def test_component_tree_scrolls_horizontally_when_content_is_clipped(self) -> None:
        panel = self.window.components
        panel.set_project(self.window.project)
        panel.tree.setFixedWidth(220)
        self.app.processEvents()

        header = panel.tree.header()
        scroll_bar = panel.tree.horizontalScrollBar()
        self.assertFalse(header.stretchLastSection())
        self.assertEqual(
            header.sectionResizeMode(0),
            QtWidgets.QHeaderView.ResizeMode.ResizeToContents,
        )
        self.assertGreater(header.length(), panel.tree.viewport().width())
        self.assertGreater(scroll_bar.maximum(), 0)
        scroll_bar.setValue(scroll_bar.maximum())
        self.assertEqual(scroll_bar.value(), scroll_bar.maximum())

    def test_canvas_editor_places_bus_and_persists_project_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = replace(
                self.window.project,
                root=Path(directory),
                editor_document=EditorDocument(),
            )
            self.window.project = project
            self.window.editor_document = EditorDocument()
            self.window.components.set_project(project)
            self.window.show_topology()
            self.window.edit_mode_action.setChecked(True)
            self.app.processEvents()
            self.assertFalse(self.window.edit_toolbar.isHidden())

            self.window._instantiate_requested(
                {"kind": "bus", "model_ref": "ACBus", "source": "instantiate_builtin"}
            )
            self.window._place_instance(QtCore.QPointF(123.0, 247.0))
            self.app.processEvents()
            self.assertEqual(len(self.window.editor_document.instances), 1)
            instance = self.window.editor_document.instances[0]
            self.assertEqual(instance.position, (120.0, 240.0))
            self.assertIn(instance.key, self.window.nodes)
            self.assertTrue((Path(directory) / ".psid_gui/model.json").is_file())
            self.assertGreater(self.window.graph.undo_stack().count(), 0)
            self.window.graph.undo_stack().undo()
            self.assertFalse(self.window.editor_document.instances)
            self.window.graph.undo_stack().redo()
            self.assertEqual(len(self.window.editor_document.instances), 1)

    def test_internal_slot_replacement_is_strongly_typed(self) -> None:
        self.window.show_device("generator-1-1")
        path = "injections/generator-1-1/avr"
        node = self.window.nodes[path]
        self.assertEqual(self.window._internal_category(node), "励磁系统")
        self.window._replace_internal_slot(
            {
                "kind": "internal",
                "component_path": path,
                "base_category": "轴系",
                "source": "instantiate_official",
                "model_ref": "SingleMass",
            }
        )
        self.assertFalse(self.window.editor_document.slot_overrides)

    def test_editor_connects_single_terminal_to_unlimited_bus_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = replace(
                self.window.project,
                root=Path(directory),
                editor_document=EditorDocument(),
            )
            self.window.project = project
            self.window.editor_document = EditorDocument()
            self.window.show_topology()
            self.window.edit_mode_action.setChecked(True)
            self.window._instantiate_requested(
                {
                    "kind": "injection",
                    "model_ref": "DynamicGenerator",
                    "source": "instantiate_builtin",
                }
            )
            self.window._place_instance(QtCore.QPointF(100.0, 100.0))
            self.window._set_edit_tool("connect")
            instance = self.window.editor_document.instances[0]
            generator = self.window.nodes[instance.key]
            bus = self.window.nodes["bus:1"]
            generator.get_output("terminal").connect_to(bus.get_input("__edit_in"))
            self.app.processEvents()
            self.assertEqual(len(self.window.editor_document.connections), 1)
            self.assertFalse(self.window.editor_document.validate(self.window.project))

    def test_bus_uses_only_real_draggable_ports_outside_connect_tool(self) -> None:
        bus = self.window.nodes["bus:7"]
        real_ports = (*bus.input_ports(), *bus.output_ports())
        self.assertTrue(real_ports)
        self.assertTrue(all(getattr(port.view, "bus_edge_port", False) for port in real_ports))
        self.assertFalse(any(port.name().startswith("__edit_") for port in real_ports))

        self.window.edit_mode_action.setChecked(True)
        self.window._set_edit_tool("select")
        self.assertTrue(all(port.view.edge_drag_enabled for port in real_ports))

        self.window._set_edit_tool("connect")
        bus = self.window.nodes["bus:7"]
        self.assertTrue(
            all(
                port.view.edge_drag_enabled
                for port in (*bus.input_ports(), *bus.output_ports())
                if not getattr(port.view, "temporary_connection_handle", False)
            )
        )
        temporary = (bus.get_input("__edit_in"), bus.get_output("__edit_out"))
        self.assertTrue(all(temporary))
        self.assertTrue(all(not port.view.isVisible() for port in temporary))

        self.window._set_edit_tool("select")
        bus = self.window.nodes["bus:7"]
        self.assertIsNone(bus.get_input("__edit_in"))
        self.assertIsNone(bus.get_output("__edit_out"))

    def test_bus_port_collision_spacing(self) -> None:
        bus = self.window.nodes["bus:7"]
        ports = list((*bus.input_ports(), *bus.output_ports()))
        self.assertGreaterEqual(len(ports), 2)
        first, second = ports[:2]
        first.view.set_normalized_position((0.0, 0.5))
        second.view.set_normalized_position((0.0, 0.5))
        rect = bus.view.boundingRect()
        first_value = first.view._point_to_scalar(
            first.view.pos() + first.view.boundingRect().center(), rect
        )
        second_value = second.view._point_to_scalar(
            second.view.pos() + second.view.boundingRect().center(), rect
        )
        perimeter = 2.0 * (rect.width() + rect.height())
        distance = min(
            abs(first_value - second_value),
            perimeter - abs(first_value - second_value),
        )
        self.assertGreaterEqual(distance, 19.9)

    def test_resized_bus_keeps_geometry_when_connect_mode_adds_handles(self) -> None:
        bus = self.window.nodes["bus:7"]
        bus.set_size(240.0, 300.0)

        self.window.edit_mode_action.setChecked(True)
        self.window._set_edit_tool("connect")

        self.assertEqual(bus.size, (240.0, 300.0))
        self.assertEqual(bus.custom_size, (240.0, 300.0))
        expected = {
            "nw": (0.0, 0.0),
            "n": (120.0, 0.0),
            "ne": (240.0, 0.0),
            "e": (240.0, 150.0),
            "se": (240.0, 300.0),
            "s": (120.0, 300.0),
            "sw": (0.0, 300.0),
            "w": (0.0, 150.0),
        }
        self.assertEqual(
            {
                direction: (handle.pos().x(), handle.pos().y())
                for direction, handle in bus.view.resize_handles.items()
            },
            expected,
        )

        self.window._set_edit_tool("select")
        self.assertEqual(bus.size, (240.0, 300.0))
        self.assertEqual(bus.view.resize_handles["s"].pos().y(), 300.0)

    def test_manual_bus_port_position_survives_connect_mode(self) -> None:
        bus = self.window.nodes["bus:7"]
        port = (*bus.input_ports(), *bus.output_ports())[0]
        port.view.set_normalized_position((0.5, 0.0))
        before = port.view.normalized_position()

        self.window.edit_mode_action.setChecked(True)
        self.window._set_edit_tool("connect")

        self.assertEqual(port.view.normalized_position(), before)
        self.window._set_edit_tool("select")
        self.assertEqual(port.view.normalized_position(), before)

    def test_temporary_bus_handle_avoids_connected_ports(self) -> None:
        bus = self.window.nodes["bus:7"]
        real_ports = list((*bus.input_ports(), *bus.output_ports()))
        real_ports[0].view.set_normalized_position((1.0, 0.5))

        self.window.edit_mode_action.setChecked(True)
        self.window._set_edit_tool("connect")

        temporary = bus.get_input("__edit_in").view
        rect = bus.view.boundingRect()
        temporary_center = temporary.pos() + temporary.boundingRect().center()
        temporary_scalar = temporary._point_to_scalar(temporary_center, rect)
        perimeter = 2.0 * (rect.width() + rect.height())
        clearances = []
        for port in real_ports:
            center = port.view.pos() + port.view.boundingRect().center()
            scalar = temporary._point_to_scalar(center, rect)
            clearances.append(
                min(
                    abs(temporary_scalar - scalar),
                    perimeter - abs(temporary_scalar - scalar),
                )
            )
        self.assertAlmostEqual(temporary_center.x(), rect.right(), places=3)
        self.assertGreaterEqual(min(clearances), BUS_PORT_MIN_DISTANCE - 0.1)

        bus.set_size(240.0, 300.0)
        resized_center = temporary.pos() + temporary.boundingRect().center()
        self.assertAlmostEqual(
            resized_center.x(), bus.view.boundingRect().right(), places=3
        )

    def test_bus_port_move_persists_without_graph_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.window.project = replace(self.window.project, root=Path(directory))
            self.window.edit_mode_action.setChecked(True)
            self.window._set_edit_tool("select")
            bus = self.window.nodes["bus:7"]
            port = (*bus.input_ports(), *bus.output_ports())[0]
            before = port.view.normalized_position()
            port.view.set_normalized_position((1.0, 0.5))
            after = port.view.normalized_position()
            node_identity = id(bus)
            self.window._bus_port_moved(bus, port.name(), before, after)
            self.assertEqual(id(self.window.nodes["bus:7"]), node_identity)
            self.assertEqual(
                self.window.editor_document.port_positions[
                    f"bus:7|{port.name()}"
                ],
                after,
            )

    def test_device_drag_near_bus_reveals_one_right_connection_handle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            generator = EditorInstance.new(
                "G-near",
                "generator",
                "DynamicGenerator",
                default_ports("generator"),
                (0.0, 0.0),
            )
            self.window.editor_document = EditorDocument(instances=(generator,))
            self.window.project = replace(
                self.window.project,
                root=Path(directory),
                editor_document=self.window.editor_document,
            )
            self.window.show_topology()
            self.window.edit_mode_action.setChecked(True)
            self.window._set_edit_tool("connect")
            viewer = self.window.graph.viewer()
            viewer._origin_pos = QtCore.QPoint(0, 0)
            viewer.start_live_connection(
                self.window.nodes[generator.key].get_output("terminal").view
            )
            bus = self.window.nodes["bus:1"]
            viewer._show_bus_connection_handles_near(
                bus.view.sceneBoundingRect().center()
            )
            input_handle = bus.get_input("__edit_in").view
            self.assertTrue(input_handle.isVisible())
            self.assertFalse(bus.get_output("__edit_out").view.isVisible())
            handle_center = input_handle.pos() + input_handle.boundingRect().center()
            self.assertAlmostEqual(
                handle_center.x(), bus.view.boundingRect().right(), places=3
            )

            class ReleaseEvent:
                @staticmethod
                def scenePos() -> QtCore.QPointF:
                    return input_handle.scenePos() + input_handle.boundingRect().center()

            viewer.apply_live_connection(ReleaseEvent())
            self.app.processEvents()
            self.assertEqual(len(self.window.editor_document.connections), 1)

    def test_disconnect_action_removes_selected_editor_connection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            generator = EditorInstance.new(
                "G-disconnect",
                "generator",
                "DynamicGenerator",
                default_ports("generator"),
                (0.0, 0.0),
            )
            connection = EditorConnection.new(
                generator.key, "terminal", "bus:1", "__edit_in"
            )
            document = EditorDocument(
                instances=(generator,), connections=(connection,)
            )
            self.window.editor_document = document
            self.window.project = replace(
                self.window.project,
                root=Path(directory),
                editor_document=document,
            )
            self.window.show_topology()
            self.window.edit_mode_action.setChecked(True)
            generator_node = self.window.nodes[generator.key]
            pipe = generator_node.get_output("terminal").view.connected_pipes[0]
            pipe.setSelected(True)
            self.window.disconnect_action.trigger()
            self.assertFalse(self.window.editor_document.connections)

    def test_topology_validation_writes_colored_run_log(self) -> None:
        self.window.run_log.clear()
        self.window.edit_mode_action.setChecked(True)
        with patch.object(QtWidgets.QMessageBox, "information"):
            self.window.validate_action.trigger()
        success = self.window.run_log.document().find("[拓扑校验] 通过")
        self.assertFalse(success.isNull())
        self.assertEqual(
            success.charFormat().foreground().color().name(), "#24a148"
        )

        generator = EditorInstance.new(
            "G-unconnected",
            "generator",
            "DynamicGenerator",
            default_ports("generator"),
            (0.0, 0.0),
        )
        self.window.editor_document = EditorDocument(instances=(generator,))
        with patch.object(QtWidgets.QMessageBox, "information"):
            self.window.validate_action.trigger()
        failure = self.window.run_log.document().find("[拓扑校验] 错误")
        self.assertFalse(failure.isNull())
        self.assertEqual(
            failure.charFormat().foreground().color().name(), "#d94141"
        )

    def test_added_standard_load_has_editable_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.window.project = replace(
                self.window.project,
                root=Path(directory),
                editor_document=EditorDocument(),
            )
            self.window.editor_document = EditorDocument()
            manager = self.window.custom_model_manager
            manager.set_project(self.window.project)
            descriptors = []
            manager.instantiate_requested.connect(descriptors.append)
            manager.begin_instantiation("load")
            manager._instantiate_selected()
            self.assertEqual(len(descriptors), 1)

            self.window._instantiate_requested(descriptors[0])
            self.window._place_instance(QtCore.QPointF(100.0, 100.0))
            instance = self.window.editor_document.instances[0]
            self.assertIn("constant_active_power", instance.parameters)
            self.assertIn("max_current_reactive_power", instance.parameters)

            node = self.window.nodes[instance.key]
            self.window.parameters.set_nodes([node])
            self.assertEqual(self.window.parameters.tree.topLevelItemCount(), 13)

    def test_model_manager_lists_official_hierarchy_and_addable_custom_level(self) -> None:
        manager = self.window.custom_model_manager
        manager.set_project(self.window.project)
        self.assertEqual(len(manager.library.model_types), 77)
        self.assertEqual(manager.list.count(), 82)

        categories = {}
        iterator = QtWidgets.QTreeWidgetItemIterator(manager.categories)
        while iterator.value():
            item = iterator.value()
            categories[tuple(item.data(0, QtCore.Qt.ItemDataRole.UserRole) or ())] = item
            iterator += 1
        self.assertIn(("动态注入设备", "动态发电机", "励磁系统"), categories)
        self.assertIn(
            ("动态注入设备", "动态逆变器", "外环控制器", "有功控制"),
            categories,
        )
        self.assertNotIn(("项目自定义模型", "官方派生模型"), categories)

        manager.categories.setCurrentItem(
            categories[("动态注入设备", "动态发电机", "励磁系统")]
        )
        self.app.processEvents()
        labels = [manager.list.item(row).text() for row in range(manager.list.count())]
        self.assertTrue(any("AVRTypeII" in label for label in labels))
        self.assertTrue(any("添加自定义励磁系统模型" in label for label in labels))

        derived_add = next(
            manager.list.item(row)
            for row in range(manager.list.count())
            if "添加自定义" in manager.list.item(row).text()
        )
        self.assertEqual(
            derived_add.data(QtCore.Qt.ItemDataRole.UserRole), ("add", "derived")
        )

        manager.categories.setCurrentItem(
            categories[("动态注入设备", "独立动态设备")]
        )
        self.app.processEvents()
        add_items = [
            manager.list.item(row)
            for row in range(manager.list.count())
            if "添加自定义" in manager.list.item(row).text()
        ]
        self.assertEqual(len(add_items), 1)
        self.assertEqual(add_items[0].foreground().color(), QtGui.QColor("#1689d8"))
        manager.set_editing_enabled(False)
        self.assertFalse(manager.create_button.isEnabled())
        manager.set_editing_enabled(True)
        self.assertTrue(manager.create_button.isEnabled())

        derived = inherited_model_template(
            "AVRTypeII",
            "励磁系统",
            manager.library.lookup("AVRTypeII"),
            self.window.project,
        )
        manager.set_project(replace(self.window.project, custom_models=(derived,)))
        categories = {}
        iterator = QtWidgets.QTreeWidgetItemIterator(manager.categories)
        while iterator.value():
            item = iterator.value()
            categories[tuple(item.data(0, QtCore.Qt.ItemDataRole.UserRole) or ())] = item
            iterator += 1
        manager.categories.setCurrentItem(
            categories[("动态注入设备", "动态发电机", "励磁系统")]
        )
        self.app.processEvents()
        derived_items = [
            manager.list.item(row)
            for row in range(manager.list.count())
            if derived.name in manager.list.item(row).text()
        ]
        self.assertEqual(len(derived_items), 1)
        self.assertEqual(derived_items[0].foreground().color(), QtGui.QColor("#2389d7"))
        manager.categories.setCurrentItem(categories[("项目自定义模型",)])
        self.app.processEvents()
        self.assertFalse(
            any(derived.name in manager.list.item(row).text() for row in range(manager.list.count()))
        )

    def test_topology_instantiation_lists_models_not_existing_devices(self) -> None:
        manager = self.window.custom_model_manager
        manager.set_project(self.window.project)
        manager.begin_instantiation("injection")
        labels = [manager.list.item(row).text() for row in range(manager.list.count())]
        self.assertTrue(any("DynamicGenerator" in label for label in labels))
        self.assertTrue(any("DynamicInverter" in label for label in labels))
        self.assertFalse(any("generator-1-1" in label for label in labels))

        descriptor = []
        manager.instantiate_requested.connect(descriptor.append)
        manager._instantiate_selected()
        self.assertEqual(descriptor[0]["source"], "instantiate_builtin")
        self.assertNotIn("template", descriptor[0])
        self.assertIn("machine", descriptor[0]["parameters"])

    def test_group_move_undo_redo_and_reset_layout(self) -> None:
        graph = self.window.graph
        first = self.window.nodes["bus:1"]
        second = self.window.nodes["branch:Bus 4-Bus1-i_7"]
        original = {first.view: tuple(first.pos()), second.view: tuple(second.pos())}
        first.view.xy_pos = (original[first.view][0] + 80, original[first.view][1] + 30)
        second.view.xy_pos = (original[second.view][0] + 80, original[second.view][1] + 30)
        graph._on_nodes_moved(original)

        self.assertTrue(graph.undo_stack().canUndo())
        graph.undo_stack().undo()
        self.assertEqual(tuple(first.pos()), original[first.view])
        self.assertEqual(tuple(second.pos()), original[second.view])
        graph.undo_stack().redo()
        self.assertNotEqual(tuple(first.pos()), original[first.view])
        moved_position = tuple(first.pos())

        self.window.show_device("generator-2-1")
        self.window.show_topology()
        self.assertEqual(tuple(self.window.nodes["bus:1"].pos()), moved_position)

        self.window.reset_layout()
        self.assertEqual(tuple(self.window.nodes["bus:1"].pos()), original[first.view])
        self.assertFalse(graph.undo_stack().canUndo())

    def test_component_actions_transform_undo_and_preserve_state(self) -> None:
        graph = self.window.graph
        dynamic = self.window.nodes["injection:generator-2-1"]
        bus = self.window.nodes["bus:2"]
        bus.view.setSelected(True)
        self.window._update_node_actions()
        self.assertFalse(self.window._is_openable(bus))
        bus.view.setSelected(False)
        dynamic.view.setSelected(True)
        self.window._update_node_actions()
        self.assertTrue(self.window._is_openable(dynamic))
        self.assertTrue(self.window._models_for_node(dynamic))
        self.assertFalse(self.window._models_for_node(bus))
        self.assertEqual(
            self.window.rotate_clockwise_action.shortcut().toString(
                QtGui.QKeySequence.SequenceFormat.PortableText
            ),
            "Ctrl+R",
        )
        self.assertEqual(
            self.window.rotate_counterclockwise_action.shortcut().toString(
                QtGui.QKeySequence.SequenceFormat.PortableText
            ),
            "Ctrl+Shift+R",
        )
        self.assertTrue(self.window.mirror_horizontal_action.shortcut().isEmpty())
        self.assertTrue(self.window.mirror_vertical_action.shortcut().isEmpty())

        pipe = dynamic.output_ports()[0].view.connected_pipes[0]
        self.assertIsInstance(pipe, EdgeNormalPipeItem)
        self.assertEqual(
            EdgeNormalPipeItem.port_normal(dynamic.output_ports()[0].view),
            QtCore.QPointF(1.0, 0.0),
        )
        original_path = QtGui.QPainterPath(pipe.path())
        original_output_x = dynamic.output_ports()[0].view.x()
        self.window.mirror_horizontal_action.trigger()
        self.assertTrue(dynamic.mirror_horizontal)
        self.assertFalse(dynamic.mirror_vertical)

        self.assertEqual(
            EdgeNormalPipeItem.port_normal(dynamic.output_ports()[0].view),
            QtCore.QPointF(-1.0, 0.0),
        )
        self.assertLess(dynamic.output_ports()[0].view.x(), original_output_x)
        self.assertNotEqual(pipe.path(), original_path)

        graph.undo_stack().undo()
        self.assertFalse(dynamic.mirror_horizontal)
        graph.undo_stack().redo()
        self.assertTrue(dynamic.mirror_horizontal)

        self.window.rotate_clockwise_action.trigger()
        self.assertEqual(dynamic.orientation, 90)
        self.assertTrue(dynamic.mirror_horizontal)
        self.assertEqual(
            EdgeNormalPipeItem.port_normal(dynamic.output_ports()[0].view),
            QtCore.QPointF(0.0, 1.0),
        )
        output_y = dynamic.output_ports()[0].view.y()
        self.assertGreater(output_y, 0.0)
        self.assertTrue(self.window.reset_orientation_action.isEnabled())

        self.window.mirror_vertical_action.trigger()
        self.assertTrue(dynamic.mirror_vertical)
        normal = EdgeNormalPipeItem.port_normal(dynamic.output_ports()[0].view)
        self.assertEqual(normal, QtCore.QPointF(0.0, -1.0))
        self.assertLess(dynamic.output_ports()[0].view.y(), output_y)
        path = pipe.path()
        end_stub = path.elementAt(path.elementCount() - 2)
        end = path.elementAt(path.elementCount() - 1)
        self.assertAlmostEqual(end_stub.x - end.x, normal.x() * 20.0)
        self.assertAlmostEqual(end_stub.y - end.y, normal.y() * 20.0)

        self.window.show_device("generator-2-1")
        self.window.show_topology()
        dynamic = self.window.nodes["injection:generator-2-1"]
        self.assertEqual(dynamic.orientation, 90)
        self.assertTrue(dynamic.mirror_horizontal)
        self.assertTrue(dynamic.mirror_vertical)
        self.window.reset_layout()
        dynamic = self.window.nodes["injection:generator-2-1"]
        self.assertEqual(dynamic.orientation, 0)
        self.assertFalse(dynamic.mirror_horizontal)
        self.assertFalse(dynamic.mirror_vertical)

    def test_editor_shortcuts_are_platform_native_and_canvas_scoped(self) -> None:
        native = QtGui.QKeySequence.SequenceFormat.NativeText
        clockwise = self.window.rotate_clockwise_action.shortcut().toString(native)
        counterclockwise = self.window.rotate_counterclockwise_action.shortcut().toString(native)
        if sys.platform == "darwin":
            self.assertEqual(clockwise, "⌘R")
            self.assertEqual(counterclockwise, "⇧⌘R")
        else:
            self.assertEqual(clockwise, "Ctrl+R")
            self.assertEqual(counterclockwise, "Ctrl+Shift+R")
        canvas_context = QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut
        expected = {
            self.window.select_tool_action: "V",
            self.window.pan_tool_action: "H",
            self.window.connect_tool_action: "W",
            self.window.disconnect_action: "X",
            self.window.add_component_action: "A",
            self.window.resize_mode_action: "E",
            self.window.show_model_action: "M",
            self.window.validate_action: "F7",
        }
        for action, shortcut in expected.items():
            self.assertEqual(action.shortcutContext(), canvas_context)
            self.assertEqual(
                action.shortcut().toString(QtGui.QKeySequence.SequenceFormat.PortableText),
                shortcut,
            )
        self.window.show()
        self.app.processEvents()
        self.window.graph.viewer().viewport().setFocus()
        QtTest.QTest.keyClick(
            self.window.graph.viewer().viewport(), QtCore.Qt.Key.Key_H
        )
        self.app.processEvents()
        self.assertEqual(self.window.edit_tool, "pan")
        self.window.search.clear()
        self.window.search.setFocus()
        QtTest.QTest.keyClick(self.window.search, QtCore.Qt.Key.Key_V)
        self.app.processEvents()
        self.assertEqual(self.window.search.text(), "v")
        self.assertEqual(self.window.edit_tool, "pan")

    def test_arrow_shortcut_nudges_selected_node_and_is_undoable(self) -> None:
        node = self.window.nodes["bus:1"]
        node.view.setSelected(True)
        self.window._update_node_actions()
        before = tuple(node.pos())
        right = next(
            action
            for action in self.window.nudge_actions
            if action.shortcut().toString(QtGui.QKeySequence.SequenceFormat.PortableText)
            == "Right"
        )
        right.trigger()
        self.assertEqual(tuple(node.pos()), (before[0] + 1.0, before[1]))
        self.window.graph.undo_stack().undo()
        self.assertEqual(tuple(node.pos()), before)

    def test_model_view_uses_component_states_and_source_equations(self) -> None:
        self.window.show_device("generator-3-1")
        shaft = self.window.nodes["injections/generator-3-1/shaft"]
        models = self.window._models_for_node(shaft)
        self.assertTrue(models)
        page = self.window._model_html(shaft, models)
        self.assertIn("M(x) · ẋ = f(x, u)", page)
        self.assertIn("d(δ)/dt", page)
        self.assertIn("d(ω)/dt", page)
        self.assertIn("generator_models/shaft_models.jl", page)

        root = self.window.nodes["injections/generator-3-1"]
        root_types = {item.model_type for item in self.window._models_for_node(root)}
        self.assertIn("AndersonFouadMachine", root_types)
        self.assertIn("AVRTypeII", root_types)

    def test_parameter_edit_is_grouped_and_undoable(self) -> None:
        self.window.show_device("generator-3-1")
        avr = self.window.nodes["injections/generator-3-1/avr"]
        self.assertEqual(avr.runtime["K0"], 200.0)
        self.window._edit_parameters([avr], ("K0",), 250.0)
        self.assertEqual(avr.runtime["K0"], 250.0)
        self.window.graph.undo_stack().undo()
        self.assertEqual(avr.runtime["K0"], 200.0)
        self.window.graph.undo_stack().redo()
        self.assertEqual(avr.runtime["K0"], 250.0)

        self.window.show_topology()
        first = self.window.nodes["injection:generator-1-1"]
        second = self.window.nodes["injection:generator-3-1"]
        before = [first.runtime["active_power"], second.runtime["active_power"]]
        self.window._edit_parameters([first, second], ("active_power",), 0.75)
        self.assertEqual(first.runtime["active_power"], 0.75)
        self.assertEqual(second.runtime["active_power"], 0.75)
        self.window.graph.undo_stack().undo()
        self.assertEqual(first.runtime["active_power"], before[0])
        self.assertEqual(second.runtime["active_power"], before[1])

    def test_mirror_applies_to_all_selected_nodes(self) -> None:
        graph = self.window.graph
        first = self.window.nodes["bus:1"]
        second = self.window.nodes["bus:2"]
        graph.select_all()
        self.window._update_node_actions()

        self.window.mirror_horizontal_action.trigger()
        self.assertTrue(first.mirror_horizontal)
        self.assertTrue(second.mirror_horizontal)
        graph.undo_stack().undo()
        self.assertFalse(first.mirror_horizontal)
        self.assertFalse(second.mirror_horizontal)

    def test_resize_updates_pipes_undoes_and_preserves_size(self) -> None:
        graph = self.window.graph
        node = self.window.nodes["injection:generator-2-1"]
        node.view.setSelected(True)
        self.window._update_node_actions()
        self.assertEqual(len(node.view.resize_handles), 8)
        self.assertFalse(any(handle.isVisible() for handle in node.view.resize_handles.values()))
        self.window._node_double_clicked(node)
        self.assertIsNone(self.window.current_device)
        self.assertEqual(self.window.resize_node_key, node.viewer_key)
        self.assertTrue(all(handle.isVisible() for handle in node.view.resize_handles.values()))

        before = (*node.pos(), *node.size)
        pipe = node.output_ports()[0].view.connected_pipes[0]
        original_path = QtGui.QPainterPath(pipe.path())
        after = (before[0], before[1], before[2] + 90.0, before[3] + 45.0)
        node.preview_resize(after[:2], after[2:])
        self.window._resize_finished(node, before, after)
        self.assertEqual(node.size, after[2:])
        self.assertNotEqual(pipe.path(), original_path)
        self.assertTrue(self.window.reset_size_action.isEnabled())

        graph.undo_stack().undo()
        self.assertEqual(node.size, before[2:])
        graph.undo_stack().redo()
        self.assertEqual(node.size, after[2:])

        self.window.show_device("generator-2-1")
        self.window.show_topology()
        node = self.window.nodes["injection:generator-2-1"]
        self.assertEqual(node.size, after[2:])
        node.view.setSelected(True)
        self.window._update_node_actions()
        self.window.reset_size_action.trigger()
        self.assertEqual(node.size, node.default_size)

    def test_resize_modifiers_and_escape_cancel(self) -> None:
        node = self.window.nodes["bus:1"]
        handle = node.view.resize_handles["se"]
        x, y = node.pos()
        width, height = node.size
        handle._before = (x, y, width, height)

        pos, size = handle._resized_geometry(
            QtCore.QPointF(60.0, 10.0),
            QtCore.Qt.KeyboardModifier.ShiftModifier,
        )
        self.assertAlmostEqual(size[0] / size[1], width / height)
        self.assertEqual(pos, (x, y))

        right_handle = node.view.resize_handles["e"]
        right_handle._before = (x, y, width, height)
        pos, size = right_handle._resized_geometry(
            QtCore.QPointF(30.0, 0.0),
            QtCore.Qt.KeyboardModifier.AltModifier,
        )
        self.assertEqual(size, (width + 60.0, height))
        self.assertEqual(pos, (x - 30.0, y))

        handle._start_scene_pos = QtCore.QPointF()
        node.preview_resize((x, y), (width + 80.0, height + 40.0))
        viewer = self.window.graph.viewer()
        viewer._psid_active_resize_handle = handle
        escape = QtGui.QKeyEvent(
            QtCore.QEvent.Type.KeyPress,
            QtCore.Qt.Key.Key_Escape,
            QtCore.Qt.KeyboardModifier.NoModifier,
        )
        self.assertTrue(self.window.read_only_filter.eventFilter(viewer, escape))
        self.assertEqual(node.size, (width, height))
        self.assertIsNone(viewer._psid_active_resize_handle)

    def test_double_click_activates_resize_and_selection_change_exits(self) -> None:
        node = self.window.nodes["injection:generator-2-1"]
        other = self.window.nodes["bus:2"]
        self.window._node_double_clicked(node)
        self.assertEqual(self.window.current_view_key, "topology")
        self.assertTrue(all(handle.isVisible() for handle in node.view.resize_handles.values()))

        node.view.setSelected(False)
        other.view.setSelected(True)
        self.window._selection_changed([], [])
        self.assertIsNone(self.window.resize_node_key)
        self.assertFalse(any(handle.isVisible() for handle in node.view.resize_handles.values()))

    def test_ports_follow_connections_and_device_boundary_is_empty(self) -> None:
        topology_ports = [
            port
            for node in self.window.nodes.values()
            for port in (*node.input_ports(), *node.output_ports())
        ]
        self.assertTrue(topology_ports)
        self.assertTrue(all(port.connected_ports() for port in topology_ports))
        self.assertFalse(self.window.nodes["injection:generator-2-1"].input_ports())

        self.window.show_device("generator-2-1")
        root = self.window.nodes["injections/generator-2-1"]
        boundary_ports = [
            port
            for port in root.output_ports()
            if getattr(port.view, "boundary_target", "")
        ]
        self.assertEqual(len(boundary_ports), 1)
        self.assertFalse(boundary_ports[0].connected_ports())
        self.assertIn("Bus 2", boundary_ports[0].view.boundary_target)
        for node in self.window.nodes.values():
            for port in (*node.input_ports(), *node.output_ports()):
                self.assertTrue(
                    port.connected_ports() or getattr(port.view, "boundary_target", "")
                )

        self.window.show()
        self.app.processEvents()
        viewer = self.window.graph.viewer()
        port_view = boundary_ports[0].view
        scene_center = port_view.mapToScene(port_view.boundingRect().center())
        viewport_pos = viewer.mapFromScene(scene_center)
        QtTest.QTest.mouseMove(viewer.viewport(), viewport_pos)
        self.app.processEvents()
        self.assertIs(self.window.read_only_filter.boundary_port, port_view)
        self.assertEqual(self.window.read_only_filter.boundary_timer.interval(), 1000)
        self.assertTrue(self.window.read_only_filter.boundary_timer.isActive())
        QtTest.QTest.mouseMove(viewer.viewport(), QtCore.QPoint(5, 5))
        self.app.processEvents()
        self.assertIsNone(self.window.read_only_filter.boundary_port)
        self.assertFalse(self.window.read_only_filter.boundary_timer.isActive())

    def test_trackpad_wheel_pans_without_consuming_mouse_wheel(self) -> None:
        class WheelEvent:
            def __init__(self, pixel_delta, phase, modifiers):
                self._pixel_delta = pixel_delta
                self._phase = phase
                self._modifiers = modifiers
                self.accepted = False

            def type(self):
                return QtCore.QEvent.Type.Wheel

            def pixelDelta(self):
                return self._pixel_delta

            def angleDelta(self):
                return QtCore.QPoint(0, 120)

            def phase(self):
                return self._phase

            def modifiers(self):
                return self._modifiers

            def accept(self):
                self.accepted = True

            def position(self):
                return QtCore.QPointF(200, 160)

        viewer = self.window.graph.viewer()
        before = QtCore.QRectF(viewer._scene_range)
        trackpad = WheelEvent(
            QtCore.QPoint(18, -24),
            QtCore.Qt.ScrollPhase.ScrollUpdate,
            QtCore.Qt.KeyboardModifier.NoModifier,
        )
        self.assertTrue(self.window.read_only_filter.eventFilter(viewer.viewport(), trackpad))
        self.assertTrue(trackpad.accepted)
        self.assertNotEqual(viewer._scene_range, before)

        mouse_wheel = WheelEvent(
            QtCore.QPoint(),
            QtCore.Qt.ScrollPhase.NoScrollPhase,
            QtCore.Qt.KeyboardModifier.NoModifier,
        )
        self.assertIsNone(ReadOnlyFilter._trackpad_delta(mouse_wheel))

        ctrl_gesture = WheelEvent(
            QtCore.QPoint(0, 12),
            QtCore.Qt.ScrollPhase.ScrollUpdate,
            QtCore.Qt.KeyboardModifier.ControlModifier,
        )
        before_zoom = QtCore.QRectF(viewer._scene_range)
        self.assertTrue(
            self.window.read_only_filter.eventFilter(viewer.viewport(), ctrl_gesture)
        )
        self.assertTrue(ctrl_gesture.accepted)
        self.assertNotEqual(viewer._scene_range, before_zoom)

    def test_native_pinch_zooms_at_gesture_position(self) -> None:
        class NativeGestureEvent:
            accepted = False

            def type(self):
                return QtCore.QEvent.Type.NativeGesture

            def gestureType(self):
                return QtCore.Qt.NativeGestureType.ZoomNativeGesture

            def value(self):
                return 0.08

            def position(self):
                return QtCore.QPointF(320, 220)

            def accept(self):
                self.accepted = True

        viewer = self.window.graph.viewer()
        before = QtCore.QRectF(viewer._scene_range)
        gesture = NativeGestureEvent()
        self.assertTrue(self.window.read_only_filter.eventFilter(viewer.viewport(), gesture))
        self.assertTrue(gesture.accepted)
        self.assertNotEqual(viewer._scene_range, before)

    def test_space_and_left_drag_pans_canvas(self) -> None:
        class InputEvent:
            def __init__(self, event_type, position=(0, 0)):
                self._type = event_type
                self._position = QtCore.QPointF(*position)
                self.accepted = False

            def type(self):
                return self._type

            def key(self):
                return QtCore.Qt.Key.Key_Space

            def isAutoRepeat(self):
                return False

            def button(self):
                return QtCore.Qt.MouseButton.LeftButton

            def modifiers(self):
                return QtCore.Qt.KeyboardModifier.NoModifier

            def position(self):
                return self._position

            def accept(self):
                self.accepted = True

        viewer = self.window.graph.viewer()
        event_filter = self.window.read_only_filter
        self.assertTrue(
            event_filter.eventFilter(
                viewer, InputEvent(QtCore.QEvent.Type.KeyPress)
            )
        )
        before = QtCore.QRectF(viewer._scene_range)
        self.assertTrue(
            event_filter.eventFilter(
                viewer.viewport(),
                InputEvent(QtCore.QEvent.Type.MouseButtonPress, (120, 100)),
            )
        )
        self.assertTrue(
            event_filter.eventFilter(
                viewer.viewport(), InputEvent(QtCore.QEvent.Type.MouseMove, (170, 135))
            )
        )
        self.assertTrue(
            event_filter.eventFilter(
                viewer.viewport(),
                InputEvent(QtCore.QEvent.Type.MouseButtonRelease, (170, 135)),
            )
        )
        self.assertNotEqual(viewer._scene_range, before)
        self.assertTrue(
            event_filter.eventFilter(
                viewer, InputEvent(QtCore.QEvent.Type.KeyRelease)
            )
        )
        self.assertFalse(event_filter.space_pan)


if __name__ == "__main__":
    unittest.main()
