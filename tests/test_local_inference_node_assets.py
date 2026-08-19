from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
NODE_ROOT = REPO_ROOT / "plastic_promise" / "local_inference_node"
ASSET_ROOT = REPO_ROOT / "deploy" / "local-inference-node"


def test_node_source_has_no_canonical_state_or_control_plane_imports():
    source = "\n".join(path.read_text(encoding="utf-8") for path in NODE_ROOT.glob("*.py"))

    for forbidden in (
        "import sqlite3",
        "import lancedb",
        "plastic_promise.mcp",
        "plastic_promise.core.store",
        "plastic_promise.storage",
        "plastic_memory.db",
    ):
        assert forbidden not in source


def test_compose_and_docker_assets_keep_models_and_listener_private():
    compose = (ASSET_ROOT / "compose.yaml").read_text(encoding="utf-8")
    cpu_compose = (ASSET_ROOT / "compose.cpu.yaml").read_text(encoding="utf-8")
    cuda_compose = (ASSET_ROOT / "compose.cuda.yaml").read_text(encoding="utf-8")
    dockerfile = (ASSET_ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")

    assert "network_mode: host" in compose
    assert "ports:" not in compose
    assert "read_only: true" in compose
    assert "read_only: true" in compose.split("target: /models", 1)[0]
    assert 'PP_LOCAL_NODE_BIND_HOST: "127.0.0.1"' in compose
    assert "/models" in compose
    assert "sqlite" not in compose.casefold()
    assert "USER ppnode" in dockerfile
    assert "EXPOSE" not in dockerfile
    assert "# syntax=docker/dockerfile:1.7" in dockerfile
    assert "--mount=type=cache,target=/root/.cache/pip,sharing=locked" in dockerfile
    assert "--retries 10" in dockerfile
    assert "--timeout 60" in dockerfile
    assert "--no-cache-dir" not in dockerfile
    assert "python3-venv" in dockerfile
    assert "python3-dev" in dockerfile
    assert "gcc g++" in dockerfile
    assert "python3 -m venv /opt/plastic-promise-venv" in dockerfile
    assert "/opt/plastic-promise-venv/bin/python -m pip install" in dockerfile
    assert "--break-system-packages" not in dockerfile
    for compute_compose in (compose, cpu_compose, cuda_compose):
        assert "/tmp:rw,exec,nosuid,size=512m" in compute_compose
        assert "/tmp:rw,noexec,nosuid,size=512m" not in compute_compose
    for excluded in ("*.db", "*.key", ".env", "models", "uv.lock"):
        assert excluded in dockerignore


def test_tunnel_example_is_loopback_only_and_has_no_real_host_or_key_material():
    tunnel = (ASSET_ROOT / "pp-local-inference-tunnel.service.example").read_text(encoding="utf-8")
    example = (ASSET_ROOT / "local-inference-tunnel.env.example").read_text(encoding="utf-8")
    cache_service = (ASSET_ROOT / "pp-local-inference-cache-plan.service.example").read_text(
        encoding="utf-8"
    )
    cache_timer = (ASSET_ROOT / "pp-local-inference-cache-plan.timer.example").read_text(
        encoding="utf-8"
    )

    assert "ClearAllForwardings=yes" in tunnel
    assert "ExitOnForwardFailure=yes" in tunnel
    assert "ForwardAgent=no" in tunnel
    assert "RequestTTY=no" in tunnel
    assert "-R 127.0.0.1:${PP_TUNNEL_SERVER_PORT}:127.0.0.1:${PP_LOCAL_NODE_PORT}" in tunnel
    assert "server-host" in example
    assert "192.168." not in tunnel + example
    assert "PRIVATE KEY" not in tunnel + example
    assert "OnCalendar=*-*-* 04:30:00" in cache_timer
    assert "Persistent=true" in cache_timer
    assert "plastic-promise-local-inference-cache-plan" in cache_service
    assert "sqlite" not in cache_service.casefold()


def test_windows_wsl_build_scripts_keep_cleanup_and_gpu_smoke_bounded():
    unix_build = (REPO_ROOT / "scripts" / "run_local_inference_node_build.sh").read_text(
        encoding="utf-8"
    )
    windows_build = (REPO_ROOT / "scripts" / "run_windows_local_inference_build.ps1").read_text(
        encoding="utf-8"
    )
    rendered_windows_build = windows_build.replace("`", "")
    example = (ASSET_ROOT / "windows-wsl-build.env.example").read_text(encoding="utf-8")

    assert "prepare_oci_build.py" in unix_build
    assert "--execute" in unix_build
    assert "plastic-promise-local" in unix_build
    assert "docker buildx build" in unix_build
    assert "--gpus all" in unix_build
    assert "org.opencontainers.image.revision" in unix_build
    assert "docker system prune" not in unix_build
    assert "docker push" not in unix_build
    assert "--credential-mode" in unix_build
    assert "--windows-source-root" in unix_build
    assert "resource-gate" in unix_build
    assert "headless-builder" in unix_build
    assert "validate-windows-source" in windows_build
    assert "deferred_resource_busy" in windows_build
    assert "wsl.exe" in windows_build
    assert "native-docker" in windows_build
    assert "scripts/prepare_oci_build.py" in windows_build
    assert "[string]$DockerCommand = 'docker.exe'" in windows_build
    assert "function Invoke-PpDocker" in windows_build
    assert "forcing ExecutionMode=wsl" in windows_build
    assert "RecreateDedicatedBuilder" in windows_build
    assert "docker-config" in windows_build
    assert "auths" in windows_build
    assert "[Guid]::NewGuid().ToString('N')" in windows_build
    assert "Remove-Item -LiteralPath $dockerConfigDirectory -Recurse -Force" in windows_build
    assert '"https://index.docker.io/v1/":{}' in rendered_windows_build
    assert "Invoke-DockerPullWithRetry" in windows_build
    assert "Invoke-DockerBuildWithRetry" in windows_build
    assert "DockerHubMirror" in windows_build
    assert "mirror.gcr.io" in windows_build
    assert "--buildkitd-config" in windows_build
    assert "Invoke-PpDocker -Arguments @('image', 'inspect', $Image)" in windows_build
    assert (
        "moby/buildkit@sha256:2f5adac4ecd194d9f8c10b7b5d7bceb5186853db1b26e5abd3a657af0b7e26ec"
    ) in windows_build
    assert "plastic-promise-buildx" in windows_build
    assert "$env:BUILDX_CONFIG = $BuildxConfigDirectory" in windows_build
    assert "$cleanupArguments += @('--docker-config', $dockerConfigDirectory)" in windows_build
    assert "CredentialMode" in windows_build
    assert "D:\\" not in example
    assert "plastic-promise-windows-local" not in unix_build + windows_build + example
    assert "PP_WINDOWS_BUILD_" in example
    assert "TOKEN" not in example

    one_click_unix = (REPO_ROOT / "scripts" / "build_compute_node.sh").read_text(encoding="utf-8")
    one_click_windows = (REPO_ROOT / "scripts" / "build_compute_node.ps1").read_text(
        encoding="utf-8"
    )
    smoke = (REPO_ROOT / "scripts" / "pp_node_smoke.py").read_text(encoding="utf-8")
    posix_example = (ASSET_ROOT / "compute-node.env.example").read_text(encoding="utf-8")

    for source in (one_click_unix, one_click_windows):
        assert "plastic-promise-local" in source
        assert "PP_LOCAL_NODE_EMBEDDING_REVISION" in source
        assert "PP_LOCAL_NODE_RERANK_REVISION" in source
        assert "PP_LOCAL_NODE_MODEL_DIRECTORY" in source
        assert "container-build-identity-" in source
        assert "--no-build" in source
        assert "PP_BUILD_POLICY_DIGEST" in source
        assert "PP_RECIPE_POLICY_DIGEST" in source
        assert "22e683669bc0f0bd69640a1354a6d0aebcfeede5" not in source
        assert "D:\\" not in source
        assert "C:\\Users\\" not in source
    assert "'-RepositoryRoot', $root" in one_click_windows
    assert "${basePrefix}_BASE_IMAGE" in one_click_windows
    assert "Invoke-PpDocker -Arguments @('tag'" in one_click_windows
    assert "Repair-ImageTritonDeps" in one_click_windows
    assert "python3-dev gcc g++" in one_click_windows
    assert "up -d --no-build" in one_click_windows
    assert "docker tag" in one_click_unix
    assert "prefix}_BASE_IMAGE" in one_click_unix
    assert "[string]$RepositoryRoot = ''" in windows_build.replace('"', "")
    assert "$RepositoryRoot = Split-Path -Parent $PSScriptRoot" in windows_build
    assert "plastic-promise/local-inference-node-smoke/v1" in smoke
    assert "plastic-promise/local-inference-runtime-status/v1" in smoke
    assert "--node-config" in smoke
    assert "median_latency_ms" in smoke
    assert "PP_LOCAL_NODE_EMBEDDING_ARTIFACT_SHA256" in smoke
    assert "PP_LOCAL_NODE_RERANK_ARTIFACT_SHA256" in smoke
    assert "D:\\" not in posix_example


def test_windows_compute_node_preflight_is_generic_and_persisted():
    preflight = (REPO_ROOT / "scripts" / "preflight_windows_node_host.ps1").read_text(
        encoding="utf-8"
    )
    setup = (REPO_ROOT / "scripts" / "setup_windows_compute_node.ps1").read_text(encoding="utf-8")
    one_click_windows = (REPO_ROOT / "scripts" / "build_compute_node.ps1").read_text(
        encoding="utf-8"
    )
    profile = (ASSET_ROOT / "windows-compute-node.env.example").read_text(encoding="utf-8")

    assert "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Lxss" in preflight
    assert "$env:SystemDrive" in preflight
    assert "wsl.exe --manage $WslDistro --move $targetPath" in preflight
    assert "PP_WSL_MEMORY" in preflight
    assert "PP_WSL_PROCESSORS" in preflight
    assert "PP_WSL_SWAP" in preflight
    assert "docker.service" in preflight
    assert "'/etc/wsl.conf'" in preflight
    assert "[boot] systemd=true" in preflight
    assert "[switch]$EnableDockerBridge" in preflight
    assert "[switch]$SkipDockerBridge" not in preflight
    assert "/etc/profile.d/pp-proxy.sh" in preflight
    assert "/etc/systemd/system/docker.service.d/pp-proxy.conf" in preflight
    assert 'docker_command = "wsl.exe -d $WslDistro -e docker"' in preflight
    assert "$report.ready = ($blockingReasons.Count -eq 0)" in preflight
    assert "exit 1" in preflight

    assert "'preflight', 'ollama', 'models', 'build', 'env', 'verify'" in setup
    assert "Resolve-PpDockerCommand" in setup
    assert "Resolve-PpProxyUrl" in setup
    assert "docker command after preflight" in setup
    assert "'-ProfilePath', $ProfilePath" in setup
    assert "Test-PpRerankTreeComplete" in setup
    assert "Invoke-PpDocker -Arguments @('compose'" in setup
    assert "running node smoke inside WSL" in setup
    assert "configure_windows_compute_env.ps1" in setup
    assert "PP_LOCAL_NODE_EMBEDDING_ARTIFACT_SHA256" in setup
    assert "PP_LOCAL_NODE_RERANK_ARTIFACT_SHA256" in setup
    assert "Protect-PpPrivateFile -Path $composeEnv" in setup
    assert "if ($Stage -eq 'verify')" in setup
    assert "if ($Stage -in @('all', 'verify'))" not in setup

    no_start = setup.index("'-NoStart'")
    alias = one_click_windows.index("Invoke-PpDocker -Arguments @('tag'")
    no_start_exit = one_click_windows.index("if ($NoStart)")
    assert no_start >= 0
    assert alias < no_start_exit
    assert "compose.cuda.yaml" in one_click_windows
    assert "PP_LOCAL_NODE_MODEL_DIRECTORY=$composeModelDirectory" in one_click_windows
    assert "$proxyUri.UserInfo" in one_click_windows
    assert "build_compute_node_proxy_url_invalid" in one_click_windows
    assert one_click_windows.index("$proxyUri.UserInfo") < one_click_windows.index(
        "'-ProxyUrl', $ProxyUrl"
    )
    assert one_click_windows.index("$proxyUri.UserInfo") < one_click_windows.index(
        '"HTTP_PROXY=$ProxyUrl"'
    )

    for key in (
        "PP_DOCKER_COMMAND",
        "PP_WSL_DISTRO",
        "PP_WSL_VHDX_TARGET",
        "PP_PROXY_URL",
        "PP_WSL_MEMORY",
        "PP_WSL_PROCESSORS",
        "PP_WSL_SWAP",
    ):
        assert key in profile
    assert "TOKEN" not in profile
    assert "PRIVATE KEY" not in profile


def test_external_llama_cpp_workers_require_immutable_image_identity():
    workers = (REPO_ROOT / "scripts" / "start_llama_cpp_compute_workers.sh").read_text(
        encoding="utf-8"
    )

    assert 'image="${PP_LLAMA_CPP_IMAGE:-}"' in workers
    assert "@sha256:[0-9a-f]{64}" in workers
    assert "llama.cpp:server-cuda" not in workers
    assert 'embedding_normalization="${PP_LOCAL_NODE_EMBEDDING_NORMALIZATION:-l2}"' in workers
    assert "l2) embedding_normalize=2" in workers
    assert "none) embedding_normalize=-1" in workers
    assert '--embd-normalize "$embedding_normalize"' in workers
    assert "--stop" in workers
    assert "--status" in workers
    assert "PP_LLAMA_CPP_RESOURCE_GATE" in workers
    assert "resource_probe resource-gate" in workers
    assert "explicit_operator_override=1" in workers


def test_windows_compute_environment_binds_artifacts_and_private_acl():
    configure = (REPO_ROOT / "scripts" / "configure_windows_compute_env.ps1").read_text(
        encoding="utf-8"
    )
    verify = (REPO_ROOT / "scripts" / "verify_windows_compute_node.ps1").read_text(encoding="utf-8")

    for key in (
        "PP_LOCAL_NODE_EMBEDDING_ARTIFACT_SHA256",
        "PP_LOCAL_NODE_RERANK_ARTIFACT_SHA256",
    ):
        assert key in configure
        assert key in verify
    assert "icacls.exe" in configure
    assert "/inheritance:r" in configure
    assert "AreAccessRulesProtected" in verify
    assert "artifact_mismatch" in verify


def test_windows_compute_environment_supports_hybrid_structured_json():
    configure = (REPO_ROOT / "scripts" / "configure_windows_compute_env.ps1").read_text(
        encoding="utf-8"
    )

    for field in (
        "StructuredJsonModel",
        "StructuredJsonRevision",
        "StructuredJsonBaseUrl",
        "StructuredJsonPath",
        "CloudApiKeyFile",
        "PP_LOCAL_NODE_STRUCTURED_JSON_CLOUD_BASE_URL",
        "PP_LOCAL_NODE_STRUCTURED_JSON_CLOUD_PATH",
        "PP_LOCAL_NODE_CLOUD_API_KEY",
    ):
        assert field in configure
    assert '$providerMode = if ($structuredJsonEnabled) { "hybrid" } else { "local" }' in configure
    assert 'PP_LOCAL_NODE_PROVIDER_MODE=$providerMode' in configure
