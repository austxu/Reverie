#!/bin/zsh
# Builds Reverie.app into ./build. Pass --install to copy it to /Applications and launch it.
set -euo pipefail
cd "$(dirname "$0")"

swift build -c release --arch arm64
BIN="$(swift build -c release --arch arm64 --show-bin-path)/Reverie"

if [[ ! -f build/icon/Reverie.icns ]]; then
    swift tools/make_icon.swift build/icon
fi

APP=build/Reverie.app
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/Reverie"
cp Resources/Info.plist "$APP/Contents/Info.plist"
cp build/icon/Reverie.icns "$APP/Contents/Resources/Reverie.icns"
codesign --force --sign - --timestamp=none "$APP"
echo "Built $APP"

if [[ "${1:-}" == "--install" ]]; then
    pkill -x Reverie 2>/dev/null && sleep 0.5 || true
    rm -rf /Applications/Reverie.app
    cp -R "$APP" /Applications/Reverie.app
    open /Applications/Reverie.app
    echo "Installed to /Applications/Reverie.app"
fi
