from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = REPO_ROOT / "plastic_promise" / "mcp" / "dashboard_v2" / "static"


def _source(name: str) -> str:
    return (STATIC_ROOT / name).read_text(encoding="utf-8")


def _deployment_center_surface(app: str) -> str:
    start = app.index("  function deploymentCenterBridgeBase(payload) {")
    end = app.index("  function resetControlSecretEditors(scope) {")
    return app[start:end]


def test_deployment_center_is_a_static_non_authoritative_bridge_view():
    index = _source("index.html")
    app = _source("app.js")
    surface = _deployment_center_surface(app)

    assert 'href="#/deployment-center" data-view="deployment-center"' in index
    assert '"deployment-center": {' in app
    assert "deploymentCenter: true" in app
    assert 'fetch("/pp-local-edge/v1/bridge-config.json", {' in surface
    assert 'cache: "no-store"' in surface
    assert 'credentials: "omit"' in surface
    assert 'referrerPolicy: "no-referrer"' in surface

    assert 'endpoint + "/inspect"' in surface
    assert 'endpoint + "/preview"' in surface
    assert "installation_ref: installationRef" in surface
    assert "candidate_manifest: candidateManifest" in surface
    assert '"/apply"' not in surface
    assert "plan hash is inspection-only" in surface
    assert "mutation is deferred to PR5" in surface
    assert "browser/cache state is non-authoritative" in surface


def test_deployment_center_keeps_bridge_and_candidate_data_page_local_only():
    app = _source("app.js")
    surface = _deployment_center_surface(app)

    assert "http://127.0.0.1:" in surface
    assert 'url.pathname !== "/ppctl/v1"' in surface
    assert 'url.hostname !== "127.0.0.1"' in surface
    assert "DEPLOYMENT_CENTER_DEFAULT_CANDIDATE" in app
    assert 'schema_version: "plastic-promise-deployment/v2"' in app
    assert "candidateText" in surface
    assert "localStorage" not in surface
    assert "Authorization" not in surface
