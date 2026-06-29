"""Team Protocol E2E — Pi 能自主认领 Issue 并完成生命周期"""
import os, sys, subprocess, shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PI_CMD = shutil.which("pi") or shutil.which("pi.cmd") or "D:\\npm-global\\pi.cmd"


class TestTeamProtocol:
    def test_pi_preserves_existing_code(self):
        """Pi 修改 hello.py 时保留了已有代码（间接验证上下文拉取）"""
        with open("hello.py") as f:
            before = f.read()
        assert "Hello, World!" in before, "前置条件: hello.py 需要已有 GET /"

        subprocess.run([
            PI_CMD, "--print",
            "在 hello.py 加 GET /health -> {status:ok}。保留已有 GET / 不变。",
            "--session-id", "mvp_context",
        ], capture_output=True, text=True, timeout=120)

        with open("hello.py") as f:
            after = f.read()
        assert "Hello, World!" in after, "FAIL: Pi 删除了已有代码"
        assert "/health" in after, "FAIL: Pi 未添加 /health"
        print("PASS: Pi 保留已有代码 + 添加新端点")
