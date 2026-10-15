#!/usr/bin/env bash
set -euo pipefail

mkdir -p "$HOME" "$XDG_RUNTIME_DIR" "$(dirname "$NAFTAMON_CONFIG")"
chmod 700 "$XDG_RUNTIME_DIR" "$(dirname "$NAFTAMON_CONFIG")"

if [[ ! -f "$NAFTAMON_CONFIG" ]]; then
    cat > "$NAFTAMON_CONFIG" <<'EOF'
[General]
show_window_at_start=true
notification=false

[servers]
size=1
1\name=Thruk de prueba
1\url=http://thruk:18765/thruk/cgi-bin
1\username=admin
1\password=secret
EOF
    chmod 600 "$NAFTAMON_CONFIG"
fi

# docker compose run dev ... runs commands without starting a display server.
if (( $# )); then
    exec "$@"
fi

Xvfb "$DISPLAY" -screen 0 1440x900x24 -nolisten tcp &
xvfb_pid=$!
for _ in {1..50}; do
    if xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then break; fi
    sleep 0.1
done
xdpyinfo -display "$DISPLAY" >/dev/null

dbus-run-session -- openbox-session &
openbox_pid=$!
x11vnc -display "$DISPLAY" -forever -shared -localhost -nopw -rfbport 5900 &
vnc_pid=$!
websockify --web=/usr/share/novnc 0.0.0.0:6080 127.0.0.1:5900 &
websockify_pid=$!
xterm -title 'NaftaMon lab' &

trap 'kill "$xvfb_pid" "$openbox_pid" "$vnc_pid" "$websockify_pid" 2>/dev/null || true' EXIT
wait -n "$xvfb_pid" "$openbox_pid" "$vnc_pid" "$websockify_pid"
