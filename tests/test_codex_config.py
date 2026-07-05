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
    assert fallback["command"] == ".venv\\Scripts\\python.exe"
    assert fallback["args"] == ["-m", "plastic_promise"]
    assert fallback["env"]["PYTHONIOENCODING"] == "utf-8"
