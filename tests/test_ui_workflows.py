from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from Qt import QtCore, QtTest, QtWidgets
    from psid_graph_viewer.__main__ import configure_application
    from psid_graph_viewer.window import MainWindow
except ImportError:
    QtCore = None
    QtTest = None
    QtWidgets = None
    configure_application = None
    MainWindow = None


SAMPLE = Path(__file__).resolve().parents[1] / "gfm_compiled_network"


@unittest.skipIf(QtWidgets is None, "Qt binding is not installed")
class UiWorkflowTests(unittest.TestCase):
    """Exercise complete UI paths using the same events as a desktop user."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self) -> None:
        self.window = MainWindow()
        self.window.show()
        self.app.processEvents()

    def tearDown(self) -> None:
        with patch.object(
            QtWidgets.QMessageBox,
            "question",
            return_value=QtWidgets.QMessageBox.StandardButton.Discard,
        ):
            self.window.close()
        self.app.processEvents()

    def test_application_branding_uses_project_name_and_logo(self) -> None:
        configure_application(self.app)
        self.assertEqual(self.window.windowTitle(), "PowerSimulationDynamics-GUI")
        self.assertEqual(self.app.applicationName(), "PSD-GUI")
        self.assertEqual(self.app.applicationDisplayName(), "PSD-GUI")
        self.assertEqual(self.app.style().objectName().casefold(), "macos")
        self.assertFalse(self.window.windowIcon().isNull())
        self.assertFalse(self.app.windowIcon().isNull())

    def _open_sample_from_welcome_page(self) -> None:
        buttons = self.window.canvas_stack.currentWidget().findChildren(
            QtWidgets.QPushButton
        )
        open_button = next(button for button in buttons if button.text() == "打开项目…")
        with patch.object(
            QtWidgets.QFileDialog,
            "getExistingDirectory",
            return_value=str(SAMPLE),
        ):
            QtTest.QTest.mouseClick(
                open_button, QtCore.Qt.MouseButton.LeftButton
            )
        self.app.processEvents()

    def test_welcome_open_button_loads_project_and_enables_project_actions(self) -> None:
        self.assertEqual(self.window.canvas_stack.currentIndex(), 0)
        self.assertFalse(self.window.reset_layout_action.isEnabled())
        self.assertFalse(self.window.run_simulation_action.isEnabled())

        self._open_sample_from_welcome_page()

        self.assertEqual(self.window.canvas_stack.currentIndex(), 1)
        self.assertEqual(self.window.current_view_key, "topology")
        self.assertIn("gfm_compiled_network", self.window.breadcrumb.text())
        self.assertTrue(self.window.reset_layout_action.isEnabled())
        self.assertTrue(self.window.run_simulation_action.isEnabled())
        self.assertTrue(self.window.custom_models_action.isEnabled())
        self.assertTrue(self.window.nodes)

    def test_invalid_project_dialog_keeps_current_screen_usable(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(
            QtWidgets.QFileDialog,
            "getExistingDirectory",
            return_value=directory,
        ), patch.object(QtWidgets.QMessageBox, "critical") as critical:
            self.window.open_action.trigger()

        critical.assert_called_once()
        title, message = critical.call_args.args[1:3]
        self.assertEqual(title, "无法打开项目")
        self.assertIn("manifest.toml", message)
        self.assertIsNone(self.window.project)
        self.assertEqual(self.window.canvas_stack.currentIndex(), 0)
        self.assertTrue(self.window.open_action.isEnabled())

    def test_return_runs_search_and_clear_restores_canvas_selection(self) -> None:
        self._open_sample_from_welcome_page()
        self.window.search.setFocus()
        QtTest.QTest.keyClicks(self.window.search, "ActivePowerDroop")
        QtTest.QTest.keyClick(self.window.search, QtCore.Qt.Key.Key_Return)
        self.app.processEvents()

        matches = [node for node in self.window.nodes.values() if node.selected()]
        self.assertEqual([node.viewer_key for node in matches], ["injection:generator-2-1"])
        self.assertIn("找到 1 个匹配节点", self.window.statusBar().currentMessage())

        self.window.search.clear()
        self.app.processEvents()
        self.assertFalse(any(node.selected() for node in self.window.nodes.values()))

    def test_component_tree_double_click_opens_device_and_home_returns(self) -> None:
        self._open_sample_from_welcome_page()
        tree = self.window.components.tree
        item = self.window.components.items[
            "injections/generator-2-1/outer_control/active_power_control"
        ]
        parent = item.parent()
        while parent is not None:
            parent.setExpanded(True)
            parent = parent.parent()
        tree.scrollToItem(item)
        self.app.processEvents()

        self.assertFalse(tree.visualItemRect(item).isEmpty())
        tree.itemDoubleClicked.emit(item, 0)
        self.app.processEvents()

        self.assertEqual(self.window.current_view_key, "device:generator-2-1")
        self.assertTrue(self.window.home_action.isEnabled())
        self.assertIn(
            "injections/generator-2-1/outer_control/active_power_control",
            self.window.components._selected_keys(),
        )

        self.window.home_action.trigger()
        self.app.processEvents()
        self.assertEqual(self.window.current_view_key, "topology")
        self.assertFalse(self.window.home_action.isEnabled())

    def test_edit_mode_exposes_tools_and_canvas_key_switches_active_tool(self) -> None:
        self._open_sample_from_welcome_page()
        self.assertFalse(self.window.edit_toolbar.isVisible())

        self.window.edit_mode_action.trigger()
        self.app.processEvents()
        self.assertTrue(self.window.edit_toolbar.isVisible())
        self.assertTrue(self.window.connect_tool_action.isEnabled())

        viewport = self.window.graph.viewer().viewport()
        viewport.setFocus()
        QtTest.QTest.keyClick(viewport, QtCore.Qt.Key.Key_W)
        self.app.processEvents()
        self.assertEqual(self.window.edit_tool, "connect")
        self.assertTrue(self.window.connect_tool_action.isChecked())

        self.window.edit_mode_action.trigger()
        self.app.processEvents()
        self.assertFalse(self.window.edit_toolbar.isVisible())
        self.assertFalse(self.window.connect_tool_action.isEnabled())

    def test_project_and_simulation_menus_and_component_edit_checkbox(self) -> None:
        menus = [action.text() for action in self.window.menuBar().actions()]
        self.assertEqual(menus, ["项目", "编辑", "仿真", "视图", "帮助"])
        self.assertFalse(self.window.components.edit_mode.isEnabled())
        self._open_sample_from_welcome_page()

        self.assertTrue(self.window.components.edit_mode.isEnabled())
        self.window.components.edit_mode.setChecked(True)
        self.app.processEvents()
        self.assertTrue(self.window.edit_mode_action.isChecked())
        self.assertTrue(self.window.edit_toolbar.isVisible())
        self.assertEqual(
            self.window.dockWidgetArea(self.window.drawing_dock),
            QtCore.Qt.DockWidgetArea.TopDockWidgetArea,
        )
        self.assertGreaterEqual(
            self.window.drawing_dock.geometry().left(),
            self.window.component_dock.geometry().right(),
        )

    def test_new_project_creates_editable_empty_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(
            QtWidgets.QFileDialog,
            "getExistingDirectory",
            return_value=directory,
        ), patch.object(
            QtWidgets.QInputDialog,
            "getText",
            return_value=("empty-grid", True),
        ):
            self.window.new_project_action.trigger()
            self.app.processEvents()

            root = Path(directory).resolve() / "empty-grid"
            self.assertEqual(self.window.project.root, root)
            self.assertTrue((root / "manifest.toml").is_file())
            self.assertTrue((root / "topology.toml").is_file())
            self.assertTrue((root / "graph.toml").is_file())
            self.assertTrue((root / "equations/index.toml").is_file())
            self.assertTrue((root / ".psid_gui/model.json").is_file())
            self.assertTrue(self.window.components.edit_mode.isChecked())
            self.assertFalse(self.window.run_simulation_action.isEnabled())

    def test_save_as_copies_project_without_compiled_cache_and_switches_root(self) -> None:
        self._open_sample_from_welcome_page()
        with tempfile.TemporaryDirectory() as directory, patch.object(
            QtWidgets.QFileDialog,
            "getExistingDirectory",
            return_value=directory,
        ), patch.object(
            QtWidgets.QInputDialog,
            "getText",
            return_value=("copied-grid", True),
        ):
            self.window.save_as_action.trigger()
            self.app.processEvents()

            root = Path(directory).resolve() / "copied-grid"
            self.assertEqual(self.window.project.root, root)
            self.assertTrue((root / "manifest.toml").is_file())
            self.assertTrue((root / ".psid_gui/model.json").is_file())
            self.assertFalse((root / "compiled").exists())

    def test_close_prompts_to_save_and_cancel_keeps_window_open(self) -> None:
        self._open_sample_from_welcome_page()
        with patch.object(
            QtWidgets.QMessageBox,
            "question",
            return_value=QtWidgets.QMessageBox.StandardButton.Cancel,
        ) as question:
            self.window.close()
            self.app.processEvents()

        question.assert_called_once()
        self.assertTrue(self.window.isVisible())

    def test_close_saves_project_before_exit(self) -> None:
        self._open_sample_from_welcome_page()
        with patch.object(
            QtWidgets.QMessageBox,
            "question",
            return_value=QtWidgets.QMessageBox.StandardButton.Save,
        ), patch.object(self.window, "_save_editor_document") as save:
            self.window.close()
            self.app.processEvents()

        save.assert_called_once_with(show_message=False)
        self.assertFalse(self.window.isVisible())


if __name__ == "__main__":
    unittest.main()
