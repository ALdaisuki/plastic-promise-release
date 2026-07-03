"""CLI integration test for neko-adapter.py."""

import subprocess
import sys
import os


def test_cli_help():
    """--help prints usage and exits cleanly."""
    script = os.path.join(
        os.path.dirname(__file__), "..", "plastic_promise", "core", "neko_adapter.py"
    )
    result = subprocess.run(
        [sys.executable, script, "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert "--bus-url" in result.stdout
    assert "--session" in result.stdout


def test_cli_defaults_smoke():
    """Adapter with --dry-run flag prints config and exits."""
    script = os.path.join(
        os.path.dirname(__file__), "..", "plastic_promise", "core", "neko_adapter.py"
    )
    result = subprocess.run(
        [sys.executable, script, "--dry-run"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert "session=" in result.stdout
    assert "bus_url=" in result.stdout
