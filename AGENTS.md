# Repository instructions

Use the local [NaftaMon Docker lab skill](.agents/skills/naftamon-docker-lab/SKILL.md) for development in this repository.

Validate code and build environment changes through `compose.dev.yaml`: build the Ubuntu 24.04 development image, compile the Qt6 application, and run `tests/run.sh` in the development container. For UI changes, inspect the running application through noVNC. Keep all tests and manual checks on the bundled Thruk mock and isolated lab configuration; never use production URLs or credentials.

If Docker Desktop cannot run in the current environment, state which Docker checks remain unverified. Do not substitute a host build for Docker validation or claim the lab is validated without running it.
