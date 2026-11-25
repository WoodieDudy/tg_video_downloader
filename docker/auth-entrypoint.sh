#!/usr/bin/env bash
set -euo pipefail

PROFILE_DIR="${CHROME_PROFILE_DIR:-/data/chrome-profile}"
DISPLAY_NUM="${DISPLAY:-:99}"
VNC_PORT="${VNC_PORT:-5900}"
NOVNC_PORT="${NOVNC_PORT:-6080}"

mkdir -p "${PROFILE_DIR}"

# Clean up any leftover Chromium lock files from previous ungraceful exits
rm -f "${PROFILE_DIR}/SingletonLock" "${PROFILE_DIR}/SingletonSocket" "${PROFILE_DIR}/SingletonCookie"

CHROME_BIN="$(find /ms-playwright -maxdepth 4 -type f -name chrome 2>/dev/null | grep -v headless | head -n 1 || true)"
if [[ -z "${CHROME_BIN}" ]]; then
  CHROME_BIN="$(python3 -c 'from playwright.sync_api import sync_playwright; p = sync_playwright().start(); print(p.chromium.executable_path); p.stop()')"
fi

echo "Using Chromium binary: ${CHROME_BIN}"
echo "Starting Xvfb on ${DISPLAY_NUM}..."
Xvfb "${DISPLAY_NUM}" -screen 0 1366x900x24 -ac +extension RANDR >/tmp/xvfb.log 2>&1 &
XVFB_PID=$!
export DISPLAY="${DISPLAY_NUM}"

sleep 1

openbox >/tmp/openbox.log 2>&1 &
OPENBOX_PID=$!

VNC_AUTH_ARGS=(-nopw)
if [[ -n "${VNC_PASSWORD:-}" ]]; then
  x11vnc -storepasswd "${VNC_PASSWORD}" /tmp/vncpass >/dev/null
  VNC_AUTH_ARGS=(-rfbauth /tmp/vncpass)
fi

x11vnc -display "${DISPLAY_NUM}" -forever -shared -rfbport "${VNC_PORT}" "${VNC_AUTH_ARGS[@]}" >/tmp/x11vnc.log 2>&1 &
VNC_PID=$!

websockify --web=/usr/share/novnc "${NOVNC_PORT}" "localhost:${VNC_PORT}" >/tmp/novnc.log 2>&1 &
NOVNC_PID=$!

cleanup() {
  echo "Shutting down auth browser cleanly..."
  if [[ -n "${CHROME_PID:-}" ]] && kill -0 "${CHROME_PID}" 2>/dev/null; then
    kill -TERM "${CHROME_PID}" 2>/dev/null || true
    wait "${CHROME_PID}" 2>/dev/null || true
  fi
  kill "${NOVNC_PID}" "${VNC_PID}" "${OPENBOX_PID}" "${XVFB_PID}" 2>/dev/null || true
}
trap cleanup SIGTERM SIGINT EXIT

echo "=================================================================="
echo "noVNC login UI ready on port ${NOVNC_PORT} (open /vnc.html)"
echo "Log into Google and visit YouTube, then stop this container."
echo "=================================================================="

"${CHROME_BIN}" \
  --user-data-dir="${PROFILE_DIR}" \
  --no-sandbox \
  --disable-dev-shm-usage \
  --no-first-run \
  --no-default-browser-check \
  --disable-blink-features=AutomationControlled \
  --window-size=1366,900 \
  --start-maximized \
  "https://accounts.google.com/" \
  "https://www.youtube.com/" &
CHROME_PID=$!

wait "${CHROME_PID}"
