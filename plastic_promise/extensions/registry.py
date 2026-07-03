"""PackRegistry — load and validate pack.yml files, build market index."""

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger("plastic-promise.extensions.registry")

PACK_TYPES = ("knowledge", "workflow", "capability", "adapter")


@dataclass
class PackInfo:
    """Validated pack metadata."""

    name: str
    version: str
    pack_type: str
    min_core_version: str = "0.0.0"
    description: str = ""
    author: str = ""
    path: str = ""
    install_pip: list[str] = field(default_factory=list)
    hooks: dict = field(default_factory=dict)
    tools: dict = field(default_factory=dict)
    replaces: dict = field(default_factory=dict)
    skills: dict = field(default_factory=dict)
    chain: dict = field(default_factory=dict)
    workflow_mode: str = "advisory"
    adapter: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)


class PackRegistry:
    """In-memory index of installed/available packs."""

    REMOTE_INDEX_URL = (
        "https://raw.githubusercontent.com/plastic-promise/market-index/main/market-index.yml"
    )

    def __init__(self, plugins_dir: str = "plugins"):
        self._plugins_dir = Path(plugins_dir)
        self._packs: dict[str, PackInfo] = {}

    def discover(self) -> list[PackInfo]:
        """Scan plugins/ for installed packs, load + validate each."""
        results = []
        if not self._plugins_dir.exists():
            return results
        for pack_dir in self._plugins_dir.iterdir():
            if not pack_dir.is_dir():
                continue
            pack_yml = pack_dir / "pack.yml"
            if not pack_yml.exists():
                continue
            try:
                info = self._load_pack(pack_yml)
                self._packs[info.name] = info
                results.append(info)
            except Exception as e:
                logger.warning("Failed to load pack from %s: %s", pack_dir, e)
        return results

    def get(self, name: str) -> PackInfo | None:
        return self._packs.get(name)

    def list_packs(self, pack_type: str | None = None) -> list[PackInfo]:
        packs = list(self._packs.values())
        if pack_type:
            packs = [p for p in packs if p.pack_type == pack_type]
        return packs

    def fetch_remote_index(self) -> list[dict]:
        """Fetch remote market index. Returns [] on any failure."""
        try:
            import urllib.request

            with urllib.request.urlopen(self.REMOTE_INDEX_URL, timeout=10) as resp:
                data = yaml.safe_load(resp.read())
            return data.get("entries", [])
        except Exception as e:
            logger.debug("Remote index fetch failed: %s", e)
            return []

    def _load_pack(self, path: Path) -> PackInfo:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            raise ValueError(f"pack.yml must be a dict, got {type(data)}")
        name = data.get("name", "")
        if not name:
            raise ValueError("pack.yml missing required field: name")
        version = data.get("version", "0.0.0")
        pack_type = data.get("type", "knowledge")
        if pack_type not in PACK_TYPES:
            raise ValueError(f"Invalid pack type '{pack_type}'. Must be one of: {PACK_TYPES}")
        return PackInfo(
            name=name,
            version=version,
            pack_type=pack_type,
            min_core_version=data.get("min_core_version", "0.0.0"),
            description=data.get("description", ""),
            author=data.get("author", ""),
            path=str(path.parent),
            install_pip=data.get("install", {}).get("pip", [])
            if isinstance(data.get("install"), dict)
            else [],
            hooks=data.get("hooks", {}),
            tools=data.get("tools", {}),
            replaces=data.get("replaces", {}),
            skills=data.get("skills", {}),
            chain=data.get("chain", {}),
            workflow_mode=data.get("workflow_mode", "advisory"),
            adapter=data.get("adapter", {}),
            raw=data,
        )
