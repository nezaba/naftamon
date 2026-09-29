#!/bin/sh
# Install Naftamon for the current user (Arch or Debian/Ubuntu): build dependencies, build,
# binary in ~/.local/bin, entry + icon in the application launcher.
#   sh install.sh              install / update
#   sh install.sh --uninstall  remove binary, launcher entry and icon (settings are kept)
set -e
cd "$(dirname "$0")"
bin=~/.local/bin/naftamon
apps=~/.local/share/applications
icons=~/.local/share/icons/hicolor/256x256/apps

refresh_launcher() {
    command -v update-desktop-database >/dev/null && update-desktop-database -q "$apps" || true
    command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t ~/.local/share/icons/hicolor || true
}

if [ "$1" = "--uninstall" ]; then
    pkill -x naftamon || true
    rm -f "$bin" "$apps/naftamon.desktop" "$icons/naftamon.png"
    refresh_launcher
    echo "removed (settings kept in ~/.config/naftamon)"
    exit 0
fi

if command -v pacman >/dev/null; then
    sudo pacman -S --needed --noconfirm base-devel qt6-base qt6-multimedia qt6-multimedia-ffmpeg
elif command -v apt >/dev/null; then
    sudo apt install -y build-essential qmake6 qt6-base-dev qt6-multimedia-dev libgl-dev libglx-dev libopengl-dev
else
    echo "unsupported distro: install Qt6 base + multimedia dev packages and qmake6, then rerun" >&2
fi
rm -rf build && mkdir build && cd build
qmake6 ../naftamon.pro && make -j"$(nproc)"

pkill -x naftamon || true  # replace a running copy
mkdir -p ~/.local/bin "$apps" "$icons"
cp naftamon "$bin"
QT_QPA_PLATFORM=offscreen ./naftamon --export-icon "$icons/naftamon.png" 256  # icon is drawn by naftamon
cat > "$apps/naftamon.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Naftamon
GenericName=Monitoring status
Comment=Fast Thruk / Nagios status monitor (Nagstamon compatible)
Exec=$bin
Icon=naftamon
Terminal=false
Categories=System;Monitor;
Keywords=nagios;thruk;naemon;icinga;monitoring;nagstamon;alerts;
StartupWMClass=naftamon
EOF
refresh_launcher
echo "installed: $bin — search \"Naftamon\" in your application launcher"
