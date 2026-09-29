#!/usr/bin/env bash
# Build Desktop Toolkit .app on macOS (Apple Silicon or Intel).
# Outputs:
#   dist/release/DesktopToolkit-<ver>-macos.zip
#   dist/release/DesktopToolkit-<ver>-macos.dmg   (drag .app into Applications)
# Usage (from repo root on a Mac / CI):
#   chmod +x build_mac.sh
#   ./build_mac.sh
set -euo pipefail
cd "$(dirname "$0")"
VER="$(tr -d ' \r\n' < VERSION)"
echo "Building DesktopToolkit ${VER} for macOS…"

python3 -m pip install -r requirements.txt pyinstaller --quiet
rm -rf build/mac dist/DesktopToolkit.app dist/DesktopToolkit dist/dmg_stage

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
  --exclude-module torch \
  --exclude-module tensorflow \
  --exclude-module matplotlib \
  main.py

OUT="dist/release"
mkdir -p "$OUT"
APP="dist/DesktopToolkit.app"
if [[ ! -d "$APP" ]]; then
  if [[ -d dist/DesktopToolkit/DesktopToolkit.app ]]; then
    APP="dist/DesktopToolkit/DesktopToolkit.app"
  fi
fi
if [[ ! -d "$APP" ]]; then
  echo "WARNING: .app not found — check dist/ and run PyInstaller output"
  ls -la dist || true
  exit 1
fi

ZIP="$OUT/DesktopToolkit-${VER}-macos.zip"
rm -f "$ZIP"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"
shasum -a 256 "$ZIP" | awk '{print $1}' > "${ZIP}.sha256"
echo "OK: $ZIP"

# DMG like RustDesk: open disk image → drag DesktopToolkit.app into Applications
DMG="$OUT/DesktopToolkit-${VER}-macos.dmg"
STAGE="dist/dmg_stage"
rm -rf "$STAGE" "$DMG"
mkdir -p "$STAGE"
ditto "$APP" "$STAGE/DesktopToolkit.app"
ln -s /Applications "$STAGE/Applications"
# UDZO = compressed read-only DMG
hdiutil create -volname "Desktop Toolkit ${VER}" \
  -srcfolder "$STAGE" \
  -ov -format UDZO \
  "$DMG"
shasum -a 256 "$DMG" | awk '{print $1}' > "${DMG}.sha256"
rm -rf "$STAGE"
echo "OK: $DMG"

echo ""
echo "macOS install:"
echo "  - Preferred: open the .dmg, drag DesktopToolkit.app into Applications"
echo "  - Or unzip the .zip and drag DesktopToolkit.app into Applications"
echo "  - First launch (unsigned): right-click → Open, or System Settings → Privacy → allow"
echo "  - Screen Recording permission may be required for screenshots/recorder"
echo "  - Remote control: install RustDesk from https://rustdesk.com if needed"
