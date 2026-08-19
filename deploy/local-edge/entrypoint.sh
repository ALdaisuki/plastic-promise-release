#!/bin/sh
# A deliberately tiny image-local entrypoint. The local edge only serves the
# packaged static dashboard; it never reads canonical state, proxy settings,
# credentials, or host files. An optional browser-side ppctl bridge is a
# metadata document only: the browser talks to the explicitly configured
# loopback adapter directly and this image never proxies requests to it.
set -eu

bridge_config_path=/tmp/pp-local-edge-bridge-config.json
bridge_csp_path=/tmp/pp-local-edge-bridge-csp.conf
bridge_endpoint="${PP_LOCAL_EDGE_PPCTL_BRIDGE_ENDPOINT:-}"
bridge_status=disabled
bridge_connect_src=

if [ -n "$bridge_endpoint" ]; then
  # Do not accept localhost (which may resolve beyond loopback), IPv6/other
  # interfaces, credentials, query strings, or arbitrary paths. The only
  # supported browser transport is the fixed local ppctl v1 base path.
  case "$bridge_endpoint" in
    http://127.0.0.1:*/ppctl/v1)
      bridge_port_and_path=${bridge_endpoint#http://127.0.0.1:}
      bridge_port=${bridge_port_and_path%/ppctl/v1}
      ;;
    *)
      printf '%s\n' \
        'PP_LOCAL_EDGE_PPCTL_BRIDGE_ENDPOINT must be http://127.0.0.1:<port>/ppctl/v1' \
        >&2
      exit 64
      ;;
  esac

  case "$bridge_port" in
    ''|0*|*[!0-9]*|??????*)
      printf '%s\n' 'PP_LOCAL_EDGE_PPCTL_BRIDGE_ENDPOINT has an invalid port' >&2
      exit 64
      ;;
  esac

  if [ "$bridge_port" -gt 65535 ]; then
    printf '%s\n' 'PP_LOCAL_EDGE_PPCTL_BRIDGE_ENDPOINT has an invalid port' >&2
    exit 64
  fi

  bridge_status=configured
  bridge_connect_src=" http://127.0.0.1:$bridge_port"
fi

# Both files are generated before nginx parses its configuration. The endpoint
# grammar above contains no JSON or nginx metacharacters, and is intentionally
# non-secret. No default makes a privileged host service reachable.
umask 077
printf '%s\n' "set \$pp_local_edge_bridge_connect_src \"$bridge_connect_src\";" \
  > "$bridge_csp_path"
printf '%s\n' \
  '{' \
  '  "schema_version": "pp-local-edge-ppctl-bridge/v1",' \
  "  \"status\": \"$bridge_status\"," \
  > "$bridge_config_path"
if [ "$bridge_status" = configured ]; then
  printf '%s\n' "  \"endpoint\": \"$bridge_endpoint\"," >> "$bridge_config_path"
else
  printf '%s\n' '  "endpoint": null,' >> "$bridge_config_path"
fi
printf '%s\n' \
  '  "operations": ["inspect", "preview"],' \
  '  "method": "POST",' \
  '  "content_type": "application/json"' \
  '}' \
  >> "$bridge_config_path"

case "${1:-}" in
  --help|-h)
    printf '%s\n' 'plastic-promise-local-edge: static, non-authoritative dashboard edge'
    exit 0
    ;;
  '')
    exec nginx -g 'daemon off;'
    ;;
  *)
    exec "$@"
    ;;
esac
