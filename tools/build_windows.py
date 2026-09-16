from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build" / "windows"
STAGE = BUILD / "stage" / "PSD-GUI"
DIST = ROOT / "dist"

PYTHON_VERSION = "3.12.10"
PYTHON_URL = (
    "https://www.python.org/ftp/python/3.12.10/"
    "python-3.12.10-embed-amd64.zip"
)
PYTHON_SHA256 = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"

JULIA_VERSION = "1.12.6"
JULIA_URL = (
    "https://julialang-s3.julialang.org/bin/winnt/x64/1.12/"
    "julia-1.12.6-win64.zip"
)
JULIA_SHA256 = "a63d991976e6893f508c512e3dc7bca1836c1a1f6ad1f3e4aedec159b6733e89"


def run(*command: str, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def download(url: str, sha256: str) -> Path:
    cache = BUILD / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / Path(url).name
    if not archive.is_file():
        print(f"Downloading {archive.name}…", flush=True)
        urllib.request.urlretrieve(url, archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != sha256:
        raise RuntimeError(f"Checksum mismatch for {archive.name}: {digest}")
    return archive


def copy_application_sources() -> None:
    ignored = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".DS_Store")
    shutil.copytree(ROOT / "psid_graph_viewer", STAGE / "psid_graph_viewer", ignore=ignored)
    shutil.copytree(
        ROOT / "power_system_component_icons",
        STAGE / "power_system_component_icons",
        ignore=ignored,
    )
    shutil.copytree(
        ROOT / "PowerSimulationsDynamics.jl",
        STAGE / "PowerSimulationsDynamics.jl",
        ignore=ignored,
    )


def build_python_runtime(archive: Path) -> None:
    runtime = STAGE / "python"
    runtime.mkdir(parents=True)
    with zipfile.ZipFile(archive) as package:
        package.extractall(runtime)
    (runtime / "python312._pth").write_text(
        "python312.zip\n.\nLib/site-packages\n..\nimport site\n",
        encoding="utf-8",
    )
    site_packages = runtime / "Lib" / "site-packages"
    site_packages.mkdir(parents=True)
    run(
        sys.executable,
        "-m",
        "pip",
        "install",
        "--target",
        str(site_packages),
        "--platform",
        "win_amd64",
        "--implementation",
        "cp",
        "--python-version",
        "3.12",
        "--abi",
        "cp312",
        "--only-binary=:all:",
        "--no-compile",
        "--cache-dir",
        str(BUILD / "pip-cache"),
        "NodeGraphQt==0.6.44",
        "PySide6==6.11.2",
        "Qt.py==2.0.5",
    )


def build_julia_runtime(archive: Path) -> None:
    extract_root = BUILD / "julia-extract"
    shutil.rmtree(extract_root, ignore_errors=True)
    extract_root.mkdir(parents=True)
    with zipfile.ZipFile(archive) as package:
        package.extractall(extract_root)
    extracted = next(path for path in extract_root.iterdir() if path.is_dir())
    shutil.move(str(extracted), STAGE / "julia")
    shutil.rmtree(extract_root)


def host_julia() -> Path:
    bundled = ROOT / "build" / "macos" / "julia" / "bin" / "julia"
    executable = bundled if bundled.is_file() else shutil.which("julia")
    if not executable:
        raise RuntimeError("A host Julia is required to resolve Windows package artifacts")
    return Path(executable)


def build_julia_depot() -> None:
    depot = BUILD / "julia_depot"
    manifest = ROOT / "psid_graph_viewer" / "julia_backend" / "Manifest.toml"
    project = manifest.with_name("Project.toml")
    fingerprint = hashlib.sha256(project.read_bytes() + manifest.read_bytes()).hexdigest()
    marker = depot / ".windows-ready"
    if not marker.is_file() or marker.read_text(encoding="utf-8") != fingerprint:
        shutil.rmtree(depot, ignore_errors=True)
        depot.mkdir(parents=True)
        environment = os.environ.copy()
        for key in tuple(environment):
            if key.startswith("JULIA_"):
                environment.pop(key)
        environment.update(
            JULIA_DEPOT_PATH=str(depot),
            JULIA_LOAD_PATH="@:@stdlib",
            JULIA_CPU_TARGET="generic",
        )
        expression = (
            'using Pkg; platform = Base.BinaryPlatforms.Platform("x86_64", "windows"); '
            "Pkg.instantiate(; platform, verbose=true, allow_build=false, "
            "allow_autoprecomp=false)"
        )
        run(
            str(host_julia()),
            f"--project={project.parent}",
            "--startup-file=no",
            "-e",
            expression,
            env=environment,
        )
        for disposable in ("compiled", "logs", "registries"):
            shutil.rmtree(depot / disposable, ignore_errors=True)
        marker.write_text(fingerprint, encoding="utf-8")
    shutil.copytree(depot, STAGE / "julia_depot")


def create_icon() -> None:
    from PySide6 import QtGui

    image = QtGui.QImage(str(ROOT / "Logo.png"))
    if image.isNull() or not image.save(str(STAGE / "PSD-GUI.ico"), "ICO"):
        raise RuntimeError("Unable to create the Windows application icon")


def validate_stage() -> None:
    required = (
        STAGE / "python" / "pythonw.exe",
        STAGE / "python" / "Lib" / "site-packages" / "PySide6" / "QtCore.pyd",
        STAGE / "julia" / "bin" / "julia.exe",
        STAGE / "julia_depot",
        STAGE / "psid_graph_viewer" / "julia_backend" / "psid_backend.jl",
        STAGE / "PowerSimulationsDynamics.jl" / "Project.toml",
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise RuntimeError("Incomplete Windows stage:\n" + "\n".join(missing))
    for executable in (required[0], required[2]):
        if executable.read_bytes()[:2] != b"MZ":
            raise RuntimeError(f"Not a Windows executable: {executable}")


def create_installer() -> Path:
    makensis = shutil.which("makensis")
    if not makensis:
        raise RuntimeError("Install NSIS (macOS: brew install makensis)")
    output = DIST / "PSD-GUI-Windows-x64-Setup.exe"
    output.unlink(missing_ok=True)
    run(
        makensis,
        f"-DSTAGE={STAGE}",
        f"-DOUTPUT={output}",
        str(ROOT / "tools" / "windows_installer.nsi"),
    )
    return output


def main() -> None:
    DIST.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(STAGE, ignore_errors=True)
    STAGE.mkdir(parents=True)
    python_archive = download(PYTHON_URL, PYTHON_SHA256)
    julia_archive = download(JULIA_URL, JULIA_SHA256)
    copy_application_sources()
    build_python_runtime(python_archive)
    build_julia_runtime(julia_archive)
    build_julia_depot()
    create_icon()
    validate_stage()
    installer = create_installer()
    print(f"Built {installer}")


if __name__ == "__main__":
    main()
