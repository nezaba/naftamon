#!/bin/sh
# Build and run the self-checks against the mock Thruk, with and without Thruk's wait feature.
set -e
cd "$(dirname "$0")/.."
mkdir -p build/tests
(cd build/tests && qmake6 ../../tests/tests.pro >/dev/null && make -s -j"$(nproc)")
scale_gateway_test=${NAFTAMON_SCALE_GATEWAY_TEST:-0}
unset NAFTAMON_SCALE_GATEWAY_TEST
for mode in "" --no-wait --basic; do
    python3 tests/mock_thruk.py 18765 $mode &
    pid=$!
    sleep 0.5
    echo "== mock Thruk ${mode:-with wait feature}"
    QT_QPA_PLATFORM=offscreen MOCK_URL=http://127.0.0.1:18765/thruk WEBHOOK_URL=http://webhooks:8080/webhook \
        build/tests/naftamon-tests || { kill $pid; exit 1; }
    kill $pid
done
if [ "$scale_gateway_test" = "1" ]; then
    python3 tests/mock_thruk.py 18765 &
    pid=$!
    trap 'kill "$pid" 2>/dev/null || true' EXIT
    sleep 0.5
    echo "== Desktop Gateway 165950-status gzip scale fixture"
    NAFTAMON_SCALE_GATEWAY_TEST=1 QT_QPA_PLATFORM=offscreen \
        MOCK_URL=http://127.0.0.1:18765/thruk WEBHOOK_URL=http://webhooks:8080/webhook \
        build/tests/naftamon-tests
    kill "$pid"
    wait "$pid" 2>/dev/null || true
    trap - EXIT
fi
