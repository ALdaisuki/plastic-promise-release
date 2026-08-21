"""Unit tests for the compute-node handshake helper functions (stdlib only)."""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "compute_node_handshake.py"
_spec = importlib.util.spec_from_file_location("compute_node_handshake", _SCRIPT)
hs = importlib.util.module_from_spec(_spec)
assert _spec is not None and _spec.loader is not None
_spec.loader.exec_module(hs)


class ParseLocalPortTest(unittest.TestCase):
    def test_extracts_loopback_port(self) -> None:
        self.assertEqual(hs.parse_local_port("http://127.0.0.1:29130"), 29130)

    def test_rejects_missing_port(self) -> None:
        with self.assertRaises(ValueError):
            hs.parse_local_port("http://127.0.0.1")


class SshLocalForwardTest(unittest.TestCase):
    def test_matches_prefix(self) -> None:
        self.assertTrue(hs.is_ssh_local_forward("ssh-local-forward-29130"))

    def test_rejects_other_transport(self) -> None:
        self.assertFalse(hs.is_ssh_local_forward("direct-lan"))


class BuildSshCommandTest(unittest.TestCase):
    def test_binds_local_to_remote(self) -> None:
        cmd = hs.build_ssh_command(
            host="192.168.5.6",
            user="plastic",
            key=Path("/tmp/k"),
            local_port=29130,
            remote_port=8080,
        )
        self.assertIn("-L", cmd)
        self.assertIn("127.0.0.1:29130:127.0.0.1:8080", cmd)
        self.assertIn("plastic@192.168.5.6", cmd)
        self.assertIn("ExitOnForwardFailure=yes", cmd)


class LoadNodesTest(unittest.TestCase):
    def _write(self, text: str) -> Path:
        fd, name = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        return Path(name)

    def test_load_and_select(self) -> None:
        payload = '{"nodes": [{"node_id": "n1", "transport_id": "ssh-local-forward-29130", "base_url": "http://127.0.0.1:29130"}]}'
        path = self._write(payload)
        try:
            node = hs.select_node(hs.load_nodes(path), None)
            self.assertEqual(node["node_id"], "n1")
        finally:
            os.unlink(path)

    def test_unknown_id_fails(self) -> None:
        path = self._write('{"nodes": [{"node_id": "n1"}]}')
        try:
            with self.assertRaises(ValueError):
                hs.select_node(hs.load_nodes(path), "missing")
        finally:
            os.unlink(path)


class BearerTokenTest(unittest.TestCase):
    def test_env_precedence(self) -> None:
        os.environ["PP_TEST_NODE_TOKEN"] = "abc123"
        try:
            token, source = hs.bearer_token("PP_TEST_NODE_TOKEN", "nonexistent-service")
            self.assertEqual(token, "abc123")
            self.assertEqual(source, "environment")
        finally:
            del os.environ["PP_TEST_NODE_TOKEN"]

    def test_missing_reports_unavailable(self) -> None:
        token, source = hs.bearer_token("PP_DEFINITELY_UNSET_VAR_XYZ", "nonexistent-service")
        self.assertIsNone(token)
        self.assertIn(source, ["keychain_error", "unavailable"])


class OnboardHelpersTest(unittest.TestCase):
    def test_launchd_mode_omits_fork(self) -> None:
        base = hs.build_ssh_command(
            host="h",
            user="u",
            key=Path("/tmp/k"),
            local_port=1,
            remote_port=2,
        )
        daemon = hs.build_ssh_command(
            host="h",
            user="u",
            key=Path("/tmp/k"),
            local_port=1,
            remote_port=2,
            include_fork=False,
        )
        self.assertIn("-f", base)
        self.assertNotIn("-f", daemon)
        self.assertIn("127.0.0.1:1:127.0.0.1:2", daemon)

    def test_tunnel_plist_is_persistent(self) -> None:
        plist = hs.build_tunnel_plist(
            label="org.plastic-promise.node-tunnel",
            ssh_args=["/usr/bin/ssh", "-N"],
            log_path=Path("/tmp/a.log"),
            err_path=Path("/tmp/b.log"),
        )
        self.assertTrue(plist["KeepAlive"])
        self.assertTrue(plist["RunAtLoad"])
        self.assertEqual(plist["Label"], "org.plastic-promise.node-tunnel")
        self.assertEqual(plist["ProgramArguments"], ["/usr/bin/ssh", "-N"])


class ResolveEndpointsPathTest(unittest.TestCase):
    def test_explicit_wins(self) -> None:
        resolved = hs.resolve_endpoints_path("/tmp/e.json", "/tmp/other.json")
        self.assertEqual(resolved, Path("/tmp/e.json"))

    def test_env_second(self) -> None:
        resolved = hs.resolve_endpoints_path(None, "/tmp/env.json")
        self.assertEqual(resolved, Path("/tmp/env.json"))

    def test_canonical_default_when_present(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            target = home / ".local/share/plastic-promise/mac-server"
            target.mkdir(parents=True)
            (target / "private-node-endpoints.json").write_text("{}", encoding="utf-8")
            resolved = hs.resolve_endpoints_path(None, None, home=home)
            self.assertEqual(resolved, target / "private-node-endpoints.json")

    def test_none_when_missing(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(hs.resolve_endpoints_path(None, None, home=Path(tmp)))


class ProbeHeaderTest(unittest.TestCase):
    def test_bearer_prefix_idempotent(self) -> None:
        captured = {}

        class FakeResponse:
            status = 200

            def read(self, n=-1):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(request, timeout=None):
            captured["headers"] = dict(request.header_items())
            return FakeResponse()

        original = hs.urllib.request.urlopen
        hs.urllib.request.urlopen = fake_urlopen
        try:
            hs.probe_health("http://127.0.0.1:1", "Bearer abc", 2)
            once = captured["headers"].get("Authorization")
            hs.probe_health("http://127.0.0.1:1", "abc", 2)
            twice = captured["headers"].get("Authorization")
        finally:
            hs.urllib.request.urlopen = original
        self.assertEqual(once, "Bearer abc")
        self.assertEqual(twice, "Bearer abc")


if __name__ == "__main__":
    unittest.main()
