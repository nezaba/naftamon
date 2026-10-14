---
name: naftamon-docker-lab
description: Build, test, and manually inspect NaftaMon in its isolated Docker Desktop lab. Use for changes to this repository that need Qt6 compilation, mock Thruk tests, webhook sink inspection, or noVNC UI checks.
---

# NaftaMon Docker lab

Use `compose.dev.yaml` from the repository root. The `dev` image contains Ubuntu 24.04, qmake6, Qt6 Widgets/Network/Multimedia, C++17 tools, and a virtual X desktop. The repository is bind mounted at `/workspace`; `build/` and `/lab-state` are named volumes. Keep the lab pointed at the bundled Thruk mock and use only its `admin` / `secret` test account. Do not import local Nagstamon files or use production URLs or credentials.

1. Start with `docker compose -f compose.dev.yaml up --build -d` and check `docker compose -f compose.dev.yaml ps`.
2. Compile with `docker compose -f compose.dev.yaml exec dev bash -lc 'mkdir -p build && cd build && qmake6 ../naftamon.pro && make -j"$(nproc)"'`.
3. Run the headless suite with `docker compose -f compose.dev.yaml run --rm -T dev bash tests/run.sh`. This uses the existing mock in three authentication/wait modes and requires no display.
4. For UI work, launch `./build/naftamon` inside `dev` and inspect `http://localhost:6080/vnc.html?autoconnect=1` from Windows. The app uses `/lab-state/config/naftamon.ini`, seeded for the mock Thruk service.
5. Check both private services with `docker compose -f compose.dev.yaml exec dev python3 docker/lab-smoke.py`. Inspect their output with `docker compose -f compose.dev.yaml logs -f thruk webhooks` and app output with `docker compose -f compose.dev.yaml exec dev cat /lab-state/naftamon.log` when launched as documented in README.
6. Stop with `docker compose -f compose.dev.yaml down`. Use `down -v` only when intentionally discarding lab builds and configuration.

The `webhooks` service is only an HTTP JSON sink at `http://webhooks:8080/webhook` on the Compose network; it does not implement the future Webhook Gateway. Its received JSON appears in `docker compose logs webhooks`. Neither `webhooks` nor `thruk` publishes a Windows port.

Before reporting Docker validation, verify the actual build and test result. If Docker Desktop is unavailable, report that explicitly and do not claim container or UI validation.
