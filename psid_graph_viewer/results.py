from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import zlib
from array import array
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from Qt import QtCore, QtWidgets

from .models import ExportedProject


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def topology_key(project: ExportedProject) -> str:
    """Return a stable identity that excludes editable numeric parameters."""
    structure = {
        "buses": sorted(
            (bus.number, bus.name, bus.bus_type, bus.available) for bus in project.buses
        ),
        "branches": sorted(
            (branch.name, branch.branch_type, branch.from_bus, branch.to_bus, branch.available)
            for branch in project.branches
        ),
        "injections": sorted(
            (item.name, item.injection_type, item.dynamic_type, item.bus, item.available)
            for item in project.injections
        ),
    }
    raw = json.dumps(structure, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ResultDatabase:
    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            root = Path(
                QtCore.QStandardPaths.writableLocation(
                    QtCore.QStandardPaths.StandardLocation.AppDataLocation
                )
            )
            path = root / "simulation_results.sqlite3"
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS topologies (
                    id INTEGER PRIMARY KEY,
                    topology_key TEXT NOT NULL UNIQUE,
                    alias TEXT NOT NULL,
                    project_path TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY,
                    topology_id INTEGER NOT NULL REFERENCES topologies(id),
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    combination_mode TEXT NOT NULL,
                    case_count INTEGER NOT NULL,
                    completed_count INTEGER NOT NULL DEFAULT 0,
                    settings_json TEXT NOT NULL,
                    graph_fingerprint TEXT NOT NULL,
                    error_text TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY,
                    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    case_index INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    parameter_snapshot BLOB NOT NULL,
                    changed_parameters_json TEXT NOT NULL,
                    time_values BLOB,
                    row_count INTEGER NOT NULL DEFAULT 0,
                    signal_count INTEGER NOT NULL DEFAULT 0,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    error_text TEXT NOT NULL DEFAULT '',
                    UNIQUE(task_id, case_index)
                );
                CREATE TABLE IF NOT EXISTS signals (
                    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    values_blob BLOB NOT NULL,
                    PRIMARY KEY(run_id, name)
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_topology ON tasks(topology_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id, case_index);
                """
            )

    def ensure_topology(self, project: ExportedProject) -> int:
        key = topology_key(project)
        with self.connect() as db:
            db.execute(
                """INSERT INTO topologies(topology_key, alias, project_path, manifest_json, created_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(topology_key) DO UPDATE SET
                   alias=excluded.alias, project_path=excluded.project_path,
                   manifest_json=excluded.manifest_json""",
                (
                    key,
                    project.root.name,
                    str(project.root),
                    json.dumps(dict(project.manifest), ensure_ascii=False),
                    utc_now(),
                ),
            )
            row = db.execute(
                "SELECT id FROM topologies WHERE topology_key=?", (key,)
            ).fetchone()
            return int(row["id"])

    def unique_task_name(self, topology_id: int, requested: str) -> str:
        with self.connect() as db:
            names = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM tasks WHERE topology_id=?", (topology_id,)
                )
            }
        if requested not in names:
            return requested
        suffix = 2
        while f"{requested} #{suffix}" in names:
            suffix += 1
        return f"{requested} #{suffix}"

    def create_task(
        self,
        project: ExportedProject,
        name: str,
        combination_mode: str,
        case_count: int,
        settings: dict[str, Any],
    ) -> tuple[int, str]:
        topology_id = self.ensure_topology(project)
        name = self.unique_task_name(topology_id, name)
        now = utc_now()
        with self.connect() as db:
            cursor = db.execute(
                """INSERT INTO tasks(
                    topology_id,name,created_at,started_at,status,combination_mode,
                    case_count,settings_json,graph_fingerprint)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    topology_id,
                    name,
                    now,
                    now,
                    "running",
                    combination_mode,
                    case_count,
                    json.dumps(settings, ensure_ascii=False),
                    str(project.manifest.get("fingerprint", "")),
                ),
            )
            return int(cursor.lastrowid), name

    def begin_run(
        self,
        task_id: int,
        case_index: int,
        parameters: dict[str, Any],
        changed: dict[str, float],
    ) -> int:
        snapshot = zlib.compress(
            json.dumps(parameters, ensure_ascii=False, separators=(",", ":")).encode()
        )
        with self.connect() as db:
            cursor = db.execute(
                """INSERT INTO runs(task_id,case_index,status,started_at,
                   parameter_snapshot,changed_parameters_json) VALUES(?,?,?,?,?,?)""",
                (
                    task_id,
                    case_index,
                    "running",
                    utc_now(),
                    snapshot,
                    json.dumps(changed, ensure_ascii=False),
                ),
            )
            return int(cursor.lastrowid)

    @staticmethod
    def _packed(values: Iterable[float]) -> bytes:
        return zlib.compress(array("d", values).tobytes())

    def complete_run(
        self,
        run_id: int,
        csv_path: str | Path,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with Path(csv_path).open("r", encoding="utf-8", newline="") as stream:
            reader = csv.reader(stream)
            headers = next(reader)
            columns: list[list[float]] = [[] for _ in headers]
            for row in reader:
                if len(row) != len(headers):
                    raise ValueError("仿真结果数据列数不一致")
                for column, value in zip(columns, row):
                    column.append(float(value))
        if not headers or headers[0] != "time":
            raise ValueError("仿真结果缺少 time 列")
        with self.connect() as db:
            db.execute(
                """UPDATE runs SET status='completed', finished_at=?, time_values=?,
                   row_count=?, signal_count=?, metadata_json=? WHERE id=?""",
                (
                    utc_now(),
                    self._packed(columns[0]),
                    len(columns[0]),
                    len(headers) - 1,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    run_id,
                ),
            )
            db.executemany(
                "INSERT INTO signals(run_id,name,ordinal,values_blob) VALUES(?,?,?,?)",
                (
                    (run_id, name, ordinal, self._packed(values))
                    for ordinal, (name, values) in enumerate(
                        zip(headers[1:], columns[1:]), 1
                    )
                ),
            )
            db.execute(
                """UPDATE tasks SET completed_count=(
                   SELECT COUNT(*) FROM runs WHERE task_id=? AND status='completed'
                   ) WHERE id=(SELECT task_id FROM runs WHERE id=?)""",
                (self.task_for_run(run_id, db), run_id),
            )

    @staticmethod
    def task_for_run(run_id: int, db: sqlite3.Connection) -> int:
        return int(db.execute("SELECT task_id FROM runs WHERE id=?", (run_id,)).fetchone()[0])

    def fail_run(self, run_id: int, error: str, status: str = "failed") -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE runs SET status=?, finished_at=?, error_text=? WHERE id=?",
                (status, utc_now(), error, run_id),
            )

    def finish_task(self, task_id: int, status: str, error: str = "") -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE tasks SET status=?, finished_at=?, error_text=? WHERE id=?",
                (status, utc_now(), error, task_id),
            )

    def list_tasks(self, current_topology_key: str | None = None) -> list[sqlite3.Row]:
        query = """SELECT tasks.*, topologies.alias AS topology_alias,
                   topologies.topology_key FROM tasks JOIN topologies
                   ON topologies.id=tasks.topology_id"""
        parameters: tuple[Any, ...] = ()
        if current_topology_key:
            query += " WHERE topologies.topology_key=?"
            parameters = (current_topology_key,)
        query += " ORDER BY tasks.created_at DESC, tasks.id DESC"
        with self.connect() as db:
            return list(db.execute(query, parameters))

    def list_runs(self, task_id: int) -> list[sqlite3.Row]:
        with self.connect() as db:
            return list(
                db.execute(
                    "SELECT * FROM runs WHERE task_id=? ORDER BY case_index", (task_id,)
                )
            )

    def rename_task(self, task_id: int, name: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE tasks SET name=? WHERE id=?", (name, task_id))

    def delete_task(self, task_id: int) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    @staticmethod
    def unpack(blob: bytes | None) -> list[float]:
        if not blob:
            return []
        values = array("d")
        values.frombytes(zlib.decompress(blob))
        return list(values)

    def export_run(self, run_id: int, path: str | Path) -> None:
        with self.connect() as db:
            run = db.execute("SELECT time_values FROM runs WHERE id=?", (run_id,)).fetchone()
            signals = list(
                db.execute(
                    "SELECT name, values_blob FROM signals WHERE run_id=? ORDER BY ordinal",
                    (run_id,),
                )
            )
        if run is None:
            raise ValueError("仿真实例不存在")
        names = ["time", *(row["name"] for row in signals)]
        columns = [self.unpack(run["time_values"]), *(self.unpack(row["values_blob"]) for row in signals)]
        with Path(path).open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(names)
            writer.writerows(zip(*columns))


class DataManagerDialog(QtWidgets.QDialog):
    database_changed = QtCore.Signal(object)

    def __init__(self, database: ResultDatabase, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.database = database
        self.current_topology_key: str | None = None
        self.setWindowTitle("数据管理器")
        self.setModal(False)
        self.resize(1040, 700)
        layout = QtWidgets.QVBoxLayout(self)
        top = QtWidgets.QHBoxLayout()
        self.path_label = QtWidgets.QLabel(str(database.path))
        self.path_label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.all_topologies = QtWidgets.QCheckBox("所有拓扑")
        self.all_topologies.toggled.connect(self.refresh)
        refresh = QtWidgets.QPushButton("刷新")
        refresh.clicked.connect(self.refresh)
        self.change_button = QtWidgets.QPushButton("更换…")
        self.change_button.clicked.connect(self._change_database)
        top.addWidget(QtWidgets.QLabel("数据库："))
        top.addWidget(self.path_label, 1)
        top.addWidget(self.change_button)
        top.addWidget(self.all_topologies)
        top.addWidget(refresh)
        layout.addLayout(top)

        splitter = QtWidgets.QSplitter()
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["任务 / 实例", "时间", "状态", "进度"])
        self.tree.setAlternatingRowColors(True)
        self.tree.itemSelectionChanged.connect(self._show_selection)
        self.details = QtWidgets.QTabWidget()
        self.summary = QtWidgets.QTextBrowser()
        self.parameters = QtWidgets.QTreeWidget()
        self.parameters.setHeaderLabels(["参数", "值"])
        self.results = QtWidgets.QTreeWidget()
        self.results.setHeaderLabels(["信号", "采样数"])
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.details.addTab(self.summary, "概要")
        self.details.addTab(self.parameters, "参数")
        self.details.addTab(self.results, "结果")
        self.details.addTab(self.log, "日志")
        splitter.addWidget(self.tree)
        splitter.addWidget(self.details)
        splitter.setSizes([480, 560])
        layout.addWidget(splitter, 1)

        buttons = QtWidgets.QHBoxLayout()
        self.rename_button = QtWidgets.QPushButton("重命名")
        self.export_button = QtWidgets.QPushButton("导出 CSV…")
        self.delete_button = QtWidgets.QPushButton("删除")
        self.rename_button.clicked.connect(self._rename)
        self.export_button.clicked.connect(self._export)
        self.delete_button.clicked.connect(self._delete)
        buttons.addWidget(self.rename_button)
        buttons.addWidget(self.export_button)
        buttons.addWidget(self.delete_button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.refresh()

    def _change_database(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "选择结果数据库",
            str(self.database.path),
            "SQLite 数据库 (*.sqlite3 *.db)",
        )
        if not path:
            return
        try:
            database = ResultDatabase(path)
        except (OSError, sqlite3.Error) as error:
            QtWidgets.QMessageBox.critical(self, "无法打开数据库", str(error))
            return
        self.database = database
        self.path_label.setText(str(database.path))
        self.database_changed.emit(database)
        self.refresh()

    def set_topology(self, project: ExportedProject | None) -> None:
        self.current_topology_key = topology_key(project) if project else None
        self.refresh()

    @QtCore.Slot()
    def refresh(self) -> None:
        self.tree.clear()
        key = None if self.all_topologies.isChecked() else self.current_topology_key
        groups: dict[str, QtWidgets.QTreeWidgetItem] = {}
        for task in self.database.list_tasks(key):
            topology = task["topology_alias"]
            group_key = task["topology_key"]
            parent = groups.get(group_key)
            if parent is None:
                parent = QtWidgets.QTreeWidgetItem(self.tree, [topology])
                parent.setToolTip(0, group_key)
                groups[group_key] = parent
            progress = f'{task["completed_count"]}/{task["case_count"]}'
            item = QtWidgets.QTreeWidgetItem(
                parent,
                [task["name"], task["created_at"], task["status"], progress],
            )
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, ("task", task["id"]))
            for run in self.database.list_runs(task["id"]):
                child = QtWidgets.QTreeWidgetItem(
                    item,
                    [f'实例 #{run["case_index"]}', run["started_at"] or "", run["status"], ""],
                )
                child.setData(0, QtCore.Qt.ItemDataRole.UserRole, ("run", run["id"]))
        self.tree.expandToDepth(1)
        self.tree.resizeColumnToContents(0)
        self._show_selection()

    def _selection(self) -> tuple[str, int] | None:
        selected = self.tree.selectedItems()
        return selected[0].data(0, QtCore.Qt.ItemDataRole.UserRole) if selected else None

    def _show_selection(self) -> None:
        self.parameters.clear()
        self.results.clear()
        self.log.clear()
        selection = self._selection()
        self.rename_button.setEnabled(bool(selection and selection[0] == "task"))
        self.delete_button.setEnabled(bool(selection and selection[0] == "task"))
        self.export_button.setEnabled(bool(selection))
        if not selection:
            self.summary.setHtml("<p>请选择仿真任务或实例。</p>")
            return
        kind, identity = selection
        with self.database.connect() as db:
            if kind == "task":
                row = db.execute("SELECT * FROM tasks WHERE id=?", (identity,)).fetchone()
                self.summary.setHtml(
                    f'<h3>{row["name"]}</h3><p>状态：{row["status"]}<br>'
                    f'实例：{row["completed_count"]}/{row["case_count"]}<br>'
                    f'创建：{row["created_at"]}<br>完成：{row["finished_at"] or "—"}</p>'
                )
                self.log.setPlainText(row["error_text"] or "无错误记录")
                return
            row = db.execute("SELECT * FROM runs WHERE id=?", (identity,)).fetchone()
            changed = json.loads(row["changed_parameters_json"])
            changed_root = QtWidgets.QTreeWidgetItem(self.parameters, ["本次扫描变化", ""])
            for key, value in sorted(changed.items()):
                QtWidgets.QTreeWidgetItem(changed_root, [key, str(value)])
            full_root = QtWidgets.QTreeWidgetItem(self.parameters, ["完整参数快照", ""])
            snapshot = json.loads(zlib.decompress(row["parameter_snapshot"]).decode("utf-8"))
            self._add_parameter_items(full_root, snapshot)
            changed_root.setExpanded(True)
            for signal in db.execute(
                "SELECT name FROM signals WHERE run_id=? ORDER BY ordinal", (identity,)
            ):
                QtWidgets.QTreeWidgetItem(self.results, [signal["name"], str(row["row_count"])])
            self.summary.setHtml(
                f'<h3>实例 #{row["case_index"]}</h3><p>状态：{row["status"]}<br>'
                f'采样：{row["row_count"]}<br>信号：{row["signal_count"]}</p>'
            )
            self.log.setPlainText(row["error_text"] or "无错误记录")

    @staticmethod
    def _add_parameter_items(parent: QtWidgets.QTreeWidgetItem, value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                item = QtWidgets.QTreeWidgetItem(parent, [str(key), ""])
                DataManagerDialog._add_parameter_items(item, child)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                item = QtWidgets.QTreeWidgetItem(parent, [f"[{index}]", ""])
                DataManagerDialog._add_parameter_items(item, child)
        else:
            parent.setText(1, str(value))

    def _rename(self) -> None:
        selection = self._selection()
        if not selection or selection[0] != "task":
            return
        name, accepted = QtWidgets.QInputDialog.getText(self, "重命名任务", "任务名称")
        if accepted and name.strip():
            self.database.rename_task(selection[1], name.strip())
            self.refresh()

    def _export(self) -> None:
        selection = self._selection()
        if not selection:
            return
        try:
            if selection[0] == "run":
                path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "导出仿真结果", "simulation.csv", "CSV (*.csv)")
                if path:
                    self.database.export_run(selection[1], path)
            else:
                directory = QtWidgets.QFileDialog.getExistingDirectory(self, "选择任务导出目录")
                if directory:
                    for run in self.database.list_runs(selection[1]):
                        if run["status"] == "completed":
                            self.database.export_run(
                                run["id"], Path(directory) / f'case-{run["case_index"]:04d}.csv'
                            )
        except (OSError, ValueError) as error:
            QtWidgets.QMessageBox.critical(self, "导出失败", str(error))

    def _delete(self) -> None:
        selection = self._selection()
        if not selection or selection[0] != "task":
            return
        if QtWidgets.QMessageBox.question(self, "删除任务", "删除后无法恢复，是否继续？") == QtWidgets.QMessageBox.StandardButton.Yes:
            self.database.delete_task(selection[1])
            self.refresh()
