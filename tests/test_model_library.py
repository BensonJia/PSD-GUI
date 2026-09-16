from __future__ import annotations

import unittest

from psid_graph_viewer.model_library import ModelLibrary


class ModelLibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.library = ModelLibrary()

    def test_representative_official_models_are_bundled(self) -> None:
        for model_type in (
            "AndersonFouadMachine",
            "AVRTypeII",
            "TGTypeI",
            "SingleMass",
            "AverageConverter",
            "VoltageModeControl",
            "LCLFilter",
            "StandardLoad",
            "Source",
            "DynamicBranch",
            "SingleCageInductionMachine",
            "CSVGN1",
            "MagnitudeOutputCurrentLimiter",
        ):
            with self.subTest(model_type=model_type):
                implementations = self.library.lookup(model_type)
                self.assertTrue(implementations)
                self.assertTrue(
                    any(
                        item.derivatives or item.algebraic or item.mass_matrix
                        for item in implementations
                    )
                )

    def test_composite_type_resolves_child_models_once(self) -> None:
        implementations = self.library.lookup(
            "DynamicGenerator{AndersonFouadMachine, SingleMass, AVRTypeII, TGTypeI, PSSFixed}"
        )
        model_types = [item.model_type for item in implementations]
        self.assertIn("AndersonFouadMachine", model_types)
        self.assertIn("SingleMass", model_types)
        self.assertEqual(len(model_types), len(set(model_types)))


if __name__ == "__main__":
    unittest.main()
