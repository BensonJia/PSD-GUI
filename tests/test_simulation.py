from __future__ import annotations

import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from psid_graph_viewer.simulation import (
    JuliaProcess,
    ParameterExpression,
    SimulationSettings,
    SweepParameter,
    build_parameter_cases,
    editable_values,
    parse_parameter_expression,
    set_value,
    value_at,
    write_settings,
)


class SimulationSupportTests(unittest.TestCase):
    def test_settings_round_trip_to_backend_toml(self) -> None:
        settings = SimulationSettings(end_time=2.5, output_directory="/tmp/结果")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "simulation.toml"
            write_settings(
                path,
                settings,
                project="/tmp/project",
                system_file="/tmp/system.json",
            )
            with path.open("rb") as stream:
                data = tomllib.load(stream)
        self.assertEqual(data["end_time"], 2.5)
        self.assertEqual(data["output_directory"], "/tmp/结果")
        self.assertEqual(data["project"], "/tmp/project")

    def test_settings_support_application_network_cache(self) -> None:
        settings = SimulationSettings()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "simulation.toml"
            write_settings(
                path,
                settings,
                project="/tmp/project",
                system_file="/tmp/system.json",
                network_cache="/tmp/cache",
            )
            with path.open("rb") as stream:
                data = tomllib.load(stream)
        self.assertEqual(data["network_cache"], "/tmp/cache")

    def test_parameter_discovery_excludes_structural_fields(self) -> None:
        runtime = {
            "name": "generator",
            "n_states": 2,
            "gain": 3.0,
            "limits": {"min": -1.0, "max": 1.0},
            "__metadata__": {"type": "Controller"},
        }
        values = editable_values(runtime)
        self.assertEqual(values[("gain",)], 3.0)
        self.assertEqual(values[("limits", "max")], 1.0)
        self.assertNotIn(("n_states",), values)
        set_value(runtime, ("limits", "max"), 2.0)
        self.assertEqual(value_at(runtime, ("limits", "max")), 2.0)

    def test_bundled_julia_backend_exists(self) -> None:
        self.assertTrue(JuliaProcess._backend_script().is_file())
        self.assertTrue((JuliaProcess._julia_environment() / "Project.toml").is_file())
        self.assertEqual(JuliaProcess._julia_executable(), "julia")

    def test_windows_bundle_uses_private_julia(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            install_root = Path(directory)
            python = install_root / "python" / "pythonw.exe"
            julia = install_root / "julia" / "bin" / "julia.exe"
            python.parent.mkdir()
            julia.parent.mkdir(parents=True)
            python.touch()
            julia.touch()
            with (
                mock.patch("psid_graph_viewer.simulation.sys.platform", "win32"),
                mock.patch("psid_graph_viewer.simulation.sys.executable", str(python)),
            ):
                self.assertEqual(JuliaProcess._bundle_resources(), install_root.resolve())
                self.assertEqual(JuliaProcess._julia_executable(), str(julia.resolve()))

    def test_parameter_expression_formats_and_decimal_range(self) -> None:
        self.assertEqual(parse_parameter_expression("1.1").values, (1.1,))
        self.assertEqual(parse_parameter_expression("[1.1, 2.1]").values, (1.1, 2.1))
        self.assertEqual(
            parse_parameter_expression("range(0.1, 0.3, 0.1)").values,
            (0.1, 0.2, 0.3),
        )
        self.assertEqual(
            parse_parameter_expression("range(2, 1, -0.5)").values,
            (2.0, 1.5, 1.0),
        )
        with self.assertRaisesRegex(ValueError, "步长不能为零"):
            parse_parameter_expression("range(1, 2, 0)")

    def test_parameter_cases_support_cartesian_and_zip(self) -> None:
        sweeps = [
            SweepParameter((('a',),), ('x',), 'a.x', ParameterExpression('[1,2]', (1.0, 2.0))),
            SweepParameter((('b',),), ('y',), 'b.y', ParameterExpression('[3,4]', (3.0, 4.0))),
        ]
        self.assertEqual(len(build_parameter_cases(sweeps, "cartesian")), 4)
        self.assertEqual(
            build_parameter_cases(sweeps, "zip"),
            [{0: 1.0, 1: 3.0}, {0: 2.0, 1: 4.0}],
        )


if __name__ == "__main__":
    unittest.main()
