"""Release import smoke tests for the PyO3 context_engine_core extension."""

import importlib.machinery
import importlib.util
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

MODULE_NAME = "context_engine_core"
_MISSING_MODULE = object()


def _cargo_build_environment() -> dict[str, str]:
    """Keep direct macOS Cargo smoke builds compatible with PyO3 extensions."""

    environment = os.environ.copy()
    if sys.platform == "darwin":
        dynamic_lookup = "-C link-arg=-undefined -C link-arg=dynamic_lookup"
        existing = environment.get("RUSTFLAGS", "").strip()
        if dynamic_lookup not in existing:
            environment["RUSTFLAGS"] = " ".join(
                value for value in (existing, dynamic_lookup) if value
            )
    return environment


def _import_from_temp_artifact(importable):
    previous_module = sys.modules.get(MODULE_NAME, _MISSING_MODULE)
    try:
        spec = importlib.util.spec_from_file_location(MODULE_NAME, importable)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[MODULE_NAME] = module
        spec.loader.exec_module(module)
        assert Path(module.__file__).resolve() == importable.resolve()
        return module
    finally:
        if previous_module is _MISSING_MODULE:
            sys.modules.pop(MODULE_NAME, None)
        else:
            sys.modules[MODULE_NAME] = previous_module


def test_import_from_temp_artifact_restores_existing_module(tmp_path, monkeypatch):
    previous_module = object()
    module_path = tmp_path / "context_engine_core.py"
    module_path.write_text("MARKER = 'temp-artifact'\n", encoding="utf-8")

    monkeypatch.setitem(sys.modules, MODULE_NAME, previous_module)

    module = _import_from_temp_artifact(module_path)

    assert module.MARKER == "temp-artifact"
    assert Path(module.__file__).resolve() == module_path.resolve()
    assert sys.modules[MODULE_NAME] is previous_module


def test_release_context_engine_core_import_contract(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    crate_dir = repo / "rust" / "context-engine-core"
    cargo = shutil.which("cargo")
    assert cargo is not None, "cargo is required for the fail-closed release build smoke"
    maturin = shutil.which("maturin")
    assert maturin is not None, "maturin is required for the fail-closed release build smoke"
    wheel_dir = tmp_path / "wheel"

    subprocess.run(
        [
            maturin,
            "build",
            "--release",
            "--manifest-path",
            str(crate_dir / "Cargo.toml"),
            "--interpreter",
            sys.executable,
            "--out",
            str(wheel_dir),
        ],
        cwd=repo,
        env=_cargo_build_environment(),
        check=True,
    )

    extension_suffix = importlib.machinery.EXTENSION_SUFFIXES[0]
    importable = tmp_path / f"context_engine_core{extension_suffix}"
    wheels = list(wheel_dir.glob("*.whl"))
    assert len(wheels) == 1, f"expected one Rust wheel, found: {wheels}"
    with zipfile.ZipFile(wheels[0]) as archive:
        extension_members = [
            name
            for name in archive.namelist()
            if Path(name).name.startswith("context_engine_core")
            and Path(name).suffix in {".so", ".pyd"}
        ]
        assert len(extension_members) == 1, (
            f"expected one compiled extension in Rust wheel, found: {extension_members}"
        )
        importable.write_bytes(archive.read(extension_members[0]))

    module = _import_from_temp_artifact(importable)

    engine = module.ContextEngine.new_with_backends(":memory:", ":memory:")
    pack = engine.supply(
        "release import code generation",
        [0.0] * 1024,
        "code_generation",
        "global",
        [],
    )

    assert pack.activated_principles
    assert pack.audit_metadata["principle_injection_count"] == str(len(pack.activated_principles))
