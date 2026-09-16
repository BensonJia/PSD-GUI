from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from psid_graph_viewer.layout import topology_positions
from psid_graph_viewer.loader import ProjectLoadError, ProjectLoader


SAMPLE = Path(__file__).resolve().parents[1] / "gfm_compiled_network"


class ProjectLoaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.project = ProjectLoader.load(SAMPLE)

    def test_loads_exported_sample(self) -> None:
        self.assertEqual(len(self.project.buses), 9)
        self.assertEqual(len(self.project.branches), 9)
        self.assertEqual(len(self.project.injections), 6)
        self.assertEqual(sum(item.is_dynamic for item in self.project.injections), 3)
        self.assertEqual(self.project.graph["variable_count"], 63)

    def test_maps_equation_runtime_parameters(self) -> None:
        runtime = self.project.runtime_for_equation(
            "injections/generator-2-1/outer_control/active_power_control"
        )
        self.assertIsNotNone(runtime)
        self.assertEqual(runtime["__metadata__"]["type"], "ActivePowerDroop")
        self.assertIn("P_ref", runtime)

    def test_layout_is_deterministic_and_starts_at_ref_bus(self) -> None:
        first = topology_positions(self.project)
        second = topology_positions(self.project)
        self.assertEqual(first, second)
        self.assertEqual(first["bus:1"].x, 0.0)
        self.assertEqual(set(first), {
            *(f"bus:{item.number}" for item in self.project.buses),
            *(f"branch:{item.name}" for item in self.project.branches),
            *(f"injection:{item.name}" for item in self.project.injections),
        })

    def test_system_json_is_optional(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            shutil.copytree(SAMPLE, target)
            (target / "system.json").unlink()
            project = ProjectLoader.load(target)
        self.assertIsNone(project.system)
        self.assertEqual(len(project.buses), 9)

    def test_rejects_missing_required_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            shutil.copytree(SAMPLE, target)
            (target / "graph.toml").unlink()
            with self.assertRaisesRegex(ProjectLoadError, "graph.toml"):
                ProjectLoader.load(target)

    def test_rejects_unknown_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            shutil.copytree(SAMPLE, target)
            manifest = target / "manifest.toml"
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace("schema_version = 1", "schema_version = 2"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ProjectLoadError, "schema_version=2"):
                ProjectLoader.load(target)

    def test_rejects_dangling_branch_bus(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            shutil.copytree(SAMPLE, target)
            topology = target / "topology.toml"
            topology.write_text(
                topology.read_text(encoding="utf-8").replace("to = 7", "to = 999", 1),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ProjectLoadError, "不存在的母线"):
                ProjectLoader.load(target)

    def test_rejects_invalid_system_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            shutil.copytree(SAMPLE, target)
            (target / "system.json").write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(ProjectLoadError, "system.json"):
                ProjectLoader.load(target)


if __name__ == "__main__":
    unittest.main()

