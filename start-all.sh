#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_DIR="$SCRIPT_DIR/.interop/.pid"
NO_NEKO=0
STATUS_ONLY=0

for arg in "$@"; do
  case "$arg" in
    --no-neko) NO_NEKO=1 ;;
    --status)  STATUS_ONLY=1 ;;
  esac
done

mkdir -p "$PID_DIR"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

cleanup() {
  echo ""
  echo -e "${YELLOW}[launcher] Shutting down all services...${NC}"
  for pidfile in "$PID_DIR"/*.pid; do
    [ -f "$pidfile" ] || continue
    local name=$(basename "$pidfile" .pid)
    local pid=$(cat "$pidfile")
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      echo -e "  ${GREEN}stopped${NC} $name (pid=$pid)"
    fi
    rm -f "$pidfile"
  done
  echo -e "${GREEN}[launcher] All services stopped.${NC}"
}
trap cleanup EXIT INT TERM

check_port() {
  local port=$1
  if command -v ss &>/dev/null; then
    ss -tlnp 2>/dev/null | grep -q ":$port " && return 0
  elif command -v netstat &>/dev/null; then
    netstat -tlnp 2>/dev/null | grep -q ":$port " && return 0
  fi
  return 1
}

wait_for_port() {
  local port=$1
  local timeout=${2:-5}
  local waited=0
  while [ $waited -lt $timeout ]; do
    if check_port "$port"; then
      return 0
    fi
    sleep 0.5
    waited=$((waited + 1))
  done
  return 1
}

start_service() {
  local name=$1
  local cmd=$2
  local pidfile="$PID_DIR/$name.pid"

  echo -e "${GREEN}[launcher] Starting $name...${NC}"
  eval "$cmd" &
  local pid=$!
  echo "$pid" > "$pidfile"
  sleep 0.5
  if kill -0 "$pid" 2>/dev/null; then
    echo -e "  ${GREEN}OK${NC} $name (pid=$pid)"
  else
    echo -e "  ${RED}FAILED${NC} $name"
    return 1
  fi
}

status_all() {
  echo "=== agent-interop services ==="
  for pidfile in "$PID_DIR"/*.pid; do
    [ -f "$pidfile" ] || continue
    local name=$(basename "$pidfile" .pid)
    local pid=$(cat "$pidfile")
    if kill -0 "$pid" 2>/dev/null; then
      echo -e "  ${GREEN}●${NC} $name (pid=$pid)"
    else
      echo -e "  ${RED}○${NC} $name (pid=$pid, dead)"
    fi
  done
  if [ -z "$(ls -A "$PID_DIR" 2>/dev/null)" ]; then
    echo "  No services running."
  fi
}

if [ "$STATUS_ONLY" -eq 1 ]; then
  status_all
  exit 0
fi

# Start services
cd "$SCRIPT_DIR"

start_service "event-bus" "npx tsx bridge/event-bus.ts" || true

echo -e "${YELLOW}[launcher] Waiting for event-bus (port 48970)...${NC}"
if wait_for_port 48970 10; then
  echo -e "  ${GREEN}OK${NC} event-bus is ready"
else
  echo -e "  ${YELLOW}WARN${NC} event-bus not detected on port 48970, continuing anyway"
fi

if [ "$NO_NEKO" -eq 0 ]; then
  start_service "neko-adapter" "python bridge/neko_adapter.py" || true
fi

echo ""
echo -e "${GREEN}=== All services started ===${NC}"
status_all
echo ""
echo "Press Ctrl+C to stop all services."

# Wait forever
wait
