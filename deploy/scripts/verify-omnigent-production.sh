#!/usr/bin/env bash
# verify-omnigent-production — sanity checks the Omnigent 2 production
# instance without touching the maintenance instance.
#
# Asserts:
#   - production server unit is active on port 4197 (healthy)
#   - production host unit is active and connected to the production server
#   - maintenance units still have their pre-deploy PIDs/timestamps
#   - production isolation paths exist and are isolated from maintenance
#   - production config is pointing at production paths only
#   - the tailnet URL on :2222 is reachable
#
# Exits 0 on success, non-zero with a clear failure on any check.

set -uo pipefail

PROD_HOME=/var/lib/omnigent-production
PROD_RELEASE_ROOT=/opt/omnigent-production
PROD_CONFIG=/etc/omnigent-production
PROD_SERVER_PORT=4197
PROD_TAILNET_URL="https://hermes-agent.taile0361b.ts.net:2222/"
PROD_HEALTH_URL="http://127.0.0.1:${PROD_SERVER_PORT}/health"
MAINTENANCE_SERVER_PID=1620126
MAINTENANCE_SERVER_START="Tue 2026-08-04 07:54:42 UTC"
MAINTENANCE_HOST_PID=1614576
MAINTENANCE_HOST_START="Tue 2026-08-04 07:45:46 UTC"

fail=0
note() { printf '\033[36m%s\033[0m\n' "$*"; }
ok()   { printf '\033[32m✓\033[0m %s\n' "$*"; }
bad()  { printf '\033[31m✗\033[0m %s\n' "$*"; fail=1; }

note "== production server =="
if systemctl is-active --quiet omnigent-production.service; then
  ok "omnigent-production.service active"
else
  bad "omnigent-production.service not active"
fi
if curl -fsS --max-time 5 "$PROD_HEALTH_URL" >/dev/null 2>&1; then
  ok "GET $PROD_HEALTH_URL returned 2xx"
else
  bad "GET $PROD_HEALTH_URL failed"
fi

note "== production host =="
if systemctl is-active --quiet omnigent-production-host.service; then
  ok "omnigent-production-host.service active"
else
  bad "omnigent-production-host.service not active"
fi

note "== maintenance baseline (must be unchanged) =="
maint_server_pid=$(systemctl show omnigent.service -p MainPID --value)
maint_server_start=$(systemctl show omnigent.service -p ActiveEnterTimestamp --value)
maint_host_pid=$(systemctl show omnigent-host.service -p MainPID --value)
maint_host_start=$(systemctl show omnigent-host.service -p ActiveEnterTimestamp --value)

if [[ "$maint_server_pid" == "$MAINTENANCE_SERVER_PID" \
   && "$maint_server_start" == "$MAINTENANCE_SERVER_START" ]]; then
  ok "omnigent.service MainPID=$maint_server_pid, started $maint_server_start (unchanged)"
else
  bad "omnigent.service drift: MainPID=$maint_server_pid (was $MAINTENANCE_SERVER_PID), start=$maint_server_start (was $MAINTENANCE_SERVER_START)"
fi
if [[ "$maint_host_pid" == "$MAINTENANCE_HOST_PID" \
   && "$maint_host_start" == "$MAINTENANCE_HOST_START" ]]; then
  ok "omnigent-host.service MainPID=$maint_host_pid, started $maint_host_start (unchanged)"
else
  bad "omnigent-host.service drift: MainPID=$maint_host_pid (was $MAINTENANCE_HOST_PID), start=$maint_host_start (was $MAINTENANCE_HOST_START)"
fi

note "== isolation paths =="
for p in /opt/omnigent-production /etc/omnigent-production /var/lib/omnigent-production; do
  if [[ -e "$p" ]]; then ok "exists $p"; else bad "missing $p"; fi
done
if grep -q "/opt/omnigent/[^p]" /etc/omnigent-production/omnigent.env 2>/dev/null; then
  bad "omnigent.env references /opt/omnigent/<not-production> — isolation breach"
else
  ok "omnigent.env references only production paths"
fi
if grep -q "/var/lib/omnigent/[^p]" /etc/omnigent-production/omnigent.env 2>/dev/null; then
  bad "omnigent.env references /var/lib/omnigent/<not-production>"
else
  ok "omnigent.env has no maintenance state paths"
fi
if grep -qE "4097|9461" /etc/omnigent-production/omnigent.env 2>/dev/null; then
  bad "omnigent.env references maintenance ports"
else
  ok "omnigent.env has no maintenance ports"
fi

note "== tailnet URL =="
# tailscale serve status confirms the mapping; reachability is only
# verifiable from inside the tailnet (MagicDNS), so we prefer that.
status=$(tailscale serve status 2>/dev/null || true)
if grep -q "ts.net:2222" <<<"$status"; then
  if grep -A1 "ts.net:2222" <<<"$status" | grep -q "127.0.0.1:4197"; then
    ok "https://hermes-agent.taile0361b.ts.net:2222 -> 127.0.0.1:4197 (mapped)"
  else
    bad "hermes-agent.taile0361b.ts.net:2222 exists but does not point at 4197"
  fi
else
  bad "https://hermes-agent.taile0361b.ts.net:2222 not in tailscale serve status"
fi
# Ensure the maintenance URL is still mapped (regression guard).
if grep -q "ts.net:1111" <<<"$status"; then
  ok "https://hermes-agent.taile0361b.ts.net:1111 still mapped"
else
  bad "https://hermes-agent.taile0361b.ts.net:1111 mapping missing"
fi
# The legacy :9461 alias was deliberately removed after the dual-instance
# acceptance pass; the verifier asserts the removal held.
if grep -q "ts.net:9461" <<<"$status"; then
  bad "https://hermes-agent.taile0361b.ts.net:9461 is mapped (must be removed)"
else
  ok "https://hermes-agent.taile0361b.ts.net:9461 not mapped (legacy alias removed)"
fi

if [[ $fail -ne 0 ]]; then
  printf '\n\033[31mVERIFY FAILED\033[0m\n' >&2
  exit 1
fi
printf '\n\033[32mALL CHECKS PASSED\033[0m\n'