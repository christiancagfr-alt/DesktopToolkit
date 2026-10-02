#!/usr/bin/env bash
# Build Desktop Toolkit portable tree for Linux x86_64.
# Output: dist/release/DesktopToolkit-<ver>-linux-x86_64.zip (+ .sha256)
set -euo pipefail
cd "$(dirname "$0")"
VER="$(tr -d ' \r\n' < VERSION)"
echo "Building DesktopToolkit ${VER} for Linux…"

python3 -m pip install -r requirements.txt pyinstaller --quiet
rm -rf build/linux dist/DesktopToolkit

python3 -m PyInstaller --noconfirm --windowed --name DesktopToolkit \
  --icon logo.ico \
  --add-data "assets:assets" \
  --add-data "logo.png:." \
  --add-data "logo.ico:." \
  --add-data "cloudflare:cloudflare" \
  --add-data "VERSION:." \
  --hidden-import mss \
  --hidden-import imageio_ffmpeg \
  --hidden-import cv2 \
  --hidden-import numpy \
  --hidden-import sounddevice \
  --hidden-import websockets \
  --hidden-import PyQt6.QtMultimedia \
  --hidden-import weather \
  --hidden-import notebook_store \
  --hidden-import notebook_ui \
  --hidden-import notebook_sync \
  --hidden-import file_organizer \
  --hidden-import file_organizer_ui \
  --hidden-import rustdesk_bridge \
  --hidden-import win_topmost \
  --hidden-import p2p_transfer \
  --hidden-import p2p_ui \
  --hidden-import lan_remote \
  --hidden-import remote_lan_ui \
  --hidden-import pynput \
  --hidden-import PIL \
  --hidden-import ui_platform \
  --exclude-module torch \
  --exclude-module tensorflow \
  --exclude-module matplotlib \
  main.py

OUT="dist/release"
mkdir -p "$OUT"
APPDIR="dist/DesktopToolkit"
if [[ ! -d "$APPDIR" ]]; then
  echo "ERROR: dist/DesktopToolkit missing after PyInstaller"
  ls -la dist || true
  exit 1
fi

# Ensure a launcher script exists for desktop users
if [[ ! -f "$APPDIR/DesktopToolkit" && ! -f "$APPDIR/DesktopToolkit.bin" ]]; then
  echo "WARNING: binary name unexpected"; ls -la "$APPDIR" | head
fi

ZIP="$OUT/DesktopToolkit-${VER}-linux-x86_64.zip"
rm -f "$ZIP"
(
  cd dist
  zip -r -9 "../$ZIP" DesktopToolkit
)
sha256sum "$ZIP" | awk '{print $1}' > "${ZIP}.sha256"
echo "OK: $ZIP"
echo "Linux run: unzip, then ./DesktopToolkit/DesktopToolkit"
echo "Needs: X11 (or XWayland), PortAudio/Pulse for mic, ffmpeg libs bundled via imageio-ffmpeg"
