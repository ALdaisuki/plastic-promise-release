#!/usr/bin/env bash
# Build and smoke-test the local inference-node image from Linux or WSL2.
#
# This is a local derived-inference preflight only. It never pushes an image,
# publishes an artifact, starts MCP, or opens canonical SQLite/LanceDB state.
set -euo pipefail

readonly DEFAULT_BUILDER="plastic-promise-local"
readonly DEFAULT_RETENTION_HOURS="24"
readonly DEFAULT_IMAGE_TAG="plastic-promise-local-inference-node:local"
readonly DEFAULT_PIP_INDEX_URL="https://pypi.org/simple"
readonly REPORT_SCHEMA="plastic-promise-local-node-build/v1"

source_revision=""
image_tag="$DEFAULT_IMAGE_TAG"
builder="$DEFAULT_BUILDER"
retention_hours="$DEFAULT_RETENTION_HOURS"
report_directory="artifacts/local-node-build"
compute_variant="auto"
gpu_smoke="required"
credential_mode="desktop-interactive"
pip_index_url="$DEFAULT_PIP_INDEX_URL"
windows_source_root=""
disk_path="."

usage() {
  cat <<'EOF'
Usage: scripts/run_local_inference_node_build.sh --source-revision <40-hex-sha> [options]

Required:
  --source-revision SHA       Exact source commit used for the OCI revision label.

Options:
  --image-tag TAG            Local-only tag (default: plastic-promise-local-inference-node:local).
  --compute-variant VARIANT  cpu, cuda, or auto (default: auto; detects nvidia-smi).
  --builder NAME             Dedicated Buildx builder (default: plastic-promise-local).
  --retention-hours HOURS    Retain recent project cache/images (default: 24).
  --report-directory PATH    Local-only JSON/JSONL report directory.
  --pip-index-url URL        PyPI index used by the image build (default: pypi.org).
  --credential-mode MODE     desktop-interactive (default) or headless-builder.
  --windows-source-root PATH Exact D:\PlasticPromise\remote-builds\<SHA>\source path when called from Windows.
  --disk-path PATH           Disk to observe during the mandatory resource gate (default: source root).
  --skip-gpu-smoke           Build and label-check only; do not assert CUDA availability.
  --help                     Show this message.

The script always runs the bounded Docker cleanup preflight with --execute
before building. That preflight only removes stale Plastic Promise images and
the named builder's unused cache; it never prunes containers, volumes,
networks, models, databases, or another project's image.
EOF
}

require_value() {
  local option="$1"
  local value="${2:-}"
  if [[ -z "$value" || "$value" == --* ]]; then
    printf 'missing value for %s\n' "$option" >&2
    exit 2
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source-revision)
      require_value "$1" "${2:-}"
      source_revision="$2"
      shift 2
      ;;
    --image-tag)
      require_value "$1" "${2:-}"
      image_tag="$2"
      shift 2
      ;;
    --compute-variant)
      require_value "$1" "${2:-}"
      compute_variant="$2"
      shift 2
      ;;
    --builder)
      require_value "$1" "${2:-}"
      builder="$2"
      shift 2
      ;;
    --retention-hours)
      require_value "$1" "${2:-}"
      retention_hours="$2"
      shift 2
      ;;
    --report-directory)
      require_value "$1" "${2:-}"
      report_directory="$2"
      shift 2
      ;;
    --pip-index-url)
      require_value "$1" "${2:-}"
      pip_index_url="$2"
      shift 2
      ;;
    --credential-mode)
      require_value "$1" "${2:-}"
      credential_mode="$2"
      shift 2
      ;;
    --windows-source-root)
      require_value "$1" "${2:-}"
      windows_source_root="$2"
      shift 2
      ;;
    --disk-path)
      require_value "$1" "${2:-}"
      disk_path="$2"
      shift 2
      ;;
    --skip-gpu-smoke)
      gpu_smoke="skipped_by_operator"
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      printf 'unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ! "$source_revision" =~ ^[0-9a-fA-F]{40}$ ]]; then
  printf 'source revision must be an exact 40-character hexadecimal SHA\n' >&2
  exit 2
fi
source_revision="$(printf '%s' "$source_revision" | tr '[:upper:]' '[:lower:]')"
if [[ ! "$builder" =~ ^plastic-promise-[a-z0-9][a-z0-9-]*$ ]]; then
  printf 'builder must be a dedicated plastic-promise-* builder\n' >&2
  exit 2
fi
if [[ ! "$retention_hours" =~ ^[0-9]+$ ]] || (( retention_hours < 1 || retention_hours > 2160 )); then
  printf 'retention hours must be between 1 and 2160\n' >&2
  exit 2
fi
if [[ ! "$image_tag" =~ ^plastic-promise-local-inference-node:[A-Za-z0-9._-]+$ ]]; then
  printf 'image tag must stay in the local Plastic Promise inference namespace\n' >&2
  exit 2
fi
if [[ ! "$pip_index_url" =~ ^https?://[A-Za-z0-9._:/-]+$ ]]; then
  printf 'pip index url must be an http(s) URL\n' >&2
  exit 2
fi
if [[ "$compute_variant" == "auto" ]]; then
  if command -v nvidia-smi >/dev/null 2>&1; then
    compute_variant="cuda"
  else
    compute_variant="cpu"
  fi
fi
if [[ "$compute_variant" != "cpu" && "$compute_variant" != "cuda" ]]; then
  printf 'compute variant must be cpu, cuda, or auto\n' >&2
  exit 2
fi
if [[ "$credential_mode" != "desktop-interactive" && "$credential_mode" != "headless-builder" ]]; then
  printf 'credential mode must be desktop-interactive or headless-builder\n' >&2
  exit 2
fi
for required_path in deploy/local-inference-node/Dockerfile scripts/prepare_oci_build.py pyproject.toml; do
  if [[ ! -f "$required_path" ]]; then
    printf 'run this script from the Plastic Promise source root; missing %s\n' "$required_path" >&2
    exit 2
  fi
done
for required_path in deploy/oci-base-images.json scripts/resolve_container_artifact_identity.py; do
  if [[ ! -f "$required_path" ]]; then
    printf 'run this script from the Plastic Promise source root; missing %s\n' "$required_path" >&2
    exit 2
  fi
done
pp_python_bin=""
for candidate in python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
    pp_python_bin="$candidate"
    break
  fi
done
if [[ -z "$pp_python_bin" ]]; then
  printf 'Python 3.11 or newer is required for the resource gate and bounded cleanup preflight\n' >&2
  exit 2
fi
if [[ -n "$windows_source_root" ]]; then
  "$pp_python_bin" -m plastic_promise.release_builder.resource_probe validate-windows-source \
    --path "$windows_source_root" \
    --source-revision "$source_revision"
fi
if [[ -d .git ]] && [[ "$(git rev-parse HEAD 2>/dev/null || true)" != "$source_revision" ]]; then
  printf 'source revision does not match this immutable source checkout\n' >&2
  exit 2
fi
# This must precede every Docker mutation, including Buildx creation and the
# bounded cleanup preflight. A busy outcome exits with 75 and does not queue.
"$pp_python_bin" -m plastic_promise.release_builder.resource_probe resource-gate --disk-path "$disk_path"
command -v docker >/dev/null 2>&1 || {
  printf 'docker is required\n' >&2
  exit 2
}

mkdir -p "$report_directory"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
cleanup_report="$report_directory/docker-cleanup-${timestamp}.jsonl"
build_report="$report_directory/local-inference-build-${timestamp}.json"

if [[ "$credential_mode" == "headless-builder" ]]; then
  # This builder never pushes. The empty config keeps a headless invocation
  # from inheriting a desktop credential helper during local image smoke.
  docker_config_directory="$report_directory/docker-config-${timestamp}"
  mkdir -p "$docker_config_directory"
  printf '{"auths":{}}\n' > "$docker_config_directory/config.json"
  export DOCKER_CONFIG="$docker_config_directory"
fi

if ! docker buildx inspect "$builder" >/dev/null 2>&1; then
  docker buildx create --name "$builder" --driver docker-container >/dev/null
fi
docker buildx inspect "$builder" --bootstrap >/dev/null

# This is intentionally mandatory and precedes every local build. The Python
# preflight writes its plan before it makes the narrowly-scoped cleanup change.
"$pp_python_bin" scripts/prepare_oci_build.py \
  --execute \
  --builder "$builder" \
  --retention-hours "$retention_hours" \
  --report "$cleanup_report"

package_version="$("$pp_python_bin" - <<'PY'
import re
from pathlib import Path

content = Path("pyproject.toml").read_text(encoding="utf-8")
match = re.search(r'^version\s*=\s*"([^"]+)"\s*$', content, re.MULTILINE)
if match is None:
    raise SystemExit("package version not found")
print(match.group(1))
PY
)"

local_catalog_digest="$("$pp_python_bin" - <<'PY'
import hashlib

print("sha256:" + hashlib.sha256(b"plastic-promise-local-builder-catalog/v1").hexdigest())
PY
)"
identity_file="$report_directory/container-build-identity-${timestamp}.json"
"$pp_python_bin" scripts/resolve_container_artifact_identity.py \
  --repository-root . \
  --profile-id split-accelerated \
  --source-revision "$source_revision" \
  --package-version "$package_version" \
  --platform linux/amd64 \
  --compute-variant "$compute_variant" \
  --model-catalog-reference local-builder-catalog \
  --model-catalog-digest "$local_catalog_digest" \
  --artifact-role pp-compute-node \
  --artifact-platform linux/amd64 \
  --artifact-variant "$compute_variant" \
  --verify-head \
  --output "$identity_file" >/dev/null

identity_build_arg() {
  local key="$1"
  "$pp_python_bin" - "$identity_file" "$key" <<'PY'
import json
import sys

payload = json.loads(open(sys.argv[1], encoding="utf-8").read())
value = payload["build_args"].get(sys.argv[2])
if not isinstance(value, str) or not value:
    raise SystemExit("container_artifact_identity_output_invalid")
print(value)
PY
}

base_image="$(identity_build_arg BASE_IMAGE)"
base_image_digest="$(identity_build_arg BASE_IMAGE_DIGEST)"
compute_variant="$(identity_build_arg COMPUTE_VARIANT)"
build_policy_digest="$(identity_build_arg BUILD_POLICY_DIGEST)"
recipe_policy_digest="$(identity_build_arg RECIPE_POLICY_DIGEST)"

proxy_build_args=()
for proxy_name in HTTP_PROXY HTTPS_PROXY http_proxy https_proxy; do
  proxy_value="${!proxy_name:-}"
  if [[ -n "$proxy_value" ]]; then
    proxy_build_args+=(--build-arg "${proxy_name}=${proxy_value}")
  fi
done

docker buildx build \
  --builder "$builder" \
  --load \
  --platform linux/amd64 \
  --file deploy/local-inference-node/Dockerfile \
  --tag "$image_tag" \
  --build-arg "BASE_IMAGE=$base_image" \
  --build-arg "BASE_IMAGE_DIGEST=$base_image_digest" \
  --build-arg "COMPUTE_VARIANT=$compute_variant" \
  --build-arg "SOURCE_REVISION=$source_revision" \
  --build-arg "PACKAGE_VERSION=$package_version" \
  --build-arg "BUILD_POLICY_DIGEST=$build_policy_digest" \
  --build-arg "RECIPE_POLICY_DIGEST=$recipe_policy_digest" \
  --build-arg "PIP_INDEX_URL=$pip_index_url" \
  "${proxy_build_args[@]}" \
  .

actual_revision="$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$image_tag")"
actual_base_digest="$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.base.digest" }}' "$image_tag")"
actual_policy_digest="$(docker image inspect --format '{{ index .Config.Labels "org.plastic-promise.build.policy-digest" }}' "$image_tag")"
actual_recipe_policy_digest="$(docker image inspect --format '{{ index .Config.Labels "org.plastic-promise.build.recipe-policy-digest" }}' "$image_tag")"
if [[ "$actual_revision" != "$source_revision" || "$actual_base_digest" != "$base_image_digest" || "$actual_policy_digest" != "$build_policy_digest" || "$actual_recipe_policy_digest" != "$recipe_policy_digest" ]]; then
  printf 'image labels do not match the resolved immutable build identity\n' >&2
  exit 1
fi

# Package smoke does not download or mount model weights. A real embedding or
# rerank run is performed separately only with pre-reviewed, immutable models.
docker run --rm --entrypoint plastic-promise-local-inference-node "$image_tag" --help >/dev/null
if [[ "$gpu_smoke" == "required" && "$compute_variant" == "cuda" ]]; then
  docker run --rm --gpus all --entrypoint nvidia-smi "$image_tag" \
    --query-gpu=name,driver_version,memory.total --format=csv,noheader
  gpu_smoke="passed"
elif [[ "$gpu_smoke" == "required" && "$compute_variant" == "cpu" ]]; then
  gpu_smoke="not_applicable_cpu_variant"
fi

image_id="$(docker image inspect --format '{{.Id}}' "$image_tag")"
"$pp_python_bin" - "$build_report" "$source_revision" "$image_tag" "$image_id" "$builder" \
  "$cleanup_report" "$gpu_smoke" "$package_version" "$identity_file" "$base_image_digest" \
  "$build_policy_digest" "$recipe_policy_digest" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

(
    report_path,
    source_revision,
    image_tag,
    image_id,
    builder,
    cleanup_report,
    gpu_smoke,
    package_version,
    identity_file,
    base_image_digest,
    build_policy_digest,
    recipe_policy_digest,
) = sys.argv[1:]
Path(report_path).write_text(
    json.dumps(
        {
            "schema_version": "plastic-promise-local-node-build/v1",
            "source_revision": source_revision,
            "package_version": package_version,
            "image_tag": image_tag,
            "image_id": image_id,
            "builder": builder,
            "cleanup_report": cleanup_report,
            "identity_file": identity_file,
            "base_image_digest": base_image_digest,
            "build_policy_digest": build_policy_digest,
            "recipe_policy_digest": recipe_policy_digest,
            "package_smoke": "passed",
            "gpu_smoke": gpu_smoke,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        },
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
PY

printf 'local inference image build passed: %s\n' "$build_report"
