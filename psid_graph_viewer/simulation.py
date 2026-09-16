from __future__ import annotations

import json
import math
import os
import re
import sys
import tempfile
from decimal import Decimal, InvalidOperation
from itertools import product
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from Qt import QtCore, QtGui, QtWidgets


@dataclass
class SimulationSettings:
    start_time: float = 0.0
    end_time: float = 10.0
    model: str = "ResidualModel"
    solver: str = "IDA"
    initialize_simulation: bool = True
    frequency_reference: str = "ReferenceBus"
    saveat: float = 0.01
    dtmax: float = 0.01
    abstol: float = 1e-9
    reltol: float = 1e-6
    maxiters: int = 1_000_000
    adaptive: bool = True
    disable_timer_outputs: bool = True
    output_directory: str = ""
    output_name: str = "simulation"


@dataclass(frozen=True)
class ParameterExpression:
    source: str
    values: tuple[float, ...]

    @property
    def is_sweep(self) -> bool:
        return len(self.values) > 1 or self.source.lstrip().startswith(("[", "range("))


@dataclass(frozen=True)
class SweepParameter:
    target_paths: tuple[tuple[str | int, ...], ...]
    parameter_path: tuple[str, ...]
    label: str
    expression: ParameterExpression


_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"


def parse_parameter_expression(text: str, maximum: int = 10_000) -> ParameterExpression:
    source = text.strip()
    if re.fullmatch(_NUMBER, source):
        value = float(source)
        if not math.isfinite(value):
            raise ValueError("请输入有限数值")
        return ParameterExpression(source, (value,))
    if source.startswith("[") and source.endswith("]"):
        parts = [part.strip() for part in source[1:-1].split(",")]
        if not parts or any(not part or not re.fullmatch(_NUMBER, part) for part in parts):
            raise ValueError("数组格式应为 [1.0, 2.0]")
        if len(parts) > maximum:
            raise ValueError(f"单个参数最多允许 {maximum} 个值")
        values = tuple(float(part) for part in parts)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("数组只能包含有限数值")
        return ParameterExpression(source, values)
    match = re.fullmatch(
        rf"range\s*\(\s*({_NUMBER})\s*,\s*({_NUMBER})\s*,\s*({_NUMBER})\s*\)",
        source,
    )
    if not match:
        raise ValueError("请输入单值、[a, b] 或 range(a, b, c)")
    try:
        start, stop, step = (Decimal(value) for value in match.groups())
    except InvalidOperation as error:
        raise ValueError("range 参数不是有效数值") from error
    if not step:
        raise ValueError("range 步长不能为零")
    if (stop - start) * step < 0:
        raise ValueError("range 步长方向与起止值不一致")
    values: list[float] = []
    current = start
    compare = (lambda value: value <= stop) if step > 0 else (lambda value: value >= stop)
    while compare(current):
        values.append(float(current))
        if len(values) > maximum:
            raise ValueError(f"单个参数最多允许 {maximum} 个值")
        current += step
    return ParameterExpression(source, tuple(values))


def build_parameter_cases(
    sweeps: list[SweepParameter], mode: str = "cartesian", maximum: int = 10_000
) -> list[dict[int, float]]:
    if not sweeps:
        return [{}]
    if mode == "zip":
        lengths = {len(item.expression.values) for item in sweeps if len(item.expression.values) > 1}
        if len(lengths) > 1:
            raise ValueError("同步配对模式要求所有数组长度一致；单值可以广播")
        count = next(iter(lengths), 1)
        cases = [
            {
                index: item.expression.values[0 if len(item.expression.values) == 1 else case]
                for index, item in enumerate(sweeps)
            }
            for case in range(count)
        ]
    else:
        count = math.prod(len(item.expression.values) for item in sweeps)
        if count > maximum:
            raise ValueError(f"参数组合共 {count} 个实例，超过上限 {maximum}")
        cases = [dict(enumerate(values)) for values in product(*(item.expression.values for item in sweeps))]
    if len(cases) > maximum:
        raise ValueError(f"参数组合共 {len(cases)} 个实例，超过上限 {maximum}")
    return cases


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float)) and math.isfinite(value):
        return str(value)
    raise ValueError(f"无法写入仿真设置值：{value!r}")


def write_settings(path: Path, settings: SimulationSettings, **extra: Any) -> None:
    values = {**settings.__dict__, **extra}
    path.write_text(
        "\n".join(f"{key} = {_toml_value(value)}" for key, value in values.items())
        + "\n",
        encoding="utf-8",
    )


class JuliaProcess(QtCore.QObject):
    phase_changed = QtCore.Signal(str, str)
    log_received = QtCore.Signal(str)
    completed = QtCore.Signal(bool, str)
    state_changed = QtCore.Signal(str, str)
    info_changed = QtCore.Signal(str, str)

    def __init__(self, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self.process = QtCore.QProcess(self)
        self.process.setProcessChannelMode(QtCore.QProcess.ProcessChannelMode.SeparateChannels)
        self.process.readyReadStandardOutput.connect(self._read_stdout)
        self.process.readyReadStandardError.connect(self._read_stderr)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._process_error)
        self._stdout = ""
        self._last_message = ""
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._pending: tuple[str, str | None] | None = None
        self._current_job: str | None = None
        self._closing = False
        self._restart = False
        self._stop_requested = False
        self._stop_timer = QtCore.QTimer(self)
        self._stop_timer.setSingleShot(True)
        self._stop_timer.timeout.connect(self._kill_if_running)
        self.uptime = QtCore.QElapsedTimer()
        self.job_elapsed = QtCore.QElapsedTimer()
        self.state = "Offline"
        self.state_message = "Julia 后端未启动"
        self.info: dict[str, str] = {}

    @property
    def running(self) -> bool:
        return self.process.state() != QtCore.QProcess.ProcessState.NotRunning

    @property
    def busy(self) -> bool:
        return self.state == "Busy"

    @property
    def current_job(self) -> str | None:
        return self._current_job

    @staticmethod
    def _backend_script() -> Path:
        return Path(__file__).resolve().parent / "julia_backend" / "psid_backend.jl"

    @staticmethod
    def _julia_environment() -> Path:
        return Path(__file__).resolve().parent / "julia_backend"

    @staticmethod
    def _bundle_resources() -> Path | None:
        executable = Path(sys.executable).resolve()
        if sys.platform == "darwin" and executable.parent.name == "MacOS":
            return executable.parent.parent / "Resources"
        if sys.platform == "win32":
            install_root = executable.parent.parent
            if (install_root / "julia" / "bin" / "julia.exe").is_file():
                return install_root
        return None

    @classmethod
    def _julia_executable(cls) -> str:
        resources = cls._bundle_resources()
        if resources:
            executable = "julia.exe" if sys.platform == "win32" else "julia"
            return str(resources / "julia" / "bin" / executable)
        return "julia"

    @classmethod
    def _process_environment(cls) -> QtCore.QProcessEnvironment:
        environment = QtCore.QProcessEnvironment.systemEnvironment()
        for key in environment.keys():
            if key.startswith("JULIA_"):
                environment.remove(key)
        resources = cls._bundle_resources()
        if resources:
            cache = (
                Path(
                    QtCore.QStandardPaths.writableLocation(
                        QtCore.QStandardPaths.StandardLocation.CacheLocation
                    )
                )
                / "julia"
            )
            cache.mkdir(parents=True, exist_ok=True)
            (cache / "logs").mkdir(exist_ok=True)
            environment.insert(
                "JULIA_DEPOT_PATH",
                os.pathsep.join((str(cache), str(resources / "julia_depot"))),
            )
            environment.insert("JULIA_LOAD_PATH", "@:@stdlib")
            environment.insert("JULIA_PKG_OFFLINE", "true")
            environment.insert("JULIA_CPU_TARGET", "generic")
            environment.insert("JULIA_HISTORY", str(cache / "logs" / "repl_history.jl"))
        return environment

    def start_backend(self) -> None:
        if self.running:
            return
        self._closing = False
        self._stop_requested = False
        self._stdout = ""
        self.uptime.start()
        self._set_state("Busy", "正在启动 Julia 后端")
        environment = self._julia_environment()
        self.process.setProcessEnvironment(self._process_environment())
        self.process.setProgram(self._julia_executable())
        self.process.setArguments(
            [
                f"--project={environment}",
                "--startup-file=no",
                "--threads=2",
                str(self._backend_script()),
                "serve",
            ]
        )
        self.process.start()

    def precompile(self) -> None:
        self._submit("PRECOMPILE")

    def run(
        self,
        project: Path,
        system: dict[str, Any],
        settings: SimulationSettings,
        custom_models: tuple[Any, ...] = (),
        editor_document: Any = None,
    ) -> None:
        config_file = self._make_config(
            project,
            system,
            settings,
            internal_output=True,
            custom_models=custom_models,
            editor_document=editor_document,
        )
        self.info.pop("result_file", None)
        self.info.pop("metadata_file", None)
        self._submit("RUN", str(config_file))

    def prepare_network(
        self,
        project: Path,
        system: dict[str, Any],
        settings: SimulationSettings,
        custom_models: tuple[Any, ...] = (),
        editor_document: Any = None,
    ) -> None:
        config_file = self._make_config(
            project,
            system,
            settings,
            custom_models=custom_models,
            editor_document=editor_document,
        )
        self._submit("PREPARE", str(config_file))

    def rebuild_network(
        self,
        project: Path,
        system: dict[str, Any],
        settings: SimulationSettings,
        custom_models: tuple[Any, ...] = (),
        editor_document: Any = None,
    ) -> None:
        config_file = self._make_config(
            project,
            system,
            settings,
            custom_models=custom_models,
            editor_document=editor_document,
        )
        self._submit("REBUILD", str(config_file))

    def terminate(self) -> None:
        if not self.running or not self.busy:
            return
        self.phase_changed.emit("cancel", "正在终止仿真")
        self._send("CANCEL")

    def stop_backend(self) -> None:
        if not self.running:
            self._set_state("Offline", "Julia 后端未启动")
            return
        self._stop_requested = True
        self._send("SHUTDOWN")
        self._stop_timer.start(4000)

    def restart_backend(self) -> None:
        if self.running:
            self._restart = True
            self.stop_backend()
        else:
            self._restart = False
            self.start_backend()

    def shutdown(self) -> None:
        self._closing = True
        self._restart = False
        self._stop_requested = True
        if self.running:
            self._send("SHUTDOWN")
            if not self.process.waitForFinished(2500):
                self.process.kill()
                self.process.waitForFinished(1000)

    def _kill_if_running(self) -> None:
        if self.running:
            self.process.kill()

    def _make_config(
        self,
        project: Path,
        system: dict[str, Any],
        settings: SimulationSettings,
        internal_output: bool = False,
        custom_models: tuple[Any, ...] = (),
        editor_document: Any = None,
    ) -> Path:
        if self._temporary is not None:
            raise RuntimeError("已有 Julia 任务正在运行")
        self._temporary = tempfile.TemporaryDirectory(prefix="psid-viewer-")
        temporary = Path(self._temporary.name)
        system_file = temporary / "system.json"
        system_file.write_text(
            json.dumps(system, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        config_file = temporary / "simulation.toml"
        cache = Path(
            QtCore.QStandardPaths.writableLocation(
                QtCore.QStandardPaths.StandardLocation.CacheLocation
            )
        ) / "networks"
        extra = {
            "project": str(project),
            "system_file": str(system_file),
            "network_cache": str(cache),
        }
        structural_edit = bool(
            editor_document
            and (
                editor_document.instances
                or editor_document.deleted_keys
                or editor_document.slot_overrides
            )
        )
        executable_models = tuple(
            model
            for model in custom_models
            if getattr(model, "model_kind", "equation") == "equation"
            or (structural_edit and getattr(model, "model_kind", "") == "topology")
        )
        if executable_models:
            custom_models_file = temporary / "custom_models.json"
            custom_models_file.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "models": [model.to_dict() for model in executable_models],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            extra["custom_models_file"] = str(custom_models_file)
        if structural_edit:
            editor_file = temporary / "editor_model.json"
            editor_file.write_text(
                json.dumps(
                    editor_document.to_dict(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            extra["editor_model_file"] = str(editor_file)
            extra["editor_structure_fingerprint"] = editor_document.fingerprint()
        if internal_output:
            extra.update(output_directory=str(temporary), output_name="result")
        write_settings(
            config_file,
            settings,
            **extra,
        )
        return config_file

    def _submit(self, command: str, argument: str | None = None) -> None:
        if self.busy and self.running:
            raise RuntimeError("Julia 后端正忙")
        self._pending = (command, argument)
        if self.state == "Ready" and self.running:
            self._send_pending()
        else:
            self.start_backend()

    def _send_pending(self) -> None:
        if self._pending is None:
            return
        command, argument = self._pending
        self._pending = None
        self._current_job = command
        self.job_elapsed.start()
        self._set_state("Busy", "正在提交任务")
        self._send(command if argument is None else f"{command}\t{argument}")

    def _send(self, line: str) -> None:
        self.process.write((line + "\n").encode("utf-8"))

    def _set_state(self, state: str, message: str) -> None:
        self.state = state
        self.state_message = message
        self.state_changed.emit(state, message)

    def _finish_job(self, success: bool, message: str) -> None:
        if self._current_job is None:
            return
        self._current_job = None
        self.completed.emit(success, message)
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None

    def _read_stdout(self) -> None:
        self._stdout += bytes(self.process.readAllStandardOutput()).decode(
            "utf-8", errors="replace"
        )
        lines = self._stdout.split("\n")
        self._stdout = lines.pop()
        for line in lines:
            if line.startswith("PSID_STATE\t"):
                _, state, message = line.split("\t", 2)
                self._set_state(state, message)
                if state == "Ready":
                    self._send_pending()
            elif line.startswith("PSID_INFO\t"):
                _, key, value = line.split("\t", 2)
                self.info[key] = value
                self.info_changed.emit(key, value)
            elif line.startswith("PSID_EVENT\t"):
                _, phase, message = line.split("\t", 2)
                self._last_message = message
                self.phase_changed.emit(phase, message)
                if phase == "complete":
                    self._finish_job(True, message)
                elif phase in {"error", "cancelled"}:
                    self._finish_job(False, message)
            elif line:
                self.log_received.emit(line)

    def _read_stderr(self) -> None:
        text = bytes(self.process.readAllStandardError()).decode("utf-8", errors="replace")
        if text:
            self.log_received.emit(text.rstrip())

    def _finished(self, exit_code: int, _status: Any) -> None:
        self._read_stdout()
        self._read_stderr()
        self._stop_timer.stop()
        expected = self._closing or self._restart or self._stop_requested
        message = "Julia 后端已停止" if expected else f"Julia 后端异常退出（代码 {exit_code}）"
        self._set_state("Offline", message)
        if self._current_job is not None:
            self._finish_job(False, message)
        elif self._pending is not None:
            self._pending = None
            self.completed.emit(False, message)
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None
        if self._restart and not self._closing:
            self._restart = False
            QtCore.QTimer.singleShot(0, self.start_backend)
        self._stop_requested = False

    def _process_error(self, error: Any) -> None:
        if error != QtCore.QProcess.ProcessError.FailedToStart:
            return
        message = f"无法启动 Julia：{self._julia_executable()}"
        self._set_state("Offline", message)
        if self._current_job is not None:
            self._finish_job(False, message)
        elif self._pending is not None:
            self._pending = None
            self.completed.emit(False, message)
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None


class BackendStatusWidget(QtWidgets.QWidget):
    clicked = QtCore.Signal()

    COLORS = {"Ready": "#2eaf5d", "Offline": "#d64545", "Busy": "#d9a621"}

    def __init__(self, backend: JuliaProcess, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(5)
        self.light = QtWidgets.QLabel()
        self.light.setFixedSize(11, 11)
        self.button = QtWidgets.QToolButton()
        self.button.setAutoRaise(True)
        self.button.clicked.connect(self.clicked)
        layout.addWidget(self.light)
        layout.addWidget(self.button)
        backend.state_changed.connect(self.set_state)
        self.set_state(backend.state, backend.state_message)

    @QtCore.Slot(str, str)
    def set_state(self, state: str, message: str) -> None:
        color = self.COLORS.get(state, self.COLORS["Offline"])
        self.light.setStyleSheet(
            f"background:{color}; border:1px solid rgba(0,0,0,0.35); border-radius:5px;"
        )
        self.button.setText(state)
        self.setToolTip(message)
        self.button.setToolTip(f"{message}\n点击管理 Julia 后端")
        self.light.setAccessibleName(f"后端状态：{state}")


class BackendManagerDialog(QtWidgets.QDialog):
    prepare_requested = QtCore.Signal()
    rebuild_requested = QtCore.Signal()

    def __init__(
        self,
        backend: JuliaProcess,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.backend = backend
        self.setWindowTitle("仿真后端管理")
        self.setModal(False)
        self.resize(720, 620)
        layout = QtWidgets.QVBoxLayout(self)

        status_group = QtWidgets.QGroupBox("后端")
        status_layout = QtWidgets.QGridLayout(status_group)
        self.state_label = QtWidgets.QLabel()
        state_font = self.state_label.font()
        state_font.setPointSize(state_font.pointSize() + 3)
        state_font.setBold(True)
        self.state_label.setFont(state_font)
        self.detail_label = QtWidgets.QLabel()
        self.pid_label = QtWidgets.QLabel("—")
        self.julia_label = QtWidgets.QLabel("—")
        self.psid_label = QtWidgets.QLabel("—")
        self.uptime_label = QtWidgets.QLabel("00:00:00")
        status_layout.addWidget(self.state_label, 0, 0, 1, 4)
        status_layout.addWidget(self.detail_label, 1, 0, 1, 4)
        status_layout.addWidget(QtWidgets.QLabel("PID"), 2, 0)
        status_layout.addWidget(self.pid_label, 2, 1)
        status_layout.addWidget(QtWidgets.QLabel("Julia"), 2, 2)
        status_layout.addWidget(self.julia_label, 2, 3)
        status_layout.addWidget(QtWidgets.QLabel("PowerSimulationsDynamics"), 3, 0)
        status_layout.addWidget(self.psid_label, 3, 1, 1, 3)
        status_layout.addWidget(QtWidgets.QLabel("运行时间"), 4, 0)
        status_layout.addWidget(self.uptime_label, 4, 1)
        controls = QtWidgets.QHBoxLayout()
        self.start_button = QtWidgets.QPushButton("启动")
        self.stop_button = QtWidgets.QPushButton("停止后端")
        self.restart_button = QtWidgets.QPushButton("重新启动")
        self.start_button.clicked.connect(backend.start_backend)
        self.stop_button.clicked.connect(backend.stop_backend)
        self.restart_button.clicked.connect(backend.restart_backend)
        controls.addWidget(self.start_button)
        controls.addWidget(self.stop_button)
        controls.addWidget(self.restart_button)
        controls.addStretch()
        status_layout.addLayout(controls, 5, 0, 1, 4)
        layout.addWidget(status_group)

        job_group = QtWidgets.QGroupBox("当前任务")
        job_layout = QtWidgets.QHBoxLayout(job_group)
        self.job_label = QtWidgets.QLabel("空闲")
        self.job_time_label = QtWidgets.QLabel("00:00:00")
        self.cancel_button = QtWidgets.QPushButton("终止当前任务")
        self.cancel_button.clicked.connect(backend.terminate)
        job_layout.addWidget(self.job_label, 1)
        job_layout.addWidget(self.job_time_label)
        job_layout.addWidget(self.cancel_button)
        layout.addWidget(job_group)

        cache_group = QtWidgets.QGroupBox("网络计算图")
        cache_layout = QtWidgets.QFormLayout(cache_group)
        self.project_label = QtWidgets.QLabel("尚未打开项目")
        self.project_label.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.project_fingerprint = QtWidgets.QLabel("—")
        self.current_fingerprint = QtWidgets.QLabel("—")
        self.cache_status = QtWidgets.QLabel("尚未检查")
        self.model_label = QtWidgets.QLabel("—")
        cache_layout.addRow("当前项目", self.project_label)
        cache_layout.addRow("项目指纹", self.project_fingerprint)
        cache_layout.addRow("运行时指纹", self.current_fingerprint)
        cache_layout.addRow("缓存状态", self.cache_status)
        cache_layout.addRow("模型形式 / 频率参考", self.model_label)
        cache_buttons = QtWidgets.QHBoxLayout()
        self.prepare_button = QtWidgets.QPushButton("准备当前网络")
        self.rebuild_button = QtWidgets.QPushButton("重新构建当前网络")
        self.prepare_button.clicked.connect(self.prepare_requested)
        self.rebuild_button.clicked.connect(self.rebuild_requested)
        cache_buttons.addWidget(self.prepare_button)
        cache_buttons.addWidget(self.rebuild_button)
        cache_buttons.addStretch()
        cache_layout.addRow(cache_buttons)
        layout.addWidget(cache_group)

        environment_group = QtWidgets.QGroupBox("环境")
        environment_layout = QtWidgets.QHBoxLayout(environment_group)
        self.environment_label = QtWidgets.QLabel(str(backend._julia_environment()))
        self.environment_label.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.precompile_button = QtWidgets.QPushButton("预编译 Julia 环境")
        self.precompile_button.clicked.connect(backend.precompile)
        environment_layout.addWidget(self.environment_label, 1)
        environment_layout.addWidget(self.precompile_button)
        layout.addWidget(environment_group)

        log_group = QtWidgets.QGroupBox("日志")
        log_layout = QtWidgets.QVBoxLayout(log_group)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        buttons = QtWidgets.QHBoxLayout()
        copy_button = QtWidgets.QPushButton("复制")
        clear_button = QtWidgets.QPushButton("清空显示")
        copy_button.clicked.connect(self.log.selectAll)
        copy_button.clicked.connect(self.log.copy)
        clear_button.clicked.connect(self.log.clear)
        buttons.addStretch()
        buttons.addWidget(copy_button)
        buttons.addWidget(clear_button)
        log_layout.addWidget(self.log)
        log_layout.addLayout(buttons)
        layout.addWidget(log_group, 1)

        backend.state_changed.connect(self._state_changed)
        backend.phase_changed.connect(self._phase_changed)
        backend.log_received.connect(self.log.appendPlainText)
        backend.info_changed.connect(self._info_changed)
        self._state_changed(backend.state, backend.state_message)
        for key, value in backend.info.items():
            self._info_changed(key, value)
        self.elapsed_timer = QtCore.QTimer(self)
        self.elapsed_timer.setInterval(1000)
        self.elapsed_timer.timeout.connect(self._update_elapsed)
        self.elapsed_timer.start()

    def set_project(self, path: Path | None, fingerprint: str = "") -> None:
        self.project_label.setText(str(path) if path else "尚未打开项目")
        self.project_fingerprint.setText(fingerprint or "—")
        self.prepare_button.setEnabled(path is not None and not self.backend.busy)
        self.rebuild_button.setEnabled(self.prepare_button.isEnabled())

    @QtCore.Slot(str, str)
    def _state_changed(self, state: str, message: str) -> None:
        colors = BackendStatusWidget.COLORS
        self.state_label.setText(f"● {state}")
        self.state_label.setStyleSheet(f"color:{colors.get(state, colors['Offline'])}")
        self.detail_label.setText(message)
        line = f"[后端 {state}] {message}"
        if not self.log.document().lastBlock().text() == line:
            self.log.appendPlainText(line)
        offline = state == "Offline"
        busy = state == "Busy"
        self.start_button.setEnabled(offline)
        self.stop_button.setEnabled(not offline)
        self.restart_button.setEnabled(not busy)
        self.cancel_button.setEnabled(
            busy and self.backend.current_job in {"RUN", "PREPARE", "REBUILD"}
        )
        self.precompile_button.setEnabled(not busy)
        self.prepare_button.setEnabled(
            not busy and self.project_label.text() != "尚未打开项目"
        )
        self.rebuild_button.setEnabled(self.prepare_button.isEnabled())
        if state == "Ready":
            self.job_label.setText("空闲")

    @QtCore.Slot(str, str)
    def _phase_changed(self, phase: str, message: str) -> None:
        self.job_label.setText(message)
        self.log.appendPlainText(f"[{phase}] {message}")

    @QtCore.Slot(str, str)
    def _info_changed(self, key: str, value: str) -> None:
        labels = {
            "pid": self.pid_label,
            "julia_version": self.julia_label,
            "psid_version": self.psid_label,
            "project_fingerprint": self.project_fingerprint,
            "current_fingerprint": self.current_fingerprint,
            "cache_status": self.cache_status,
        }
        if key in labels:
            labels[key].setText(value)
        if key in {"model", "frequency_reference"}:
            model = self.backend.info.get("model", "—")
            frequency = self.backend.info.get("frequency_reference", "—")
            self.model_label.setText(f"{model} / {frequency}")

    def _update_elapsed(self) -> None:
        self.uptime_label.setText(
            self._format_elapsed(self.backend.uptime.elapsed())
            if self.backend.running and self.backend.uptime.isValid()
            else "00:00:00"
        )
        self.job_time_label.setText(
            self._format_elapsed(self.backend.job_elapsed.elapsed())
            if self.backend.current_job and self.backend.job_elapsed.isValid()
            else "00:00:00"
        )

    @staticmethod
    def _format_elapsed(milliseconds: int) -> str:
        seconds = max(0, milliseconds // 1000)
        return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


STRUCTURAL_PARAMETER_KEYS = {
    "name",
    "number",
    "n_states",
    "states",
    "states_types",
    "available",
    "status",
    "bus",
    "arc",
    "dynamic_injector",
    "services",
    "internal",
    "ext",
    "__metadata__",
}


def editable_values(value: Any, prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], float]:
    result: dict[tuple[str, ...], float] = {}
    if not isinstance(value, dict):
        return result
    for key, child in value.items():
        if key in STRUCTURAL_PARAMETER_KEYS:
            continue
        path = (*prefix, key)
        if isinstance(child, float):
            result[path] = child
        elif isinstance(child, dict) and "__metadata__" not in child:
            result.update(editable_values(child, path))
    return result


def value_at(mapping: dict[str, Any], path: tuple[str, ...]) -> float:
    current: Any = mapping
    for key in path:
        current = current[key]
    return float(current)


def set_value(mapping: dict[str, Any], path: tuple[str, ...], value: float) -> None:
    current: Any = mapping
    for key in path[:-1]:
        current = current[key]
    current[path[-1]] = value


class ParameterEditor(QtWidgets.QWidget):
    expression_requested = QtCore.Signal(object, object, object)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.nodes: list[Any] = []
        self.loading = False
        self.expression_lookup: Any = None
        layout = QtWidgets.QVBoxLayout(self)
        self.caption = QtWidgets.QLabel("选择元件后可编辑运行参数")
        self.caption.setWordWrap(True)
        layout.addWidget(self.caption)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setItemDelegate(_ValueDelegate(self.tree))
        self.tree.setHeaderLabels(["参数", "输入值", "解析结果"])
        self.tree.setAlternatingRowColors(True)
        self.tree.itemChanged.connect(self._item_changed)
        layout.addWidget(self.tree)

    def set_nodes(self, nodes: list[Any]) -> None:
        self.loading = True
        self.nodes = [node for node in nodes if isinstance(getattr(node, "runtime", None), dict)]
        self.tree.clear()
        if not self.nodes:
            self.caption.setText("该元件没有可编辑的运行参数")
            self.loading = False
            return
        type_names = {
            node.runtime.get("__metadata__", {}).get("type", type(node.payload).__name__)
            for node in self.nodes
        }
        if len(type_names) != 1:
            self.caption.setText("多选参数编辑要求元件类别一致")
            self.loading = False
            return
        common = set(editable_values(self.nodes[0].runtime))
        for node in self.nodes[1:]:
            common &= set(editable_values(node.runtime))
        self.caption.setText(
            f"{len(self.nodes)} 个元件 · 修改在下次构建仿真时生效"
            if len(self.nodes) > 1
            else "双击数值或按 Enter 编辑；修改在下次构建仿真时生效"
        )
        groups: dict[tuple[str, ...], QtWidgets.QTreeWidgetItem] = {}
        for path in sorted(common):
            parent = self.tree.invisibleRootItem()
            for depth in range(1, len(path)):
                group_path = path[:depth]
                if group_path not in groups:
                    groups[group_path] = QtWidgets.QTreeWidgetItem(parent, [path[depth - 1], ""])
                parent = groups[group_path]
            values = [value_at(node.runtime, path) for node in self.nodes]
            text = f"{values[0]:.12g}" if all(value == values[0] for value in values) else "多个值"
            if self.expression_lookup:
                stored = self.expression_lookup(self.nodes, path)
                if stored:
                    text = stored.source
            try:
                parsed = parse_parameter_expression(text) if text != "多个值" else None
            except ValueError:
                parsed = None
            result = (
                f"扫描 {len(parsed.values)} 点"
                if parsed and parsed.is_sweep
                else ("单值" if parsed else "")
            )
            item = QtWidgets.QTreeWidgetItem(parent, [path[-1], text, result])
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, path)
            item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsEditable)
        self.tree.expandAll()
        self.tree.resizeColumnToContents(0)
        self.loading = False

    def _item_changed(self, item: QtWidgets.QTreeWidgetItem, column: int) -> None:
        if self.loading or column != 1:
            return
        path = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        if not path:
            return
        try:
            expression = parse_parameter_expression(item.text(1))
        except ValueError as error:
            self.loading = True
            item.setForeground(1, QtGui.QBrush(QtGui.QColor("#d64545")))
            item.setToolTip(1, str(error))
            item.setText(2, "格式错误")
            self.loading = False
            QtWidgets.QToolTip.showText(QtGui.QCursor.pos(), str(error), self)
            return
        self.loading = True
        item.setForeground(1, QtGui.QBrush())
        item.setToolTip(1, "")
        item.setText(2, f"扫描 {len(expression.values)} 点" if expression.is_sweep else "单值")
        self.loading = False
        self.expression_requested.emit(self.nodes, tuple(path), expression)


class _ValueDelegate(QtWidgets.QStyledItemDelegate):
    def createEditor(self, parent: Any, option: Any, index: Any) -> Any:
        if index.column() != 1:
            return None
        return super().createEditor(parent, option, index)


class SimulationSettingsDialog(QtWidgets.QDialog):
    def __init__(
        self,
        settings: SimulationSettings,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("仿真设置")
        self.resize(560, 620)
        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        self.start_time = QtWidgets.QDoubleSpinBox()
        self.end_time = QtWidgets.QDoubleSpinBox()
        for widget in (self.start_time, self.end_time):
            widget.setRange(-1e9, 1e9)
            widget.setDecimals(6)
        self.model = QtWidgets.QComboBox()
        self.model.addItems(["ResidualModel", "MassMatrixModel"])
        self.solver = QtWidgets.QComboBox()
        self.initialize = QtWidgets.QCheckBox("初始化运行点")
        self.frequency = QtWidgets.QComboBox()
        self.frequency.addItems(["ReferenceBus", "ConstantFrequency"])
        self.saveat = self._positive_spin()
        self.dtmax = self._positive_spin()
        self.abstol = self._positive_spin(decimals=12)
        self.reltol = self._positive_spin(decimals=12)
        self.maxiters = QtWidgets.QSpinBox()
        self.maxiters.setRange(1, 2_000_000_000)
        self.adaptive = QtWidgets.QCheckBox("自适应步长")
        self.timer_disabled = QtWidgets.QCheckBox("隐藏构建计时详情")
        form.addRow("起始时间", self.start_time)
        form.addRow("结束时间", self.end_time)
        form.addRow("模型形式", self.model)
        form.addRow("求解器", self.solver)
        form.addRow("初始化", self.initialize)
        form.addRow("频率参考", self.frequency)
        form.addRow("保存间隔 saveat", self.saveat)
        form.addRow("最大步长 dtmax", self.dtmax)
        form.addRow("绝对容差", self.abstol)
        form.addRow("相对容差", self.reltol)
        form.addRow("最大迭代数", self.maxiters)
        form.addRow("步长控制", self.adaptive)
        form.addRow("日志", self.timer_disabled)
        layout.addLayout(form)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_values)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.model.currentTextChanged.connect(self._update_solvers)
        self._load()

    @staticmethod
    def _positive_spin(decimals: int = 8) -> QtWidgets.QDoubleSpinBox:
        widget = QtWidgets.QDoubleSpinBox()
        widget.setDecimals(decimals)
        widget.setRange(0.0, 1e9)
        widget.setSpecialValueText("自动")
        return widget

    def _load(self) -> None:
        s = self.settings
        self.start_time.setValue(s.start_time)
        self.end_time.setValue(s.end_time)
        self.model.setCurrentText(s.model)
        self._update_solvers(s.model)
        self.solver.setCurrentText(s.solver)
        self.initialize.setChecked(s.initialize_simulation)
        self.frequency.setCurrentText(s.frequency_reference)
        self.saveat.setValue(s.saveat)
        self.dtmax.setValue(s.dtmax)
        self.abstol.setValue(s.abstol)
        self.reltol.setValue(s.reltol)
        self.maxiters.setValue(s.maxiters)
        self.adaptive.setChecked(s.adaptive)
        self.timer_disabled.setChecked(s.disable_timer_outputs)

    def _update_solvers(self, model: str) -> None:
        current = self.solver.currentText()
        self.solver.clear()
        self.solver.addItems(["IDA"] if model == "ResidualModel" else ["Rodas4", "Rodas5"])
        if self.solver.findText(current) >= 0:
            self.solver.setCurrentText(current)

    def _accept_values(self) -> None:
        if self.end_time.value() <= self.start_time.value():
            QtWidgets.QMessageBox.warning(self, "仿真设置", "结束时间必须大于起始时间")
            return
        s = self.settings
        s.start_time = self.start_time.value()
        s.end_time = self.end_time.value()
        s.model = self.model.currentText()
        s.solver = self.solver.currentText()
        s.initialize_simulation = self.initialize.isChecked()
        s.frequency_reference = self.frequency.currentText()
        s.saveat = self.saveat.value()
        s.dtmax = self.dtmax.value()
        s.abstol = self.abstol.value()
        s.reltol = self.reltol.value()
        s.maxiters = self.maxiters.value()
        s.adaptive = self.adaptive.isChecked()
        s.disable_timer_outputs = self.timer_disabled.isChecked()
        self.accept()


class SimulationTaskDialog(QtWidgets.QDialog):
    def __init__(
        self,
        topology_alias: str,
        sweeps: list[SweepParameter],
        database_path: Path,
        parent: QtWidgets.QWidget,
    ) -> None:
        super().__init__(parent)
        self.sweeps = sweeps
        self.cases: list[dict[int, float]] = []
        self.setWindowTitle("创建仿真任务")
        self.resize(620, 420)
        outer = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        default_name = f"{topology_alias} · {datetime.now().strftime('%Y-%m-%d %H-%M-%S')}"
        self.name = QtWidgets.QLineEdit(default_name)
        self.mode = QtWidgets.QComboBox()
        self.mode.addItem("笛卡尔积（全部组合）", "cartesian")
        self.mode.addItem("同步配对（单值广播）", "zip")
        self.count = QtWidgets.QLabel()
        database = QtWidgets.QLabel(str(database_path))
        database.setWordWrap(True)
        database.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow("任务名称", self.name)
        form.addRow("组合方式", self.mode)
        form.addRow("仿真实例", self.count)
        form.addRow("结果数据库", database)
        outer.addLayout(form)
        self.preview = QtWidgets.QTreeWidget()
        self.preview.setHeaderLabels(["扫描参数", "表达式", "数量"])
        for item in sweeps:
            QtWidgets.QTreeWidgetItem(
                self.preview,
                [item.label, item.expression.source, str(len(item.expression.values))],
            )
        outer.addWidget(self.preview, 1)
        self.warning = QtWidgets.QLabel()
        self.warning.setWordWrap(True)
        outer.addWidget(self.warning)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText("开始仿真")
        buttons.accepted.connect(self._accept_values)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self.mode.currentIndexChanged.connect(self._update_count)
        self._update_count()

    def _update_count(self) -> None:
        try:
            self.cases = build_parameter_cases(self.sweeps, self.combination_mode)
            self.count.setText(str(len(self.cases)))
            self.warning.setText(
                "实例较多，开始前将再次确认。" if len(self.cases) > 100 else ""
            )
        except ValueError as error:
            self.cases = []
            self.count.setText("无法生成")
            self.warning.setText(str(error))

    @property
    def combination_mode(self) -> str:
        return str(self.mode.currentData())

    def _accept_values(self) -> None:
        name = self.name.text().strip()
        if not name or any(character in name for character in "/\\"):
            QtWidgets.QMessageBox.warning(self, "任务名称", "名称不能为空且不能包含路径分隔符")
            return
        if not self.cases:
            QtWidgets.QMessageBox.warning(self, "参数组合", self.warning.text())
            return
        if len(self.cases) > 100 and QtWidgets.QMessageBox.question(
            self, "大量仿真实例", f"本任务将运行 {len(self.cases)} 个实例，是否继续？"
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self.accept()


# Kept as an import-compatible alias for extensions written against 0.1.x.
OutputDialog = SimulationTaskDialog
