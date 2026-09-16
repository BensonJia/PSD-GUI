from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from psid_graph_viewer.editor import (
    EditorConnection,
    EditorDocument,
    EditorDocumentStore,
    EditorInstance,
    PortDefinition,
    SlotOverride,
    default_ports,
    default_instance_parameters,
    upgrade_editor_instances,
)
from psid_graph_viewer.loader import ProjectLoader
from psid_graph_viewer.custom_models import (
    CustomModel,
    CustomModelStore,
    CustomState,
    validate_topology_model,
)


SAMPLE = Path(__file__).resolve().parents[1] / "gfm_compiled_network"


class EditorDocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project = ProjectLoader.load(SAMPLE)

    def test_round_trip_and_structural_fingerprint_excludes_position_and_parameters(self) -> None:
        instance = EditorInstance.new(
            "Bus10", "bus", "ACBus", default_ports("bus"), (10.0, 20.0), {"base_voltage": 230.0}, 10
        )
        document = EditorDocument(
            instances=(instance,),
            port_positions={"bus:1|to:line-1": (0.0, 0.4)},
        )
        changed = document.replace_instance(
            EditorInstance(
                instance.instance_id,
                instance.name,
                instance.kind,
                instance.model_ref,
                instance.ports,
                (200.0, 300.0),
                {"base_voltage": 115.0},
                instance.bus_number,
                instance.backend_supported,
            )
        )
        self.assertEqual(document.fingerprint(), changed.fingerprint())
        moved_port = changed.with_port_position("bus:1", "to:line-1", (1.0, 0.6))
        self.assertEqual(document.fingerprint(), moved_port.fingerprint())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            EditorDocumentStore.save(root, document)
            self.assertEqual(EditorDocumentStore.load(root), document)

    def test_required_port_and_cardinality_validation(self) -> None:
        generator = EditorInstance.new(
            "G-new", "generator", "DynamicGenerator", default_ports("generator"), (0.0, 0.0)
        )
        document = EditorDocument(instances=(generator,))
        self.assertTrue(any("尚未连接" in issue.message for issue in document.validate(self.project)))
        connected = document.with_connection(
            EditorConnection.new(generator.key, "terminal", "bus:1", "__edit_in")
        )
        self.assertFalse(any("尚未连接" in issue.message for issue in connected.validate(self.project)))
        connected = connected.with_connection(
            EditorConnection.new(generator.key, "terminal", "bus:2", "__edit_in")
        )
        self.assertTrue(any("只能连接一次" in issue.message for issue in connected.validate(self.project)))

    def test_multiport_model_is_saved_and_reports_backend_contract(self) -> None:
        router = EditorInstance.new(
            "Router",
            "custom",
            "router-model",
            (
                PortDefinition("grid", "电网侧"),
                PortDefinition("load", "负荷侧"),
            ),
            (0.0, 0.0),
            backend_supported=False,
        )
        document = EditorDocument(
            instances=(router,),
            connections=(
                EditorConnection.new(router.key, "grid", "bus:1", "__edit_in"),
                EditorConnection.new(router.key, "load", "bus:2", "__edit_in"),
            ),
            slot_overrides=(SlotOverride("injections/generator-1-1/avr", "m1", "MyAVR", "励磁系统"),),
        )
        issues = document.validate(self.project)
        self.assertTrue(any("Julia 后端" in issue.message for issue in issues))
        self.assertEqual(EditorDocument.from_dict(document.to_dict()), document)

    def test_custom_whole_model_keeps_port_contract(self) -> None:
        ports = (
            PortDefinition("grid", "电网侧"),
            PortDefinition("load", "负荷侧"),
        )
        model = CustomModel(
            model_id="router-model",
            name="EnergyRouter",
            static_injection="",
            description="two-port test model",
            base_power=100.0,
            initialization="fixed",
            states=(CustomState("energy", "", 1.0, 0.0),),
            parameters=(),
            outputs={},
            model_kind="topology",
            base_category="自定义整体器件",
            interface_inputs=("grid_v_r", "grid_v_i", "load_v_r", "load_v_i"),
            interface_outputs=("grid_i_r", "grid_i_i", "load_i_r", "load_i_i"),
            julia_body=(
                "dx.energy = 0.0\n"
                "y.grid_i_r = 0.0\ny.grid_i_i = 0.0\n"
                "y.load_i_r = 0.0\ny.load_i_i = 0.0"
            ),
            ports=ports,
        )
        validate_topology_model(model)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            CustomModelStore.save(root, (model,))
            loaded = CustomModelStore.load(root, self.project.injections)
        self.assertEqual(loaded, (model,))

    def test_legacy_dynamic_placeholder_is_upgraded_to_library_recipe(self) -> None:
        instance = EditorInstance.new(
            "G-new", "generator", "DynamicGenerator", default_ports("generator"), (0.0, 0.0)
        )
        upgraded, count = upgrade_editor_instances(EditorDocument(instances=(instance,)))
        self.assertEqual(count, 1)
        self.assertNotIn("template", upgraded.instances[0].parameters)
        self.assertIn("machine", upgraded.instances[0].parameters)
        self.assertEqual(
            EditorDocument(instances=(instance,)).fingerprint(), upgraded.fingerprint()
        )

    def test_device_model_is_structural_but_runtime_parameters_are_not(self) -> None:
        first = EditorInstance.new(
            "G-new",
            "generator",
            "DynamicGenerator",
            default_ports("generator"),
            (0.0, 0.0),
            {"active_power": 1.0},
        )
        changed_value = EditorInstance(
            **{**first.__dict__, "parameters": {"active_power": 2.0}}
        )
        changed_model = EditorInstance(
            **{**first.__dict__, "model_ref": "DynamicInverter"}
        )
        self.assertEqual(
            EditorDocument(instances=(first,)).fingerprint(),
            EditorDocument(instances=(changed_value,)).fingerprint(),
        )
        self.assertNotEqual(
            EditorDocument(instances=(first,)).fingerprint(),
            EditorDocument(instances=(changed_model,)).fingerprint(),
        )

    def test_legacy_standard_load_receives_editable_parameter_defaults(self) -> None:
        instance = EditorInstance.new(
            "Load-new", "load", "StandardLoad", default_ports("load"), (0.0, 0.0)
        )
        upgraded, count = upgrade_editor_instances(EditorDocument(instances=(instance,)))
        self.assertEqual(count, 1)
        self.assertEqual(
            upgraded.instances[0].parameters,
            default_instance_parameters("StandardLoad"),
        )

    def test_all_materialized_official_models_have_parameter_contracts(self) -> None:
        self.assertEqual(len(default_instance_parameters("ACBus")), 4)
        self.assertEqual(len(default_instance_parameters("StandardLoad")), 13)
        self.assertEqual(len(default_instance_parameters("Source")), 9)
        generator = default_instance_parameters("DynamicGenerator")
        inverter = default_instance_parameters("DynamicInverter")
        self.assertIn("machine", generator)
        self.assertIn("prime_mover", generator)
        self.assertIn("converter", inverter)
        self.assertIn("filter", inverter)


if __name__ == "__main__":
    unittest.main()
