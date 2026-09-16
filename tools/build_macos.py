from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build" / "macos"
DIST = ROOT / "dist"
JULIA_VERSION = "1.12.6"
JULIA_URL = (
    "https://julialang-s3.julialang.org/bin/mac/aarch64/1.12/"
    "julia-1.12.6-macaarch64.tar.gz"
)
JULIA_SHA256 = "277d82fbd2eda99d0963b3e41f3dc979d7486f181399f8430fb637318ccd6a31"


def run(*command: str, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def create_icon() -> Path:
    iconset = BUILD / "PSD-GUI.iconset"
    shutil.rmtree(iconset, ignore_errors=True)
    iconset.mkdir(parents=True)
    source = ROOT / "Logo.png"
    for size in (16, 32, 128, 256, 512):
        run(
            "sips",
            "-z",
            str(size),
            str(size),
            str(source),
            "--out",
            str(iconset / f"icon_{size}x{size}.png"),
        )
        run(
            "sips",
            "-z",
            str(size * 2),
            str(size * 2),
            str(source),
            "--out",
            str(iconset / f"icon_{size}x{size}@2x.png"),
        )
    output = BUILD / "PSD-GUI.icns"
    run("iconutil", "-c", "icns", str(iconset), "-o", str(output))
    return output


def download_julia() -> Path:
    cache = BUILD / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / Path(JULIA_URL).name
    if not archive.is_file():
        print(f"Downloading Julia {JULIA_VERSION} for Apple Silicon…", flush=True)
        urllib.request.urlretrieve(JULIA_URL, archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != JULIA_SHA256:
        raise RuntimeError(f"Julia archive checksum mismatch: {digest}")
    runtime = BUILD / "julia"
    if not (runtime / "bin" / "julia").is_file():
        shutil.rmtree(runtime, ignore_errors=True)
        extract_root = BUILD / "julia-extract"
        shutil.rmtree(extract_root, ignore_errors=True)
        extract_root.mkdir(parents=True)
        with tarfile.open(archive) as package:
            package.extractall(extract_root, filter="data")
        extracted = next(path for path in extract_root.iterdir() if path.is_dir())
        shutil.move(str(extracted), runtime)
        shutil.rmtree(extract_root)
    return runtime


def build_julia_depot(runtime: Path) -> Path:
    depot = BUILD / "julia_depot"
    marker = depot / f".ready-{JULIA_VERSION}"
    if marker.is_file():
        return depot
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
    run(
        str(runtime / "bin" / "julia"),
        f"--project={ROOT / 'psid_graph_viewer' / 'julia_backend'}",
        "--startup-file=no",
        "-e",
        "using Pkg; Pkg.instantiate(); Pkg.precompile()",
        env=environment,
    )
    for disposable in ("registries", "logs"):
        shutil.rmtree(depot / disposable, ignore_errors=True)
    marker.touch()
    return depot


def build_application(runtime: Path, depot: Path) -> Path:
    shutil.rmtree(DIST / "PSD-GUI", ignore_errors=True)
    shutil.rmtree(DIST / "PSD-GUI.app", ignore_errors=True)
    run(
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        str(ROOT / "tools" / "PSD-GUI.spec"),
    )
    app = DIST / "PSD-GUI.app"
    resources = app / "Contents" / "Resources"
    shutil.copytree(runtime, resources / "julia", symlinks=True)
    shutil.copytree(depot, resources / "julia_depot", symlinks=True)
    shutil.copytree(
        ROOT / "PowerSimulationsDynamics.jl",
        resources / "PowerSimulationsDynamics.jl",
        symlinks=True,
    )
    run("codesign", "--force", "--deep", "--sign", "-", str(app))
    return app


def create_dmg(app: Path) -> Path:
    stage = BUILD / "dmg"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    shutil.copytree(app, stage / app.name, symlinks=True)
    (stage / "Applications").symlink_to("/Applications")
    output = DIST / "PSD-GUI-macOS-arm64.dmg"
    output.unlink(missing_ok=True)
    run(
        "hdiutil",
        "create",
        "-volname",
        "PSD-GUI",
        "-srcfolder",
        str(stage),
        "-ov",
        "-format",
        "UDZO",
        str(output),
    )
    return output


def main() -> None:
    if sys.platform != "darwin" or os.uname().machine != "arm64":
        raise SystemExit("This builder requires Apple Silicon macOS.")
    try:
        import PyInstaller  # noqa: F401
    except ImportError as error:
        raise SystemExit("Install the build dependency with: pip install pyinstaller") from error
    BUILD.mkdir(parents=True, exist_ok=True)
    DIST.mkdir(parents=True, exist_ok=True)
    create_icon()
    runtime = download_julia()
    depot = build_julia_depot(runtime)
    app = build_application(runtime, depot)
    dmg = create_dmg(app)
    print(f"Built {dmg}")


if __name__ == "__main__":
    main()
