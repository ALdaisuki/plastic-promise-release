#!/usr/bin/env bash
set -euo pipefail

# Start the external llama.cpp embedding and rerank workers used by
# pp-compute-node.  The model files remain operator-managed and are mounted
# read-only; Plastic Promise stores only their immutable identity metadata.

docker_bin="${PP_DOCKER_BIN:-docker}"
action="start"
resource_gate="${PP_LLAMA_CPP_RESOURCE_GATE:-on}"
resource_gate_disk_path="${PP_LLAMA_CPP_RESOURCE_GATE_DISK_PATH:-}"
python_bin="${PP_LLAMA_CPP_PYTHON_BIN:-python3}"
image="${PP_LLAMA_CPP_IMAGE:-}"
model_root="${PP_LLAMA_CPP_MODEL_ROOT:-/mnt/d/PlasticPromise/models}"
embedding_file="${PP_LLAMA_CPP_EMBEDDING_FILE:-embedding/Qwen3-Embedding-4B-Q4_K_M.gguf}"
rerank_file="${PP_LLAMA_CPP_RERANK_FILE:-rerank/Qwen3-Reranker-4B-Q4_K_M.gguf}"
embedding_port="${PP_LLAMA_CPP_EMBEDDING_PORT:-19131}"
rerank_port="${PP_LLAMA_CPP_RERANK_PORT:-19132}"
context_size="${PP_LLAMA_CPP_CONTEXT_SIZE:-8192}"
parallel="${PP_LLAMA_CPP_PARALLEL:-1}"
batch_size="${PP_LLAMA_CPP_BATCH_SIZE:-512}"
ubatch_size="${PP_LLAMA_CPP_UBATCH_SIZE:-128}"
gpu_layers="${PP_LLAMA_CPP_GPU_LAYERS:-all}"
runtime="${PP_LLAMA_CPP_RUNTIME:-cuda}"
embedding_normalization="${PP_LOCAL_NODE_EMBEDDING_NORMALIZATION:-l2}"

usage() {
  cat <<'EOF'
Usage: start_llama_cpp_compute_workers.sh [--start|--stop|--status]

The default action starts both external llama.cpp workers after the read-only
resource gate passes.  --stop is reversible and does not delete model files;
--status only inspects container state.

Environment:
  PP_LLAMA_CPP_RESOURCE_GATE=on|off   (default: on; off is an explicit override)
  PP_LLAMA_CPP_RESOURCE_GATE_DISK_PATH=PATH
  PP_LLAMA_CPP_PYTHON_BIN=python3
EOF
}

for argument in "$@"; do
  case "$argument" in
    --start) action="start" ;;
    --stop) action="stop" ;;
    --status) action="status" ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'unknown argument: %s\n' "$argument" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ "$action" == "status" ]]; then
  "$docker_bin" ps -a --filter name=pp-llama-embedding --filter name=pp-llama-rerank \
    --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
  exit 0
fi

if [[ "$action" == "stop" ]]; then
  "$docker_bin" stop pp-llama-embedding pp-llama-rerank >/dev/null 2>&1 || true
  printf 'llama_cpp_workers=stopped\n'
  exit 0
fi

case "$resource_gate" in
  on)
    if ! command -v "$python_bin" >/dev/null 2>&1; then
      printf 'resource_gate_python_unavailable: %s\n' "$python_bin" >&2
      exit 75
    fi
    repository_root="${PP_REPOSITORY_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
    if [[ -z "$resource_gate_disk_path" ]]; then
      resource_gate_disk_path="$model_root"
    fi
    printf 'resource_gate=on disk_path=%s\n' "$resource_gate_disk_path"
    set +e
    PYTHONPATH="$repository_root${PYTHONPATH:+:$PYTHONPATH}" \
      "$python_bin" -m plastic_promise.release_builder.resource_probe resource-gate \
      --disk-path "$resource_gate_disk_path"
    gate_status=$?
    set -e
    if (( gate_status != 0 )); then
      printf 'llama_cpp_workers_start_deferred=resource_gate status=%s\n' "$gate_status" >&2
      exit "$gate_status"
    fi
    ;;
  off)
    printf 'resource_gate=off explicit_operator_override=1\n' >&2
    ;;
  *)
    printf 'PP_LLAMA_CPP_RESOURCE_GATE must be on or off\n' >&2
    exit 2
    ;;
esac

require_uint() {
  local name="$1" value="$2"
  [[ "$value" =~ ^[1-9][0-9]*$ ]] || {
    printf '%s must be a positive integer\n' "$name" >&2
    exit 2
  }
}

[[ "$image" =~ ^[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$ ]] || {
  printf 'PP_LLAMA_CPP_IMAGE must be an immutable digest reference\n' >&2
  exit 2
}

require_model() {
  local relative="$1" path
  [[ "$relative" != /* && "$relative" != *".."* ]] || {
    printf 'model path must be relative to PP_LLAMA_CPP_MODEL_ROOT: %s\n' "$relative" >&2
    exit 2
  }
  path="$model_root/$relative"
  [[ -f "$path" && ! -L "$path" ]] || {
    printf 'model file missing or unsafe: %s\n' "$path" >&2
    exit 2
  }
  case "$path" in
    *.partial|*.part|*.tmp)
      printf 'refusing incomplete model file: %s\n' "$path" >&2
      exit 2
      ;;
  esac
}

require_uint PP_LLAMA_CPP_EMBEDDING_PORT "$embedding_port"
require_uint PP_LLAMA_CPP_RERANK_PORT "$rerank_port"
require_uint PP_LLAMA_CPP_CONTEXT_SIZE "$context_size"
require_uint PP_LLAMA_CPP_PARALLEL "$parallel"
require_uint PP_LLAMA_CPP_BATCH_SIZE "$batch_size"
require_uint PP_LLAMA_CPP_UBATCH_SIZE "$ubatch_size"
require_model "$embedding_file"
require_model "$rerank_file"

if ! "$docker_bin" image inspect "$image" >/dev/null 2>&1; then
  "$docker_bin" pull "$image"
fi

gpu_args=()
case "$runtime" in
  cuda) gpu_args=(--gpus all) ;;
  cpu) gpu_layers=0 ;;
  *) printf 'PP_LLAMA_CPP_RUNTIME must be cuda or cpu\n' >&2; exit 2 ;;
esac

case "$embedding_normalization" in
  l2) embedding_normalize=2 ;;
  none) embedding_normalize=-1 ;;
  *)
    printf 'PP_LOCAL_NODE_EMBEDDING_NORMALIZATION must be l2 or none\n' >&2
    exit 2
    ;;
esac

common_args=(
  --detach
  --restart unless-stopped
  --network host
  --read-only
  --cap-drop ALL
  --security-opt no-new-privileges
  --mount "type=bind,src=$model_root,dst=/models,readonly"
  "${gpu_args[@]}"
)

"$docker_bin" rm -f pp-llama-embedding pp-llama-rerank >/dev/null 2>&1 || true

"$docker_bin" run "${common_args[@]}" \
  --name pp-llama-embedding \
  --health-cmd "curl -fsS --max-time 5 http://127.0.0.1:$embedding_port/health >/dev/null" \
  --health-interval 10s --health-timeout 6s --health-retries 12 --health-start-period 30s \
  "$image" \
  --model "/models/$embedding_file" \
  --embedding --embd-normalize "$embedding_normalize" \
  --host 127.0.0.1 --port "$embedding_port" \
  --gpu-layers "$gpu_layers" \
  --ctx-size "$context_size" --parallel "$parallel" \
  --batch-size "$batch_size" --ubatch-size "$ubatch_size" >/dev/null

"$docker_bin" run "${common_args[@]}" \
  --name pp-llama-rerank \
  --health-cmd "curl -fsS --max-time 5 http://127.0.0.1:$rerank_port/health >/dev/null" \
  --health-interval 10s --health-timeout 6s --health-retries 12 --health-start-period 30s \
  "$image" \
  --model "/models/$rerank_file" \
  --rerank \
  --host 127.0.0.1 --port "$rerank_port" \
  --gpu-layers "$gpu_layers" \
  --ctx-size "$context_size" --parallel "$parallel" \
  --batch-size "$batch_size" --ubatch-size "$ubatch_size" >/dev/null

printf 'embedding_worker=pp-llama-embedding port=%s model=%s\n' "$embedding_port" "$embedding_file"
printf 'rerank_worker=pp-llama-rerank port=%s model=%s\n' "$rerank_port" "$rerank_file"
