#!/bin/sh
# Install build dependencies (Arch or Debian/Ubuntu), build from scratch, install to ~/.local/bin.
set -e
cd "$(dirname "$0")"
if command -v pacman >/dev/null; then
    sudo pacman -S --needed --noconfirm base-devel qt6-base qt6-multimedia qt6-multimedia-ffmpeg
elif command -v apt >/dev/null; then
    sudo apt install -y build-essential qmake6 qt6-base-dev qt6-multimedia-dev libgl-dev libglx-dev libopengl-dev
else
    echo "unsupported distro: install Qt6 base + multimedia dev packages and qmake6, then rerun" >&2
fi
rm -rf build && mkdir build && cd build
qmake6 ../naftamon.pro && make -j"$(nproc)"
mkdir -p ~/.local/bin && cp naftamon ~/.local/bin/
echo "installed: ~/.local/bin/naftamon"
