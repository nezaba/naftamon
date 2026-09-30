#!/bin/sh
# Build and run the self-checks against the mock Thruk, with and without Thruk's wait feature.
set -e
cd "$(dirname "$0")/.."
mkdir -p build/tests
(cd build/tests && qmake6 ../../tests/tests.pro >/dev/null && make -s -j"$(nproc)")
for mode in "" --no-wait --basic; do
    python3 tests/mock_thruk.py 18765 $mode &
    pid=$!
    sleep 0.5
    echo "== mock Thruk ${mode:-with wait feature}"
    MOCK_URL=http://127.0.0.1:18765/thruk build/tests/naftamon-tests || { kill $pid; exit 1; }
    kill $pid
done

# keyring module against a private D-Bus + gnome-keyring-daemon under build/ (never the real keyring)
if command -v gnome-keyring-daemon >/dev/null && command -v dbus-daemon >/dev/null; then
    K=$PWD/build/tests/keyring
    rm -rf "$K" && mkdir -p "$K/run" "$K/data" "$K/home" && chmod 700 "$K/run"
    export HOME="$K/home" XDG_RUNTIME_DIR="$K/run" XDG_DATA_HOME="$K/data" DBUS_SESSION_BUS_ADDRESS="unix:path=$K/run/bus"
    dbus-daemon --session --address="$DBUS_SESSION_BUS_ADDRESS" --nofork --nopidfile &
    bus=$!
    sleep 0.5
    printf testpw | gnome-keyring-daemon --foreground --unlock --components=secrets >/dev/null 2>&1 &
    ring=$!
    sleep 1.5
    echo "== keyring"
    rc=0
    KEYRING_TEST=1 build/tests/naftamon-tests || rc=1
    kill $ring $bus
    exit $rc
fi
