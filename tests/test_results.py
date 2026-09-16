from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from psid_graph_viewer.loader import ProjectLoader
from psid_graph_viewer.results import ResultDatabase, topology_key


class ResultDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.project = ProjectLoader.load(Path(__file__).parents[1] / "gfm_compiled_network")

    def test_result_round_trip_and_export(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = ResultDatabase(root / "results.sqlite3")
            task_id, name = database.create_task(
                self.project, "test", "cartesian", 1, {"end_time": 1.0}
            )
            self.assertEqual(name, "test")
            run_id = database.begin_run(
                task_id, 1, dict(self.project.system or {}), {"device.gain": 2.0}
            )
            source = root / "source.csv"
            source.write_text("time,a,b\n0,1,2\n0.1,3,4\n", encoding="utf-8")
            database.complete_run(run_id, source, {"column_count": 3})
            database.finish_task(task_id, "completed")

            tasks = database.list_tasks(topology_key(self.project))
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0]["completed_count"], 1)
            target = root / "export.csv"
            database.export_run(run_id, target)
            self.assertEqual(target.read_text(encoding="utf-8").splitlines()[0], "time,a,b")

    def test_duplicate_task_names_get_stable_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = ResultDatabase(Path(directory) / "results.sqlite3")
            _, first = database.create_task(self.project, "scan", "cartesian", 1, {})
            _, second = database.create_task(self.project, "scan", "cartesian", 1, {})
            self.assertEqual((first, second), ("scan", "scan #2"))


if __name__ == "__main__":
    unittest.main()
