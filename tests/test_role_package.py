from __future__ import annotations

from pathlib import Path

import pytest

from plastic_promise.endpoint_roles import PP_COMPUTE_NODE, PP_SERVER_BACKEND
from plastic_promise.role_package import RolePackageCompiler, RolePackageError

ROOT = Path(__file__).resolve().parents[1]


def test_compute_materialization_is_closed_and_has_no_server_surface(tmp_path: Path):
    receipt = RolePackageCompiler(ROOT).materialize(PP_COMPUTE_NODE, tmp_path / "compute", "0.2.15")
    files = set(receipt.source_paths)

    assert "plastic_promise/__init__.py" in files
    assert "plastic_promise/local_inference_node/support.py" in files
    assert not any(path.startswith("plastic_promise/core/") for path in files)
    assert not any(path.startswith("plastic_promise/mcp/") for path in files)
    assert not any(path.startswith("plastic_promise/collaboration/") for path in files)
    assert (tmp_path / "compute" / "pyproject.toml").is_file()
    assert (tmp_path / "compute" / "role-package.receipt.json").is_file()
    assert (tmp_path / "compute" / "plastic_promise" / "role-package.receipt.json").is_file()
    metadata = (tmp_path / "compute" / "pyproject.toml").read_text(encoding="utf-8")
    assert '"plastic_promise" = ["py.typed", "role-package.receipt.json"]' in metadata


def test_server_materialization_excludes_compute_transport_modules(tmp_path: Path):
    receipt = RolePackageCompiler(ROOT).materialize(
        PP_SERVER_BACKEND, tmp_path / "server", "0.2.15"
    )
    files = set(receipt.source_paths)

    assert "plastic_promise/collaboration/event_log.py" in files
    assert "plastic_promise/core/provider_http.py" not in files
    assert "plastic_promise/local_inference_node/app.py" not in files
    assert "plastic_promise/release_builder/cli.py" not in files
    metadata = (tmp_path / "server" / "pyproject.toml").read_text(encoding="utf-8")
    assert '"plastic_promise" = ["py.typed", "role-package.receipt.json"]' in metadata
    assert (
        '"plastic_promise.mcp.dashboard_v2" = ["static/app.css", "static/app.js", '
        '"static/index.html"]'
    ) in metadata


def test_materializer_rejects_non_empty_output(tmp_path: Path):
    output = tmp_path / "compute"
    output.mkdir()
    (output / "unexpected").write_text("x", encoding="utf-8")

    with pytest.raises(RolePackageError, match="role_package_output_not_empty"):
        RolePackageCompiler(ROOT).materialize(PP_COMPUTE_NODE, output, "0.2.15")


def test_materializer_rejects_output_inside_source(tmp_path: Path):
    with pytest.raises(RolePackageError, match="role_package_output_inside_source"):
        RolePackageCompiler(ROOT).materialize(PP_COMPUTE_NODE, ROOT / "build", "0.2.15")
