#!/bin/sh
# Install Naftamon for the current user (Arch or Debian/Ubuntu): build dependencies, build,
# binary in ~/.local/bin, entry + icon in the application launcher.
#   sh install.sh              install / update
#   sh install.sh --uninstall  remove binary, launcher entry and icon (settings are kept)
#   sh install.sh --update [--no-restart]
#                              used by "Check for updates": skip dependencies if present; without
#                              --no-restart it also restarts Naftamon (older app versions rely on that)
set -e
cd "$(dirname "$0")"
bin=~/.local/bin/naftamon
apps=~/.local/share/applications
icons=~/.local/share/icons/hicolor/256x256/apps  # used by older versions, cleaned up below
data=~/.local/share/naftamon

refresh_launcher() {
    command -v update-desktop-database >/dev/null && update-desktop-database -q "$apps" || true
    command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t ~/.local/share/icons/hicolor || true
}

if [ "$1" = "--uninstall" ]; then
    pkill -x naftamon || true
    rm -rf "$bin" "$apps/naftamon.desktop" "$icons/naftamon.png" "$data"
    refresh_launcher
    echo "removed (settings kept in ~/.config/naftamon)"
    exit 0
fi

if [ "$1" = "--update" ] && command -v qmake6 >/dev/null; then
    :  # dependencies already installed
elif command -v pacman >/dev/null; then
    sudo pacman -S --needed --noconfirm base-devel qt6-base qt6-multimedia qt6-multimedia-ffmpeg
elif command -v apt >/dev/null; then
    sudo apt install -y build-essential qmake6 qt6-base-dev qt6-multimedia-dev libgl-dev libglx-dev libopengl-dev
else
    echo "unsupported distro: install Qt6 base + multimedia dev packages and qmake6, then rerun" >&2
fi
rm -rf build && mkdir build && cd build
echo "@@build"
qmake6 ../naftamon.pro ${NAFTAMON_COMMIT:+NAFTAMON_COMMIT=$NAFTAMON_COMMIT} && make -j"$(nproc)"

echo "@@install"  # progress marker for the in-app updater
mkdir -p ~/.local/bin "$apps" "$data"
# never overwrite a running binary in place ("Text file busy"): write a new file, rename over the old
install -m 755 naftamon "$bin.new" && mv -f "$bin.new" "$bin"
# icon is drawn by naftamon; the file name carries a hash of its content so desktops that cache
# icons (GNOME keeps them until logout) pick up a changed icon right away
QT_QPA_PLATFORM=offscreen ./naftamon --export-icon "$data/icon.png" 256
icon="$data/naftamon-$(md5sum "$data/icon.png" | cut -c1-8).png"
find "$data" -name 'naftamon-*.png' ! -path "$icon" -delete
mv -f "$data/icon.png" "$icon"
rm -f "$icons/naftamon.png"
cat > "$apps/naftamon.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Naftamon
GenericName=Monitoring status
Comment=Fast Thruk / Nagios status monitor (Nagstamon compatible)
Exec=$bin
Icon=$icon
Terminal=false
Categories=System;Monitor;
Keywords=nagios;thruk;naemon;icinga;monitoring;nagstamon;alerts;
StartupWMClass=naftamon
EOF
refresh_launcher
echo "installed: $bin — search \"Naftamon\" in your application launcher"
[ "$2" = "--no-restart" ] && exit 0  # the app restarts itself
# stop a running copy (it still runs the old binary) and wait until it is gone
if pkill -x naftamon; then
    i=0; while pgrep -x naftamon >/dev/null && [ $i -lt 50 ]; do sleep 0.1; i=$((i + 1)); done
fi
if [ "$1" = "--update" ]; then
    setsid -f "$bin" >/dev/null 2>&1 </dev/null  # restart, detached from this terminal
    echo "Naftamon restarted"
fi
