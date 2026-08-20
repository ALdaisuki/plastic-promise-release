#!/usr/bin/env bash
# One-click local compute-node build, start, and performance smoke.
#
# Generic across macOS, Linux, and WSL2: the script auto-detects the source
# revision (git HEAD), Docker, and CUDA (nvidia-smi), resolves the
# variant, generates a non-secret compose .env with pinned model identity, then
# runs the immutable local build, starts Compose, and records performance
# evidence.  No machine-specific user, path, model, or revision is hard-coded:
# every value is auto-detected or must be supplied through PP_NODE_* env vars.
set -euo pipefail

source_revision=""
variant="auto"
builder=""
image_tag=""
retention_hours=""
report_directory=""
node_config=""
runtime_status=""
skip_gpu_smoke=""
no_start=""
dry_run=""

usage() {
  cat <<'EOF'
Usage: scripts/build_compute_node.sh [options]

One-click local compute-node build + start + performance smoke.

Options:
  --source-revision SHA    40-hex source SHA (default: git HEAD of this checkout).
  --variant VARIANT        cpu, cuda, or auto (default: auto).
  --builder NAME           Dedicated Buildx builder (default: plastic-promise-local).
  --image-tag TAG          Local-only image tag.
  --retention-hours HOURS  Retain recent project cache/images (default: 24).
  --report-directory PATH  Local-only build/smoke report directory.
  --node-config PATH       compose .env to generate/update (default: deploy/local-inference-node/.env).
  --runtime-status PATH    runtime-status.json written after the performance smoke.
  --credential-mode MODE   desktop-interactive (default) or headless-builder.
  --skip-gpu-smoke         Explicit degraded override; report is not GPU-smoke evidence.
  --no-start               Build and verify image labels only; do not start Compose.
  --dry-run                Print the resolved commands without executing them.
  --help                   Show this message.

Model identity is never hard-coded.  PP_LOCAL_NODE_EMBEDDING_MODEL / _REVISION /
_DIMENSION / _NORMALIZATION and PP_LOCAL_NODE_RERANK_MODEL / _REVISION must be
provided or auto-derived (Ollama digest from /api/tags); otherwise the script
fails closed with explicit remediation.
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
    --variant)
      require_value "$1" "${2:-}"
      variant="$2"
      shift 2
      ;;
    --builder)
      require_value "$1" "${2:-}"
      builder="$2"
      shift 2
      ;;
    --image-tag)
      require_value "$1" "${2:-}"
      image_tag="$2"
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
    --node-config)
      require_value "$1" "${2:-}"
      node_config="$2"
      shift 2
      ;;
    --runtime-status)
      require_value "$1" "${2:-}"
      runtime_status="$2"
      shift 2
      ;;
    --credential-mode)
      require_value "$1" "${2:-}"
      credential_mode="$2"
      shift 2
      ;;
    --skip-gpu-smoke)
      skip_gpu_smoke="--skip-gpu-smoke"
      shift
      ;;
    --no-start)
      no_start="1"
      shift
      ;;
    --dry-run)
      dry_run="1"
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

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
for required_path in scripts/run_local_inference_node_build.sh scripts/pp_node_smoke.py pyproject.toml; do
  if [[ ! -f "$required_path" ]]; then
    printf 'run this script from the Plastic Promise source root; missing %s\n' "$required_path" >&2
    exit 2
  fi
done

if [[ -z "$source_revision" ]]; then
  source_revision="$(git rev-parse HEAD 2>/dev/null || true)"
fi
if [[ ! "$source_revision" =~ ^[0-9a-fA-F]{40}$ ]]; then
  printf 'source revision must be an exact 40-character hexadecimal SHA (set --source-revision)\n' >&2
  exit 2
fi
source_revision="$(printf '%s' "$source_revision" | tr '[:upper:]' '[:lower:]')"
short_revision="${source_revision:0:7}"

if [[ "$variant" == "auto" ]]; then
  if command -v nvidia-smi >/dev/null 2>&1; then
    variant="cuda"
  else
    variant="cpu"
  fi
fi
if [[ "$variant" != "cpu" && "$variant" != "cuda" ]]; then
  printf 'variant must be cpu, cuda, or auto\n' >&2
  exit 2
fi

builder="${builder:-plastic-promise-local}"
image_tag="${image_tag:-plastic-promise-local-inference-node:${variant}-${short_revision}}"
retention_hours="${retention_hours:-24}"
report_directory="${report_directory:-artifacts/local-node-build}"
node_config="${node_config:-deploy/local-inference-node/.env}"
runtime_status="${runtime_status:-${report_directory}/runtime-status.json}"
mkdir -p "$report_directory"

# --- resolve model identity (never hard-coded) -----------------------------
embedding_backend="${PP_LOCAL_NODE_EMBEDDING_BACKEND:-}"
embedding_model="${PP_LOCAL_NODE_EMBEDDING_MODEL:-}"
embedding_revision="${PP_LOCAL_NODE_EMBEDDING_REVISION:-}"
embedding_dimension="${PP_LOCAL_NODE_EMBEDDING_DIMENSION:-}"
embedding_normalization="${PP_LOCAL_NODE_EMBEDDING_NORMALIZATION:-l2}"
rerank_backend="${PP_LOCAL_NODE_RERANK_BACKEND:-}"
rerank_model="${PP_LOCAL_NODE_RERANK_MODEL:-}"
rerank_revision="${PP_LOCAL_NODE_RERANK_REVISION:-}"
ollama_host="${PP_LOCAL_NODE_OLLAMA_HOST:-}"
model_directory="${PP_LOCAL_NODE_MODEL_DIRECTORY:-}"

if [[ -z "$embedding_backend" ]]; then
  embedding_backend="llama.cpp"
fi
if [[ -z "$rerank_backend" ]]; then
  rerank_backend="llama.cpp"
fi
if [[ -z "$ollama_host" ]]; then
  if [[ "$variant" == "cuda" ]]; then
    ollama_host="http://host.docker.internal:11434"
  else
    ollama_host="http://127.0.0.1:11434"
  fi
fi

if [[ "$embedding_backend" == "llama.cpp" ]]; then
  [[ -n "$embedding_model" ]] || embedding_model="Qwen3-Embedding-4B-GGUF"
  [[ -n "$embedding_dimension" ]] || embedding_dimension="2560"
  if [[ -z "$embedding_revision" ]]; then
    printf 'llama.cpp embedding requires PP_LOCAL_NODE_EMBEDDING_REVISION for %s\n' "$embedding_model" >&2
    exit 3
  fi
elif [[ "$embedding_backend" == "ollama" ]]; then
  if [[ -z "$embedding_model" ]]; then
    printf 'ollama embedding requires PP_LOCAL_NODE_EMBEDDING_MODEL (e.g. qwen3-embedding:4b)\n' >&2
    exit 3
  fi
  if [[ -z "$embedding_revision" ]]; then
    probe_host="${PP_OLLAMA_PROBE_HOST:-127.0.0.1:11434}"
    tags_json="$(curl -fsS --max-time 10 "http://${probe_host}/api/tags" 2>/dev/null || true)"
    if [[ -z "$tags_json" ]]; then
      printf 'Ollama probe failed at %s; set PP_OLLAMA_PROBE_HOST or PP_LOCAL_NODE_EMBEDDING_REVISION explicitly\n' "$probe_host" >&2
      exit 3
    fi
    embedding_revision="$(printf '%s' "$tags_json" | python3 -c '
import json, sys
model = sys.argv[1]
payload = json.load(sys.stdin)
for item in payload.get("models", []):
    if item.get("name") == model:
        digest = item.get("digest", "")
        if digest.startswith("sha256:"):
            print(digest)
            break
' "$embedding_model")"
    if [[ -z "$embedding_revision" ]]; then
      printf 'model %s not found in Ollama /api/tags; pull it first or set PP_LOCAL_NODE_EMBEDDING_REVISION\n' "$embedding_model" >&2
      exit 3
    fi
  fi
  if [[ -z "$embedding_dimension" ]]; then
    embedding_dimension="2560"
  fi
else
  [[ -n "$embedding_model" ]] || embedding_model="BAAI/bge-small-en-v1.5"
  [[ -n "$embedding_dimension" ]] || embedding_dimension="384"
  if [[ -z "$embedding_revision" ]]; then
    printf 'CPU bge-local embedding requires PP_LOCAL_NODE_EMBEDDING_REVISION (fixed 40-hex) with PP_LOCAL_NODE_EMBEDDING_MODEL=%s\n' "$embedding_model" >&2
    exit 3
  fi
fi

if [[ "$rerank_backend" == "llama.cpp" ]]; then
  [[ -n "$rerank_model" ]] || rerank_model="Qwen3-Reranker-4B-GGUF"
  if [[ -z "$rerank_revision" ]]; then
    printf 'llama.cpp rerank requires PP_LOCAL_NODE_RERANK_REVISION for %s\n' "$rerank_model" >&2
    exit 3
  fi
elif [[ "$rerank_backend" == "qwen3-cross-encoder" ]]; then
  [[ -n "$rerank_model" ]] || rerank_model="Qwen/Qwen3-Reranker-4B"
  if [[ -z "$rerank_revision" ]]; then
    printf 'qwen3-cross-encoder requires PP_LOCAL_NODE_RERANK_REVISION (fixed 40-hex) with PP_LOCAL_NODE_RERANK_MODEL=%s\n' "$rerank_model" >&2
    exit 3
  fi
else
  [[ -n "$rerank_model" ]] || rerank_model="BAAI/bge-reranker-v2-m3"
  if [[ -z "$rerank_revision" ]]; then
    printf 'CPU bge-local rerank requires PP_LOCAL_NODE_RERANK_REVISION (fixed 40-hex) with PP_LOCAL_NODE_RERANK_MODEL=%s\n' "$rerank_model" >&2
    exit 3
  fi
fi
if [[ -z "$model_directory" ]]; then
  printf 'PP_LOCAL_NODE_MODEL_DIRECTORY (pre-populated read-only model dir) is required\n' >&2
  exit 3
fi
if [[ ! -d "$model_directory" ]]; then
  printf 'PP_LOCAL_NODE_MODEL_DIRECTORY %s does not exist\n' "$model_directory" >&2
  exit 3
fi

# --- write the compose .env (non-secret identity only) ----------------------
cat > "$node_config" <<EOF
PP_LOCAL_NODE_ID=${PP_LOCAL_NODE_ID:-inference-node}
PP_LOCAL_NODE_EMBEDDING_BACKEND=${embedding_backend}
PP_LOCAL_NODE_EMBEDDING_MODEL=${embedding_model}
PP_LOCAL_NODE_EMBEDDING_REVISION=${embedding_revision}
PP_LOCAL_NODE_EMBEDDING_DIMENSION=${embedding_dimension}
PP_LOCAL_NODE_EMBEDDING_NORMALIZATION=${embedding_normalization}
PP_LOCAL_NODE_EMBEDDING_MODEL_REFERENCE=${PP_LOCAL_NODE_EMBEDDING_MODEL_REFERENCE:-/models/embedding}
PP_LOCAL_NODE_EMBEDDING_LLAMA_CPP_BASE_URL=${PP_LOCAL_NODE_EMBEDDING_LLAMA_CPP_BASE_URL:-http://127.0.0.1:19131}
PP_LOCAL_NODE_EMBEDDING_LLAMA_CPP_PATH=${PP_LOCAL_NODE_EMBEDDING_LLAMA_CPP_PATH:-/v1/embeddings}
PP_LOCAL_NODE_RERANK_BACKEND=${rerank_backend}
PP_LOCAL_NODE_RERANK_MODEL=${rerank_model}
PP_LOCAL_NODE_RERANK_REVISION=${rerank_revision}
PP_LOCAL_NODE_RERANK_MODEL_REFERENCE=${PP_LOCAL_NODE_RERANK_MODEL_REFERENCE:-/models/rerank}
PP_LOCAL_NODE_RERANK_LLAMA_CPP_BASE_URL=${PP_LOCAL_NODE_RERANK_LLAMA_CPP_BASE_URL:-http://127.0.0.1:19132}
PP_LOCAL_NODE_RERANK_LLAMA_CPP_PATH=${PP_LOCAL_NODE_RERANK_LLAMA_CPP_PATH:-/rerank}
PP_LOCAL_NODE_OLLAMA_HOST=${ollama_host}
PP_LOCAL_NODE_MODEL_DIRECTORY=${model_directory}
EOF
printf 'wrote node compose env: %s\n' "$node_config"

# --- immutable build ---------------------------------------------------------
build_args=(--source-revision "$source_revision" --compute-variant "$variant" --builder "$builder")
[[ -n "$image_tag" ]] && build_args+=(--image-tag "$image_tag")
[[ -n "$retention_hours" ]] && build_args+=(--retention-hours "$retention_hours")
[[ -n "$report_directory" ]] && build_args+=(--report-directory "$report_directory")
[[ -n "$skip_gpu_smoke" ]] && build_args+=("$skip_gpu_smoke")
if [[ -n "$credential_mode" ]]; then
  if [[ "$credential_mode" != "desktop-interactive" && "$credential_mode" != "headless-builder" ]]; then
    printf 'credential mode must be desktop-interactive or headless-builder\n' >&2
    exit 2
  fi
  build_args+=(--credential-mode "$credential_mode")
fi

if [[ -n "$dry_run" ]]; then
  printf '%s\n' "build: bash scripts/run_local_inference_node_build.sh ${build_args[*]}"
else
  bash scripts/run_local_inference_node_build.sh "${build_args[@]}"
fi

compose_file="deploy/local-inference-node/compose.yaml"
[[ "$variant" == "cpu" ]] && compose_file="deploy/local-inference-node/compose.cpu.yaml"
if [[ -n "$dry_run" ]]; then
  printf 'start: docker compose -f %s --env-file %s up -d --no-build\n' "$compose_file" "$node_config"
  printf 'smoke: python3 scripts/pp_node_smoke.py --node-config %s --output-dir %s --runtime-status %s\n' \
    "$node_config" "$report_directory" "$runtime_status"
  exit 0
fi

# --- bridge container identity into the compose env (fail closed) ----------
identity_file="$(ls -1t "$report_directory"/container-build-identity-*.json 2>/dev/null | head -n1 || true)"
if [[ -z "$identity_file" || ! -f "$identity_file" ]]; then
  printf 'container identity missing in %s\n' "$report_directory" >&2
  exit 3
fi
python3 - "$identity_file" "$node_config" "$variant" <<'PY'
import json
import sys

identity = json.load(open(sys.argv[1], encoding="utf-8"))
env_path = sys.argv[2]
prefix = "PP_COMPUTE_CPU" if sys.argv[3] == "cpu" else "PP_COMPUTE_CUDA"
added = {
    f"{prefix}_BASE_IMAGE": identity["base_image_reference"],
    f"{prefix}_BASE_IMAGE_DIGEST": identity["base_image_digest"],
    "PP_BUILD_SOURCE_REVISION": identity["build_args"]["SOURCE_REVISION"],
    "PP_BUILD_PACKAGE_VERSION": identity["build_args"]["PACKAGE_VERSION"],
    "PP_BUILD_POLICY_DIGEST": identity["build_args"]["BUILD_POLICY_DIGEST"],
    "PP_RECIPE_POLICY_DIGEST": identity["build_args"]["RECIPE_POLICY_DIGEST"],
}
values = {}
with open(env_path, encoding="utf-8") as handle:
    for line in handle:
        line = line.rstrip("\n")
        if "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value
values.update(added)
with open(env_path, "w", encoding="utf-8", newline="\n") as handle:
    for key in sorted(values):
        handle.write(f"{key}={values[key]}\n")
print("enriched compose env with container identity: " + ", ".join(sorted(added)))
PY

if [[ -n "$no_start" ]]; then
  printf 'no-start requested; image built and identity env written without Compose start\n'
  exit 0
fi

compose_image="plastic-promise-local-inference-node:dev"
[[ "$variant" == "cpu" ]] && compose_image="plastic-promise-compute-node:cpu-dev"
docker tag "$image_tag" "$compose_image"
printf 'aliased built image %s -> %s\n' "$image_tag" "$compose_image"

docker compose -f "$compose_file" --env-file "$node_config" up -d --no-build
for _attempt in $(seq 1 30); do
  if curl -fsS --max-time 2 "http://127.0.0.1:19130/health" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
python3 scripts/pp_node_smoke.py \
  --node-config "$node_config" \
  --output-dir "$report_directory" \
  --runtime-status "$runtime_status"
printf 'one-click compute-node build/start/smoke complete; reports under %s\n' "$report_directory"
