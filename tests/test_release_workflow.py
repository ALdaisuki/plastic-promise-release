import re
from pathlib import Path


def _job_block(workflow: str, job_name: str) -> str:
    """Return one top-level workflow job without confusing nested YAML indentation."""

    match = re.search(
        rf"(?ms)^  {re.escape(job_name)}:\n(?P<body>.*?)(?=^  [a-z0-9][a-z0-9-]*:|\Z)",
        workflow,
    )
    assert match is not None, f"missing workflow job: {job_name}"
    return match.group("body")


def test_release_workflow_uses_attested_script_push_and_build_dependencies():
    workflow = Path(".github/workflows/release-sync.yml").read_text(encoding="utf-8")

    assert "--push" in workflow
    assert "build" in workflow
    assert "twine" in workflow
    assert "maturin" in workflow
    assert "rustup.rs" in workflow
    assert '"./dev-repo[dev,neko]"' in workflow
    assert "path: release-repo" in workflow
    assert "--release-repo release-repo" in workflow
    assert "release_evidence_json" in workflow
    assert "release_manifest_json" in workflow
    assert "server_deployment_receipt_json" in workflow
    assert "source_commit" in workflow
    assert "environment: production-release" in workflow
    assert "id-token: write" in workflow
    assert "github.repository == 'ALdaisuki/plastic-promise-release'" in workflow
    assert "PYPI_API_TOKEN" not in workflow
    assert "DEV_REPO_PAT" not in workflow
    assert '--release-evidence "$evidence_path"' in workflow
    assert '--release-manifest "$manifest_path"' in workflow
    assert '--server-deployment-receipt "$deployment_receipt_path"' in workflow
    assert "packages-dir: release-repo/dist/" in workflow
    assert "git push origin main --tags" not in workflow


def test_release_workflow_roles_keep_pr_rc_and_stable_publishing_separate():
    verification = Path(".github/workflows/release-verify.yml").read_text(encoding="utf-8")
    rc = Path(".github/workflows/release-rc.yml").read_text(encoding="utf-8")
    testpypi = Path(".github/workflows/release-testpypi.yml").read_text(encoding="utf-8")
    stable = Path(".github/workflows/release-publish.yml").read_text(encoding="utf-8")

    assert "pull_request:" in verification
    # PR verification is evidence-only: OCI layouts are local workflow outputs,
    # not registry publication, signing, deployment, or PyPI activity.
    assert "--push" not in verification
    assert "docker/build-push-action@v6" not in verification
    assert "docker/login-action" not in verification
    assert "pypi-publish" not in verification
    assert "environment: production-release" not in verification
    assert "workflow_dispatch:" in rc
    assert "push: false" not in rc
    assert "pypi-publish" not in rc
    assert "release-manifest.json" in rc
    assert "model_catalog_path:" in rc
    assert "git ls-files --error-unmatch" in rc
    assert "--validate-catalog-only" in rc
    assert "--prepare-only" in rc
    assert "--verified-evidence dist/verified-evidence.json" in rc
    assert "release_bundle_catalog_profile_matrix_unsupported" in Path(
        "scripts/create_release_bundle.py"
    ).read_text(encoding="utf-8")
    assert "local-edge.oci.tar" in rc
    assert "inference-cpu.oci.tar" in rc
    assert "inference-node.oci.tar" in rc
    assert 'local platforms="$3"' in rc
    assert '--platform "$platforms"' in rc
    assert "linux/amd64,linux/arm64 deploy/local-edge/Dockerfile" in rc
    assert "linux/amd64,linux/arm64 deploy/server/Dockerfile" in rc
    assert "linux/amd64,linux/arm64 deploy/local-inference-node/Dockerfile" in rc
    assert "linux/amd64 deploy/local-inference-node/Dockerfile" in rc
    assert 'variant_args=(--build-arg "COMPUTE_VARIANT=$compute_variant")' in rc
    for tag in (
        "plastic-promise.invalid/local-edge:rc-${SOURCE_REVISION}",
        "plastic-promise.invalid/server:rc-${SOURCE_REVISION}",
        "plastic-promise.invalid/compute-node-cpu:rc-${SOURCE_REVISION}",
        "plastic-promise.invalid/compute-node-cuda:rc-${SOURCE_REVISION}",
    ):
        assert tag in rc
    assert "python:3.12-slim-bookworm" not in rc
    assert "nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04" not in rc
    assert "deploy/oci-base-images.json" in rc
    assert "scripts/resolve_container_artifact_identity.py" in rc
    assert "scripts/verify_oci_artifact_evidence.py" in rc
    assert "--sbom=true --provenance=mode=max" in rc
    assert rc.count("--artifact-evidence ") == 14
    assert "anchore/sbom-action/download-syft@v0" in rc
    assert 'syft scan "oci-archive:${archive}"' in rc
    assert '--platform "$target_platform"' in rc
    assert rc.count("--artifact-sbom ") == 14
    for artifact_sbom in (
        "local-edge-linux-amd64-standard.sbom.cdx.json",
        "local-edge-linux-arm64-standard.sbom.cdx.json",
        "server-backend-linux-amd64-standard.sbom.cdx.json",
        "server-backend-linux-arm64-standard.sbom.cdx.json",
        "compute-node-linux-amd64-cpu.sbom.cdx.json",
        "compute-node-linux-arm64-cpu.sbom.cdx.json",
        "compute-node-linux-amd64-cuda.sbom.cdx.json",
    ):
        assert artifact_sbom in rc
    assert ".release-package-sbom-venv/bin/python -m pip install dist/*.whl" in rc
    assert "dist/package.sbom.cdx.json" in rc
    assert "actions/attest-build-provenance@v2" in rc
    assert rc.count("actions/attest-build-provenance@v2") == 5
    for subject_path in (
        "dist/release-manifest.json",
        "dist/model-catalog.json",
        "dist/artifact-binding.json",
        "dist/artifact-sbom-receipts.json",
        "dist/release-bundle.json",
    ):
        assert f"subject-path: {subject_path}" in rc
    assert rc.count("--artifact-sbom-receipts dist/artifact-sbom-receipts.json") == 2
    assert "artifact-sbom-receipts" in rc
    assert "gh attestation verify" in rc
    assert "--signer-workflow" in rc
    assert "--cert-oidc-issuer" in rc
    assert "--source-digest" in rc
    assert "github-attestation-verify" in rc
    assert "local-generated-evidence" not in rc
    assert "SOURCE_REVISION" in rc
    assert "PACKAGE_VERSION" in rc
    assert "rc_source_commit_sha_required" in rc
    assert "rc_source_not_reachable_from_default_branch" in rc
    assert "rc_default_branch_resolution_failed" in rc
    assert "rc_workflow_source_commit_mismatch" in rc
    assert "rc_workflow_default_branch_required" in rc
    assert "rc_workflow_protected_ref_required" in rc
    assert "WORKFLOW_REF_PROTECTED: ${{ github.ref_protected }}" in rc
    assert "GH_TOKEN: ${{ github.token }}" in rc
    assert "environment: release-candidate" in rc
    assert 'gh api "repos/${GITHUB_REPOSITORY}" --jq .default_branch' in rc
    assert "Verify source package metadata before OCI work" in rc
    assert "rc_source_package_version_mismatch" in rc
    assert "release_version: ${{ steps.resolve.outputs.release_version }}" in rc
    assert "needs.resolve-rc-source.outputs.release_version" in rc
    assert "${{ inputs.release_version }}" not in rc.split("build-rc-artifacts:", 1)[1]
    assert "environment: testpypi" in testpypi
    assert "repository-url: https://test.pypi.org/legacy/" in testpypi
    assert "id-token: write" in testpypi
    assert "resolve-source:" in testpypi
    assert "testpypi_source_commit_sha_required" in testpypi
    assert "testpypi_source_commit_resolution_mismatch" in testpypi
    assert "EXPECTED_PACKAGE_VERSION" in testpypi
    assert 'os.environ["EXPECTED_PACKAGE_VERSION"]' in testpypi
    assert "${{ inputs.package_version }}" not in testpypi.split("python - <<'PY'", 1)[1]
    assert "workflow_dispatch:" in stable
    assert "environment: production-release" in stable
    assert "packages: write" in stable
    assert "attest-build-provenance" in stable
    assert "docker/login-action@v3" in stable
    assert "registry: ghcr.io" in stable
    assert "password: ${{ secrets.GITHUB_TOKEN }}" in stable
    assert "ghcr.io/aldaisuki/plastic-promise-local-edge" in stable
    assert "ghcr.io/aldaisuki/plastic-promise-server" in stable
    assert "ghcr.io/aldaisuki/plastic-promise-local-inference-node" in stable
    assert ":sha-${{ needs.resolve-source.outputs.source_commit }}" in stable
    assert ":sha-${{ needs.resolve-source.outputs.source_commit }}-cpu" in stable
    assert ":sha-${{ needs.resolve-source.outputs.source_commit }}-cuda" in stable
    assert "Publish multi-architecture local-edge image by immutable digest" in stable
    assert "Publish multi-architecture CPU inference-node image by immutable digest" in stable
    assert "Publish Linux NVIDIA inference-node image by immutable digest" in stable
    assert "Enforce the compressed CUDA control-image size budget" in stable
    assert 'MAX_COMPRESSED_BYTES: "1073741824"' in stable
    assert "stable_cuda_control_image_size_budget_exceeded" in stable
    assert "platforms: linux/amd64,linux/arm64" in stable
    assert "COMPUTE_VARIANT=cpu" in stable
    assert "COMPUTE_VARIANT=cuda" in stable
    for image_name in ("local-edge", "server", "inference-cpu", "inference-node"):
        assert f'--image "{image_name}=' in stable
    assert "resolve-source:" in stable
    assert "stable_source_commit_sha_required" in stable
    assert "stable_release_version_required" in stable
    assert "EXPECTED_SEMVER" in stable
    assert 'os.environ["EXPECTED_SEMVER"]' in stable
    assert "needs.resolve-source.outputs.source_commit" in stable
    for release_gate_path in (
        "README.md",
        "docs/README.zh-CN.md",
        ".github/readme-release-delivery.zh-CN.svg",
        "plastic_promise/deployment/**",
    ):
        assert release_gate_path in verification


def test_release_verification_covers_release_workflows_docs_and_contract_tests():
    verification = Path(".github/workflows/release-verify.yml").read_text(encoding="utf-8")

    assert '".github/workflows/release-*.yml"' in verification
    assert '"MANIFEST.in"' in verification
    assert '"docs/deployment/**"' in verification
    assert '"docs/release/**"' in verification
    assert '"docs/architecture/release-delivery/**"' in verification
    assert '"docs/architecture/three-endpoint-deployment/**"' in verification
    assert '"plastic_promise/deployment/**"' in verification
    assert '"tests/test_model_catalog.py"' in verification
    assert '"tests/test_release_bundle.py"' in verification
    assert (
        "Verify source distribution contains public activation templates and linked docs"
        in verification
    )
    assert "required_members+=(docs/deployment/*.md)" in verification
    assert "deploy/manifests/split-accelerated.example.json" in verification
    assert "deploy/local-inference-node/Dockerfile" in verification
    assert "deploy/local-edge/Dockerfile" in verification
    assert "deploy/local-inference-node/compose.cpu.yaml" in verification
    assert "deploy/local-inference-node/compose.cuda.yaml" in verification
    assert "deploy/release-builder/windows-install.ps1" in verification
    assert "scripts/plastic-promise-deploy.sh" in verification
    assert "scripts/plastic-promise-deploy.ps1" in verification
    assert "scripts/verify_release_deployment.py" in verification
    assert "scripts/create_release_bundle.py" in verification
    assert "docs/architecture/three-endpoint-deployment/container-artifacts.md" in verification
    assert '"release/**"' in verification
    assert "Verify every release profile without creating deployment state" in verification
    assert "python scripts/verify_release_deployment.py" in verification
    assert "tests/test_release_delivery_assets.py" in verification
    assert "tests/test_model_catalog.py" in verification
    assert "tests/test_release_bundle.py" in verification
    assert "tests/test_create_release_bundle.py" in verification
    assert "tests/test_packaging_metadata.py" in verification
    assert "tests/test_oci_build_preflight.py" in verification
    assert "tests/test_container_artifacts.py" in verification
    assert "tests/test_oci_artifact_evidence.py" in verification
    assert "scripts/prepare_oci_build.py" in verification
    assert "scripts/resolve_container_artifact_identity.py" in verification
    assert "scripts/validate_container_artifact_policy.py" in verification
    assert "scripts/verify_oci_artifact_evidence.py" in verification
    assert "deploy/oci-base-images.json" in verification


def test_ci_enforces_documentation_parity_outside_advisory_full_test_job():
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    match = re.search(
        r"^  documentation-parity:\n(?P<body>.*?)(?=^  [a-z0-9-]+:\n|\Z)",
        workflow,
        flags=re.DOTALL | re.MULTILINE,
    )

    assert match is not None
    job = match.group("body")
    assert 'name: "P0: Documentation parity & architecture"' in job
    assert "continue-on-error" not in job
    assert "tests/test_documentation_parity.py" in job
    assert "--no-cov" in job


def test_each_oci_verification_job_installs_static_recipe_policy_dependency_first():
    verification = Path(".github/workflows/release-verify.yml").read_text(encoding="utf-8")
    install = (
        "python -m pip install --disable-pip-version-check "
        '"$PP_STATIC_RECIPE_POLICY_PYTHON_REQUIREMENT"'
    )

    assert 'PP_STATIC_RECIPE_POLICY_PYTHON_REQUIREMENT: "PyYAML>=6.0.1,<7.0.0"' in verification
    for job_name in (
        "local-edge-image",
        "server-image",
        "compute-node-cpu-image",
        "compute-node-cuda-image",
    ):
        job = _job_block(verification, job_name)
        assert install in job
        assert job.index(install) < job.index(
            "python scripts/validate_container_artifact_policy.py"
        )


def test_release_verification_emits_plan_bound_no_push_oci_evidence_for_every_artifact():
    verification = Path(".github/workflows/release-verify.yml").read_text(encoding="utf-8")

    # The human-facing PR tag may remain ``pr-verify``, but the version passed
    # into the Python package and OCI labels must be valid PEP 440.  This keeps
    # the protected GitHub build path from failing during role-package
    # materialization while preserving a recognizable evidence tag.
    assert "PP_PR_VERIFY_PACKAGE_VERSION: 0.0.0+pr.verify" in verification
    assert "--package-version pr-verify" not in verification
    assert verification.count('--package-version "$PP_PR_VERIFY_PACKAGE_VERSION"') == 8

    expected_jobs = {
        "local-edge-image": {
            "role": "pp-local-edge",
            "variant": "standard",
            "recipe": "deploy/local-edge/Dockerfile",
            "platforms": "linux/amd64,linux/arm64",
            "attestation_tag": "local-edge",
        },
        "server-image": {
            "role": "pp-server-backend",
            "variant": "standard",
            "recipe": "deploy/server/Dockerfile",
            "platforms": "linux/amd64,linux/arm64",
            "attestation_tag": "server",
        },
        "compute-node-cpu-image": {
            "role": "pp-compute-node",
            "variant": "cpu",
            "recipe": "deploy/local-inference-node/Dockerfile",
            "platforms": "linux/amd64,linux/arm64",
            "attestation_tag": "compute-node-cpu",
        },
        "compute-node-cuda-image": {
            "role": "pp-compute-node",
            "variant": "cuda",
            "recipe": "deploy/local-inference-node/Dockerfile",
            "platforms": "linux/amd64",
            "attestation_tag": "compute-node-cuda",
        },
    }

    for job_name, expected in expected_jobs.items():
        job = _job_block(verification, job_name)
        assert "python scripts/validate_container_artifact_policy.py" in job
        assert "python scripts/resolve_container_artifact_identity.py" in job
        assert f"--artifact-role {expected['role']}" in job
        if expected["variant"] != "standard":
            assert f"--artifact-variant {expected['variant']}" in job
        assert f"--platform {expected['platforms']}" in job
        assert "docker buildx build --builder plastic-promise-oci" in job
        assert "--sbom=true --provenance=mode=max" in job
        assert (
            '--tag "plastic-promise.invalid/'
            f"{expected['attestation_tag']}:pr-verify-${{{{ github.sha }}}}" + '"'
        ) in job
        assert "--output type=oci,dest=/tmp/" in job
        assert '--build-arg BASE_IMAGE="$BASE_IMAGE"' in job
        assert '--build-arg BASE_IMAGE_DIGEST="$BASE_IMAGE_DIGEST"' in job
        assert '--build-arg SOURCE_REVISION="$SOURCE_REVISION"' in job
        assert '--build-arg PACKAGE_VERSION="$PACKAGE_VERSION"' in job
        assert '--build-arg BUILD_POLICY_DIGEST="$BUILD_POLICY_DIGEST"' in job
        assert '--build-arg RECIPE_POLICY_DIGEST="$RECIPE_POLICY_DIGEST"' in job
        assert f"--file {expected['recipe']} ." in job
        assert "python scripts/verify_oci_artifact_evidence.py" in job
        assert "actions/upload-artifact@v4" in job
        assert "--push" not in job
        assert "docker/login-action" not in job
        assert "environment:" not in job

    # CPU/CUDA selection is a build identity input, never inferred from a
    # floating base-image tag or a runtime GPU smoke test in PR verification.
    assert '--build-arg COMPUTE_VARIANT="$COMPUTE_VARIANT"' in _job_block(
        verification, "compute-node-cpu-image"
    )
    assert '--build-arg COMPUTE_VARIANT="$COMPUTE_VARIANT"' in _job_block(
        verification, "compute-node-cuda-image"
    )


def test_every_official_oci_build_runs_the_protected_prebuild_cleanup_first():
    workflow_paths = (
        ".github/workflows/ci.yml",
        ".github/workflows/release-verify.yml",
        ".github/workflows/release-rc.yml",
        ".github/workflows/release-publish.yml",
    )
    for path in workflow_paths:
        workflow = Path(path).read_text(encoding="utf-8")
        assert "python scripts/prepare_oci_build.py --execute" in workflow
        assert "name: plastic-promise-oci" in workflow
        assert "use: true" in workflow
        assert (
            "builder: plastic-promise-oci" in workflow
            or "--builder plastic-promise-oci" in workflow
        )
        assert workflow.index("python scripts/prepare_oci_build.py --execute") < min(
            index
            for token in ("docker build", "docker/build-push-action@v6")
            if (index := workflow.find(token)) >= 0
        )


def test_every_official_oci_build_reclaims_only_disposable_ci_runner_space_first():
    workflow_paths = (
        ".github/workflows/ci.yml",
        ".github/workflows/release-verify.yml",
        ".github/workflows/release-rc.yml",
        ".github/workflows/release-publish.yml",
    )
    for path in workflow_paths:
        workflow = Path(path).read_text(encoding="utf-8")
        assert "bash scripts/reclaim_ci_runner_space.sh" in workflow
        assert workflow.index("bash scripts/reclaim_ci_runner_space.sh") < workflow.index(
            "python scripts/prepare_oci_build.py --execute"
        )


def test_ci_runner_reclaim_script_has_a_fixed_disposable_path_allowlist():
    script = Path("scripts/reclaim_ci_runner_space.sh").read_text(encoding="utf-8")
    for path in (
        "/usr/local/lib/android",
        "/usr/share/dotnet",
        "/opt/ghc",
        "/opt/hostedtoolcache/CodeQL",
    ):
        assert path in script
    assert "/srv/" not in script
    assert "plastic_memory.db" not in script
    assert "docker system prune" not in script


def test_oci_cleanup_jobs_pin_the_python_interpreter_before_running_the_script():
    for path in (".github/workflows/release-verify.yml", ".github/workflows/release-publish.yml"):
        workflow = Path(path).read_text(encoding="utf-8")
        cleanup_index = workflow.index("python scripts/prepare_oci_build.py --execute")
        python_index = workflow.rfind("actions/setup-python@v5", 0, cleanup_index)

        assert python_index >= 0
        assert 'python-version: "3.12"' in workflow[python_index:cleanup_index]
