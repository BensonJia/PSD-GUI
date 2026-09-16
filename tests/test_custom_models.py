from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from psid_graph_viewer.custom_models import (
    CustomModel,
    CustomModelError,
    CustomModelStore,
    CustomParameter,
    CustomState,
    inherited_model_template,
    validate_model,
)
from psid_graph_viewer.loader import ProjectLoadError, ProjectLoader
from psid_graph_viewer.model_library import ModelLibrary


SAMPLE = Path(__file__).resolve().parents[1] / "gfm_compiled_network"


def first_order_model() -> CustomModel:
    return CustomModel(
        "first-order",
        "一阶电流响应",
        "load51",
        "测试模型",
        100.0,
        "fixed",
        (CustomState("x", "(gain * v_r - x) / T", 1.0, 0.0),),
        (CustomParameter("gain", 0.1, "pu"), CustomParameter("T", 0.2, "s")),
        {"i_r": "x", "i_i": "0.0"},
    )


class CustomModelTests(unittest.TestCase):
    def test_validates_restricted_equation_dsl(self) -> None:
        validate_model(first_order_model(), {"load51"})
        invalid = CustomModel(
            **{**first_order_model().__dict__, "outputs": {"i_r": "open('x')", "i_i": "0"}}
        )
        with self.assertRaisesRegex(CustomModelError, "不允许调用函数"):
            validate_model(invalid, {"load51"})

    def test_fingerprint_tracks_structure_but_not_parameter_values_or_initial_guess(self) -> None:
        model = first_order_model()
        changed_values = CustomModel(
            **{
                **model.__dict__,
                "states": (CustomState("x", model.states[0].rhs, 1.0, 9.0),),
                "parameters": (CustomParameter("gain", 3.0, "pu"), CustomParameter("T", 4.0, "s")),
                "base_power": 50.0,
            }
        )
        changed_equation = CustomModel(
            **{
                **model.__dict__,
                "states": (CustomState("x", "-x / T", 1.0, 0.0),),
            }
        )
        self.assertEqual(
            CustomModelStore.fingerprint((model,)),
            CustomModelStore.fingerprint((changed_values,)),
        )
        self.assertNotEqual(
            CustomModelStore.fingerprint((model,)),
            CustomModelStore.fingerprint((changed_equation,)),
        )

    def test_sidecar_round_trip_augments_export_without_changing_schema_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            shutil.copytree(SAMPLE, target)
            manifest_before = (target / "manifest.toml").read_bytes()
            CustomModelStore.save(target, (first_order_model(),))
            project = ProjectLoader.load(target)

            injection = next(item for item in project.injections if item.name == "load51")
            self.assertEqual(injection.dynamic_type, "GUIEquationModel")
            self.assertTrue(injection.is_dynamic)
            self.assertEqual(len(project.custom_models), 1)
            paths = {item.path for item in project.equations}
            self.assertIn("injections/load51", paths)
            self.assertIn("injections/load51/states/x", paths)
            self.assertEqual((target / "manifest.toml").read_bytes(), manifest_before)

    def test_loader_reports_invalid_custom_model_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            shutil.copytree(SAMPLE, target)
            invalid = CustomModel(
                **{**first_order_model().__dict__, "static_injection": "missing"}
            )
            CustomModelStore.save(target, (invalid,))
            with self.assertRaisesRegex(ProjectLoadError, "静态注入设备不存在"):
                ProjectLoader.load(target)

    def test_official_derived_model_round_trip_and_restricted_julia(self) -> None:
        library = ModelLibrary()
        model = inherited_model_template(
            "AVRTypeII", "励磁系统", library.lookup("AVRTypeII")
        )
        validate_model(model, set())
        self.assertTrue(model.is_derived)
        self.assertEqual(model.base_model, "AVRTypeII")
        self.assertIn("V_ref", model.interface_inputs)
        self.assertIn("Vf", model.interface_outputs)

        invalid = CustomModel(
            **{**model.__dict__, "helper_functions": "run(`touch /tmp/not-allowed`)"}
        )
        with self.assertRaisesRegex(CustomModelError, "不允许使用"):
            validate_model(invalid, set())

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "project"
            shutil.copytree(SAMPLE, target)
            CustomModelStore.save(target, (model,))
            loaded = ProjectLoader.load(target)
            self.assertEqual(loaded.custom_models, (model,))
            self.assertFalse(any(item.dynamic_type == "GUIEquationModel" for item in loaded.injections))


if __name__ == "__main__":
    unittest.main()
