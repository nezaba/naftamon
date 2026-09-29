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
