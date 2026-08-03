#!/usr/bin/env bash
set -euo pipefail

UPSTREAM_SHA=35519fb04743f66b30cac8a40695d5d72fa163ea
SOURCE_DIR=/home/hermes/workspace/repos/omnigent-clean-0.7-pi
DEPLOY_DIR="$SOURCE_DIR/deploy/clean-0.7"
UV=/home/hermes/.local/bin/uv
UV_EXCLUDE_NEWER=2026-08-03
PI_VERSION=0.83.0
OMNIROUTE_ENV=/home/hermes/.omniroute/omniroute-free.env
LOCAL_URL=http://127.0.0.1:4097
PUBLIC_URL=https://hermes-agent.taile0361b.ts.net:9461/
PHASE=preflight
UNITS_INSTALLED=0

failure() {
  status=$?
  trap - ERR
  printf 'Installation failed during phase: %s\n' "$PHASE" >&2
  if [[ $UNITS_INSTALLED -eq 1 ]]; then
    systemctl status omnigent.service omnigent-host.service --no-pager || true
    journalctl -u omnigent.service -u omnigent-host.service -n 200 --no-pager || true
  fi
  exit "$status"
}
trap failure ERR

wait_for_url() {
  url=$1
  attempts=$2
  for _ in $(seq 1 "$attempts"); do
    if curl --silent --show-error --fail --max-time 5 "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  printf 'Timed out waiting for %s\n' "$url" >&2
  return 1
}

if [[ $(id -u) -ne 0 ]]; then
  printf 'Run as root: sudo bash %s\n' "$0" >&2
  exit 1
fi
[[ -x "$UV" ]] || { printf 'Missing executable uv: %s\n' "$UV" >&2; exit 1; }
[[ -r "$OMNIROUTE_ENV" ]] || {
  printf 'Cannot read existing OmniRoute environment: %s\n' "$OMNIROUTE_ENV" >&2
  exit 1
}
[[ $(git -C "$SOURCE_DIR" rev-parse HEAD) == "$UPSTREAM_SHA" ]] || {
  printf 'Source HEAD is not the verified official v0.7.0 commit.\n' >&2
  exit 1
}
[[ $(git -C "$SOURCE_DIR" rev-parse 'v0.7.0^{commit}') == "$UPSTREAM_SHA" ]] || {
  printf 'Local v0.7.0 tag does not resolve to the verified upstream commit.\n' >&2
  exit 1
}

PYTHON312=$(runuser -u hermes -- "$UV" python find 3.12)
[[ -x "$PYTHON312" && "$PYTHON312" != /root/* ]] || {
  printf 'uv returned an inaccessible or root-owned Python: %s\n' "$PYTHON312" >&2
  exit 1
}
[[ $(runuser -u hermes -- "$PYTHON312" -c 'import sys; print(sys.version_info[:2])') == '(3, 12)' ]] || {
  printf 'Selected interpreter is not Python 3.12: %s\n' "$PYTHON312" >&2
  exit 1
}

set -a
# shellcheck disable=SC1090
source "$OMNIROUTE_ENV"
set +a
: "${OMNIROUTE_API_KEY:?OMNIROUTE_API_KEY is absent from the existing OmniRoute environment}"
curl --silent --show-error --fail --max-time 10 http://127.0.0.1:20128/v1/models \
  -H "Authorization: Bearer $OMNIROUTE_API_KEY" |
  "$PYTHON312" -c 'import json,sys; assert "custom/best-coding" in {m.get("id") for m in json.load(sys.stdin).get("data", [])}'
tailscale serve status | grep -A1 -F 'https://hermes-agent.taile0361b.ts.net:9461' |
  grep -F '|-- / proxy http://127.0.0.1:4097' >/dev/null

PHASE=cleanup
systemctl disable --now omnigent-eval-web.service omnigent-eval-host.service 2>/dev/null || true
systemctl disable --now omnigent.service omnigent-host.service 2>/dev/null || true
systemctl disable --now omnigent-updater.service 2>/dev/null || true
pkill -TERM -f '/99a6327debde17029664ef0867c68c1993789251/.*omnigent' 2>/dev/null || true
sleep 1
rm -rf /etc/systemd/system/omnigent-eval-web.service \
  /etc/systemd/system/omnigent-eval-host.service \
  /etc/systemd/system/omnigent-eval-web.service.d \
  /etc/systemd/system/omnigent-eval-host.service.d \
  /etc/systemd/system/omnigent-eval-*.service.d \
  /etc/systemd/system/omnigent-updater.service \
  /etc/systemd/system/omnigent-updater.service.d
rm -rf /home/hermes/workspace/deployments/omnigent/releases/99a6327debde17029664ef0867c68c1993789251 \
  /home/hermes/.omnigent-0.7-99a6327d \
  /home/hermes/.omnigent-99a6327d
rm -f /home/hermes/workspace/repos/omnigent-eval-rebuild-upstream-0.7/*99a6327d*.whl \
  /home/hermes/workspace/repos/omnigent-eval-rebuild-upstream-0.7/*99a6327d*cutover* \
  /home/hermes/workspace/repos/omnigent-eval-rebuild-upstream-0.7/*99a6327d*resume* \
  /home/hermes/workspace/repos/omnigent-eval-rebuild-upstream-0.7/*99a6327d*rollback* 2>/dev/null || true
systemctl daemon-reload
systemctl reset-failed

PHASE=directories
rm -rf /opt/omnigent /etc/omnigent /var/lib/omnigent
install -d -o hermes -g hermes -m 0755 /opt/omnigent
install -d -o root -g hermes -m 0750 /etc/omnigent /etc/omnigent/agents
install -d -o hermes -g hermes -m 0750 /var/lib/omnigent
install -d -o hermes -g hermes -m 0700 /var/lib/omnigent/home \
  /var/lib/omnigent/home/.config /var/lib/omnigent/home/.local \
  /var/lib/omnigent/home/.local/share /var/lib/omnigent/home/.cache
install -d -o hermes -g hermes -m 0750 /var/lib/omnigent/artifacts \
  /var/lib/omnigent/logs /var/lib/omnigent/uv-cache /var/lib/omnigent/npm-cache

PHASE=python-install
runuser -u hermes -- "$UV" venv --python "$PYTHON312" /opt/omnigent/venv
runuser -u hermes -- env UV_CACHE_DIR=/var/lib/omnigent/uv-cache \
  "$UV" pip install --python /opt/omnigent/venv/bin/python \
  --exclude-newer "$UV_EXCLUDE_NEWER" 'omnigent==0.7.0'
runuser -u hermes -- /opt/omnigent/venv/bin/python --version
runuser -u hermes -- /opt/omnigent/venv/bin/python -c \
  'import importlib.metadata, omnigent; from omnigent.version import VERSION; assert VERSION == importlib.metadata.version("omnigent") == "0.7.0"'
runuser -u hermes -- namei -l /opt/omnigent/venv/bin/python
if readlink -f /opt/omnigent/venv/bin/python | grep -q '^/root/'; then
  printf 'Virtual environment points into /root.\n' >&2
  exit 1
fi

PHASE=pi-install
install -d -o hermes -g hermes -m 0755 /opt/omnigent/pi
runuser -u hermes -- env NPM_CONFIG_CACHE=/var/lib/omnigent/npm-cache \
  npm install --prefix /opt/omnigent/pi "@earendil-works/pi-coding-agent@$PI_VERSION"
PI_BIN=/opt/omnigent/pi/node_modules/.bin/pi
[[ -x "$PI_BIN" ]] || { printf 'Pi binary was not installed at %s\n' "$PI_BIN" >&2; exit 1; }
runuser -u hermes -- "$PI_BIN" --version

PHASE=configuration
install -o root -g hermes -m 0640 "$DEPLOY_DIR/config.yaml" /etc/omnigent/config.yaml
install -d -o root -g hermes -m 0750 /etc/omnigent/agents/pi
install -o root -g hermes -m 0640 "$DEPLOY_DIR/pi/config.yaml" /etc/omnigent/agents/pi/config.yaml
umask 0077
{
  printf 'HOME=/var/lib/omnigent/home\n'
  printf 'XDG_CONFIG_HOME=/var/lib/omnigent/home/.config\n'
  printf 'XDG_DATA_HOME=/var/lib/omnigent/home/.local/share\n'
  printf 'XDG_CACHE_HOME=/var/lib/omnigent/home/.cache\n'
  printf 'OMNIGENT_CONFIG_HOME=/etc/omnigent\n'
  printf 'OMNIGENT_DATA_DIR=/var/lib/omnigent\n'
  printf 'OMNIGENT_WS_ALLOWED_ORIGINS=https://hermes-agent.taile0361b.ts.net:9461\n'
  printf 'OMNIGENT_ACCOUNTS_BASE_URL=https://hermes-agent.taile0361b.ts.net:9461\n'
  printf 'OMNIGENT_PI_PATH=%s\n' "$PI_BIN"
  printf 'PATH=/opt/omnigent/venv/bin:/opt/omnigent/pi/node_modules/.bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n'
  printf 'OMNIROUTE_API_KEY=%s\n' "$OMNIROUTE_API_KEY"
} > /etc/omnigent/omnigent.env
chown root:hermes /etc/omnigent/omnigent.env
chmod 0640 /etc/omnigent/omnigent.env

runuser -u hermes -- env -i HOME=/var/lib/omnigent/home \
  XDG_CONFIG_HOME=/var/lib/omnigent/home/.config \
  XDG_DATA_HOME=/var/lib/omnigent/home/.local/share \
  XDG_CACHE_HOME=/var/lib/omnigent/home/.cache \
  OMNIGENT_CONFIG_HOME=/etc/omnigent OMNIGENT_DATA_DIR=/var/lib/omnigent \
  OMNIGENT_PI_PATH="$PI_BIN" OMNIROUTE_API_KEY="$OMNIROUTE_API_KEY" \
  PATH=/opt/omnigent/venv/bin:/opt/omnigent/pi/node_modules/.bin:/usr/local/bin:/usr/bin:/bin \
  /opt/omnigent/venv/bin/python -c \
  'from omnigent.pi_native_credentials import resolve_pi_native_provider; p=resolve_pi_native_provider(); assert p and p.provider_id == "omnigent" and p.base_url == "http://127.0.0.1:20128/v1" and p.api == "openai-completions" and p.model == "custom/best-coding"'
runuser -u hermes -- env PATH=/opt/omnigent/venv/bin:/opt/omnigent/pi/node_modules/.bin:/usr/local/bin:/usr/bin:/bin \
  OMNIGENT_PI_PATH="$PI_BIN" /opt/omnigent/venv/bin/python -c \
  'from omnigent.onboarding.harness_readiness import harness_is_configured; assert harness_is_configured("pi")'
runuser -u hermes -- /opt/omnigent/venv/bin/omnigent --help >/dev/null
runuser -u hermes -- /opt/omnigent/venv/bin/omnigent server --help >/dev/null
runuser -u hermes -- /opt/omnigent/venv/bin/omnigent host --help >/dev/null

PHASE=unit-install
install -o root -g root -m 0644 "$DEPLOY_DIR/omnigent.service" /etc/systemd/system/omnigent.service
install -o root -g root -m 0644 "$DEPLOY_DIR/omnigent-host.service" /etc/systemd/system/omnigent-host.service
systemd-analyze verify /etc/systemd/system/omnigent.service /etc/systemd/system/omnigent-host.service
systemctl daemon-reload
UNITS_INSTALLED=1

PHASE=server-start
systemctl enable --now omnigent.service
wait_for_url "$LOCAL_URL/health" 90
curl --silent --show-error --fail --max-time 5 "$LOCAL_URL/health"

PHASE=host-start
systemctl enable --now omnigent-host.service
for _ in $(seq 1 90); do
  if curl --silent --show-error --fail --max-time 5 "$LOCAL_URL/v1/hosts" |
    /opt/omnigent/venv/bin/python -c \
      'import json,sys; assert any(h.get("status") == "online" for h in json.load(sys.stdin)["hosts"])' 2>/dev/null; then
    break
  fi
  sleep 2
done
curl --silent --show-error --fail --max-time 5 "$LOCAL_URL/v1/hosts" |
  /opt/omnigent/venv/bin/python -c \
    'import json,sys; assert any(h.get("status") == "online" for h in json.load(sys.stdin)["hosts"])'

PHASE=public-verification
wait_for_url "$PUBLIC_URL" 60
systemctl is-enabled --quiet omnigent.service omnigent-host.service
systemctl is-active --quiet omnigent.service omnigent-host.service
[[ -f /var/lib/omnigent/chat.db ]]
[[ ! -e /etc/systemd/system/omnigent-eval-web.service ]]
[[ ! -e /etc/systemd/system/omnigent-eval-host.service ]]
[[ ! -e /etc/systemd/system/omnigent-updater.service ]]
trap - ERR
printf 'Clean Omnigent 0.7 deployment is active.\n'
