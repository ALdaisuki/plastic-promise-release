import shutil
from pathlib import Path

import tomllib


def test_project_codex_config_uses_streamable_http_by_default():
    config_path = Path(".codex/config.toml")
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))

    plastic = data["mcp_servers"]["plastic_promise"]
    assert plastic["url"] == "http://127.0.0.1:9020/mcp"
    assert "command" not in plastic
    assert "args" not in plastic


def test_project_codex_config_keeps_stdio_fallback_profile():
    config_path = Path(".codex/config.toml")
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))

    fallback = data["profiles"]["stdio-fallback"]["mcp_servers"]["plastic_promise"]
    command = Path(fallback["command"])
    resolved = command if command.is_absolute() else config_path.parent.parent / command
    assert resolved.exists() or shutil.which(fallback["command"])
    assert fallback["args"] == ["-m", "plastic_promise"]
    assert fallback["env"]["PYTHONIOENCODING"] == "utf-8"


def test_codex_config_preflight_accepts_current_project_config():
    from plastic_promise.launcher.codex_config import check_project_codex_mcp_config

    ok, messages = check_project_codex_mcp_config(Path.cwd())

    assert ok is True
    assert any("Codex MCP config" in message and "[OK]" in message for message in messages)


def test_codex_config_preflight_rejects_missing_stdio_fallback(tmp_path):
    from plastic_promise.launcher.codex_config import check_project_codex_mcp_config

    config_dir = tmp_path / ".codex"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        """
[mcp_servers.plastic_promise]
url = "http://127.0.0.1:9020/mcp"

[profiles.stdio-fallback.mcp_servers.plastic_promise]
command = ".venv\\\\Scripts\\\\python.exe"
args = ["-m", "plastic_promise"]
""".strip(),
        encoding="utf-8",
    )

    ok, messages = check_project_codex_mcp_config(tmp_path)

    assert ok is False
    assert any("stdio fallback command not found" in message for message in messages)
