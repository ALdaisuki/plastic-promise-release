from pathlib import Path


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
    assert '--release-evidence "$evidence_path"' in workflow
    assert "packages-dir: release-repo/dist/" in workflow
    assert "git push origin main --tags" not in workflow
