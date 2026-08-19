#!/usr/bin/env bash
# One-click deployment for the Plastic Promise knowledge ingestion service.
#
# Usage:
#   deploy/deploy-knowledge-ingest.sh [source_revision]
#
# - Checks out the requested revision (default: current runtime HEAD) in the
#   runtime worktree on a deploy/<service>-<short-sha> branch.
# - Installs/updates the systemd unit, boots the loopback-only service on 9050
#   and waits for /v1/health to report enabled=true.
# - Generates PP_KNOWLEDGE_API_TOKEN on first run (kept in
#   $state/knowledge/knowledge.env, mode 0600, never printed).
# - Keeps MCP untouched; backs everything up and rolls back on failure.
#
# Safe to re-run: idempotent for the unit, env file and git checkout.
set -Eeuo pipefail

runtime=/srv/plastic-promise/runtime
state=/srv/plastic-promise/state
unit_name=plastic-promise-knowledge-ingest.service
unit_file="$runtime/deploy/systemd/$unit_name"
env_file="$state/knowledge/knowledge.env"
port=9050
health_url="http://127.0.0.1:${port}/v1/health"

new_sha="${1:-$(git -C "$runtime" rev-parse HEAD)}"
short_sha="${new_sha:0:12}"
orig_branch=$(git -C "$runtime" rev-parse --abbrev-ref HEAD)
orig_head=$(git -C "$runtime" rev-parse HEAD)
stamp=$(date -u +%Y%m%dT%H%M%SZ)
rollback_dir="$state/backups/knowledge-ingest-rollback/${short_sha}-$stamp"
cutover_started=0

die() {
  printf 'deploy-knowledge-ingest: %s\n' "$*" >&2
  exit 1
}

wait_for_health() {
  local attempt
  for attempt in $(seq 1 30); do
    if curl -fsS --max-time 3 "$health_url" | grep -q '"enabled":true'; then
      return 0
    fi
    sleep 1
  done
  return 1
}

rollback() {
  local line=${1:-unknown}
  trap - ERR
  set +e
  printf 'Deployment failed at line %s; restoring previous state\n' "$line" >&2
  if systemctl is-active --quiet "$unit_name"; then
    sudo systemctl stop "$unit_name"
  fi
  if [ -f "$rollback_dir/$unit_name" ]; then
    sudo install -o root -g root -m 0644 "$rollback_dir/$unit_name" "/etc/systemd/system/$unit_name"
    sudo systemctl daemon-reload
    if systemctl is-enabled --quiet "$unit_name" 2>/dev/null; then
      sudo systemctl enable "$unit_name" >/dev/null 2>&1 || true
    fi
  else
    sudo systemctl disable "$unit_name" >/dev/null 2>&1 || true
  fi
  if [ -d "$rollback_dir/knowledge" ]; then
    sudo rm -rf "$state/knowledge"
    sudo mv "$rollback_dir/knowledge" "$state/knowledge"
  fi
  git -C "$runtime" checkout -q "$orig_head" 2>/dev/null || true
  if [ "$orig_branch" != "HEAD" ]; then
    git -C "$runtime" branch -q -f "$orig_branch" "$orig_head" 2>/dev/null || true
    git -C "$runtime" checkout -q "$orig_branch" 2>/dev/null || true
  fi
  printf 'Rollback complete.\n' >&2
  exit 1
}

trap 'if [ "$cutover_started" = 1 ]; then rollback "$LINENO"; fi' ERR
# ---------- preconditions ----------
test "$(id -un)" = plastic || die "run as plastic (sudo is used internally)"
sudo -n true
test -d "$runtime/.git" || die "runtime is not a git checkout: $runtime"
test -f "$unit_file" || die "unit file missing: $unit_file"
test "$(git -C "$runtime" status --porcelain=v1)" = "" || die "runtime worktree is dirty"
test "$(systemctl is-active plastic-promise-mcp.service)" = active || die "MCP must be active"
test "$(systemctl is-active "$unit_name" || true)" != "activating" || die "ingest unit is stuck activating"
sqlite3 "$state/db/plastic_memory.db" 'PRAGMA integrity_check;' | grep -Fx ok || die "main DB integrity_check failed"

# ---------- backup ----------
mkdir -p "$rollback_dir"
chmod 700 "$rollback_dir"
if [ -f "/etc/systemd/system/$unit_name" ]; then
  sudo cp "/etc/systemd/system/$unit_name" "$rollback_dir/$unit_name"
fi
if [ -d "$state/knowledge" ]; then
  sudo cp -a "$state/knowledge" "$rollback_dir/knowledge"
fi

# ---------- checkout target revision ----------
git -C "$runtime" fetch origin
if ! git -C "$runtime" cat-file -e "$new_sha^{commit}" 2>/dev/null; then
  die "unknown revision: $new_sha"
fi
git -C "$runtime" checkout -q -B "deploy/knowledge-ingest-$short_sha" "$new_sha"
"$runtime/.venv/bin/python" -m compileall -q "$runtime/plastic_promise/knowledge"
"$runtime/.venv/bin/python" -c "import plastic_promise.knowledge.server; print('knowledge.server import OK')"

cutover_started=1

# ---------- env file (idempotent) ----------
sudo install -d -o plastic -g plastic -m 0750 "$state/knowledge"
if [ ! -f "$env_file" ]; then
  token=$(openssl rand -hex 32)
  printf 'PP_KNOWLEDGE_API_TOKEN=%s\n' "$token" | sudo -u plastic bash -c 'umask 077; cat > "$0"' "$env_file"
  unset token
fi
sudo chown plastic:plastic "$env_file"
sudo chmod 0600 "$env_file"
for key in PP_KNOWLEDGE_SYSTEM PP_KNOWLEDGE_STATE_ROOT; do
  if ! grep -q "^${key}=" "$env_file"; then
    case "$key" in
      PP_KNOWLEDGE_SYSTEM) value=shadow ;;
      PP_KNOWLEDGE_STATE_ROOT) value="$state/knowledge" ;;
    esac
    printf '\n%s=%s\n' "$key" "$value" | sudo tee -a "$env_file" >/dev/null
  fi
done
sudo chmod 0600 "$env_file"
grep -q '^PP_KNOWLEDGE_API_TOKEN=.\+' "$env_file" || die "PP_KNOWLEDGE_API_TOKEN missing in $env_file"

# ---------- install & start unit ----------
sudo install -o root -g root -m 0644 "$unit_file" "/etc/systemd/system/$unit_name"
sudo systemd-analyze verify "/etc/systemd/system/$unit_name"
sudo systemctl daemon-reload
sudo systemctl enable "$unit_name" >/dev/null
sudo systemctl restart "$unit_name"

wait_for_health || die "service did not become healthy on port $port"
sudo ss -lnt | grep -F "127.0.0.1:$port" || die "service not listening on 127.0.0.1:$port"
if sudo ss -lnt | grep -Eq "0\.0\.0\.0:$port|\[::\]:$port"; then
  die "service must not bind publicly on port $port"
fi

cutover_started=0

printf 'Knowledge ingest service deployed on %s (revision %s)\n' "$short_sha" "$new_sha"
printf 'Health: %s\n' "$(curl -fsS --max-time 3 "$health_url")"
printf 'Rollback material: %s\n' "$rollback_dir"
