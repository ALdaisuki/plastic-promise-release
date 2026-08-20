"""Deterministic role-package materialisation for production images.

The development repository is one monorepo, but production images are not
allowed to install that monorepo wholesale.  ``RolePackageCompiler`` is the
single seam between the endpoint-role contract and Docker/OCI recipes: it
materialises one closed source allowlist, emits a role-specific distribution
metadata file, and returns a content digest that can be attached to build
evidence.  It never starts Docker or performs deployment work.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from plastic_promise.endpoint_roles import (
    PP_COMPUTE_NODE,
    PP_LOCAL_EDGE,
    PP_SERVER_BACKEND,
    EndpointRoleContract,
    endpoint_role_contract,
)

ROLE_PACKAGE_SCHEMA_VERSION = "plastic-promise-role-package/v1"
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")


class RolePackageError(ValueError):
    """Stable, non-secret materialisation failure."""


@dataclass(frozen=True)
class RolePackageMaterialization:
    role: str
    version: str
    output_root: str
    source_paths: tuple[str, ...]
    package_digest: str
    receipt_path: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": ROLE_PACKAGE_SCHEMA_VERSION,
            "role": self.role,
            "version": self.version,
            "source_paths": list(self.source_paths),
            "package_digest": self.package_digest,
        }


class RolePackageCompiler:
    """Compile a deep, deterministic role package from one repository tree."""

    def __init__(self, source_root: str | Path) -> None:
        self.source_root = Path(source_root).resolve()
        if not self.source_root.is_dir():
            raise RolePackageError("role_package_source_root_invalid")

    def materialize(
        self,
        role: str,
        output_root: str | Path,
        version: str,
    ) -> RolePackageMaterialization:
        contract = self._contract(role)
        if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
            raise RolePackageError("role_package_version_invalid")
        destination = Path(output_root).resolve()
        if destination == self.source_root or self.source_root in destination.parents:
            raise RolePackageError("role_package_output_inside_source")
        if destination.exists():
            if not destination.is_dir() or any(destination.iterdir()):
                raise RolePackageError("role_package_output_not_empty")
        else:
            destination.mkdir(parents=True)

        selected = self._select_paths(contract)
        for relative in selected:
            source = self.source_root / relative
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

        if contract.package_kind == "python":
            self._write_python_metadata(
                destination,
                contract,
                version,
                self.source_root,
                selected,
            )
        elif role == PP_LOCAL_EDGE:
            # Static recipes use the same compiler for inventory purposes but
            # do not need Python distribution metadata.
            (destination / "role-package.json").write_text(
                json.dumps(
                    {
                        "schema_version": ROLE_PACKAGE_SCHEMA_VERSION,
                        "role": role,
                        "version": version,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )

        digest = _directory_digest(destination)
        receipt = RolePackageMaterialization(
            role=role,
            version=version,
            output_root=str(destination),
            source_paths=tuple(selected),
            package_digest=digest,
            receipt_path=str(destination / "role-package.receipt.json"),
        )
        (destination / "role-package.receipt.json").write_text(
            json.dumps(receipt.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )
        return receipt

    def source_paths_for(self, role: str) -> tuple[str, ...]:
        """Return the exact selected source inventory without writing files."""

        return tuple(self._select_paths(self._contract(role)))

    def package_digest_for(self, role: str, version: str) -> str:
        """Recompute one role package digest without exposing a reusable output."""

        with tempfile.TemporaryDirectory(prefix="plastic-promise-role-package-") as directory:
            return self.materialize(role, Path(directory) / "package", version).package_digest

    @staticmethod
    def _contract(role: str) -> EndpointRoleContract:
        try:
            return endpoint_role_contract(role)
        except Exception as exc:
            raise RolePackageError("role_package_role_invalid") from exc

    def _select_paths(self, contract: EndpointRoleContract) -> list[str]:
        selected: set[str] = set()
        for root in contract.source_paths:
            relative_root = _safe_relative(root)
            source = self.source_root / relative_root
            if not source.exists() or source.is_symlink():
                raise RolePackageError("role_package_source_path_missing")
            if source.is_file():
                candidates = [relative_root]
            elif source.is_dir():
                candidates = [
                    _as_posix(path.relative_to(self.source_root))
                    for path in source.rglob("*")
                    if path.is_file() and not path.is_symlink()
                ]
            else:
                raise RolePackageError("role_package_source_path_invalid")
            for candidate in candidates:
                if candidate.endswith(".pyc") or "/__pycache__/" in f"/{candidate}/":
                    continue
                if contract.includes_source_path(candidate):
                    selected.add(candidate)
        if contract.package_kind == "python" and "plastic_promise/__init__.py" not in selected:
            raise RolePackageError("role_package_import_foundation_missing")
        if not selected:
            raise RolePackageError("role_package_allowlist_empty")
        return sorted(selected)

    @staticmethod
    def _write_python_metadata(
        destination: Path,
        contract: EndpointRoleContract,
        version: str,
        source_root: Path,
        selected: list[str],
    ) -> None:
        dependency_block = "\n".join(f'    "{item}",' for item in contract.package_dependencies)
        script_block = "\n".join(
            f'"{name}" = "{target}"' for name, target in contract.package_scripts
        )
        package_data = RolePackageCompiler._package_data_for(source_root, selected)
        package_data_block = "\n".join(
            f'"{package}" = [{", ".join(json.dumps(pattern) for pattern in patterns)}]'
            for package, patterns in package_data.items()
        )
        metadata = f'''[build-system]
requires = ["setuptools>=68.0", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "{contract.distribution_name}"
version = "{version}"
description = "Plastic Promise {contract.role} role package"
readme = "README.md"
requires-python = ">=3.10"
dependencies = [
{dependency_block}
]

[project.scripts]
{script_block}

[tool.setuptools.packages.find]
include = ["plastic_promise*"]
exclude = ["plastic_promise.tests*"]

[tool.setuptools.package-data]
{package_data_block}
'''
        (destination / "pyproject.toml").write_text(metadata, encoding="utf-8")
        readme = source_root / "README.md"
        if not (destination / "README.md").exists() and readme.is_file():
            shutil.copy2(readme, destination / "README.md")
        license_path = source_root / "LICENSE"
        if license_path.is_file() and not (destination / "LICENSE").exists():
            shutil.copy2(license_path, destination / "LICENSE")

    @staticmethod
    def _package_data_for(source_root: Path, selected: list[str]) -> dict[str, tuple[str, ...]]:
        """Return deterministic setuptools package-data entries for non-Python files."""

        package_data: dict[str, set[str]] = {}
        for relative in selected:
            path = PurePosixPath(relative)
            if path.suffix in {".py", ".pyi", ".pyc"} or path.parts[0] != "plastic_promise":
                continue
            package_root: PurePosixPath | None = None
            for length in range(len(path.parts) - 1, 0, -1):
                candidate = PurePosixPath(*path.parts[:length])
                if (source_root / candidate / "__init__.py").is_file():
                    package_root = candidate
                    break
            if package_root is None:
                continue
            package = ".".join(package_root.parts)
            pattern = PurePosixPath(*path.parts[len(package_root.parts) :]).as_posix()
            package_data.setdefault(package, set()).add(pattern)
        return {
            package: tuple(sorted(patterns)) for package, patterns in sorted(package_data.items())
        }


def _safe_relative(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise RolePackageError("role_package_source_path_unsafe")
    return _as_posix(path)


def _as_posix(path: Path | PurePosixPath) -> str:
    return PurePosixPath(path.as_posix()).as_posix()


def _directory_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = _as_posix(path.relative_to(root))
        if relative == "role-package.receipt.json":
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--role", required=True, choices=(PP_LOCAL_EDGE, PP_SERVER_BACKEND, PP_COMPUTE_NODE)
    )
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args(argv)
    receipt = RolePackageCompiler(args.source_root).materialize(
        args.role, args.output_root, args.version
    )
    print(json.dumps(receipt.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ROLE_PACKAGE_SCHEMA_VERSION",
    "RolePackageCompiler",
    "RolePackageError",
    "RolePackageMaterialization",
]
