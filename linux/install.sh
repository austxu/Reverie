#!/bin/sh
# Installs Reverie for the current user (no root needed):
#   ~/.local/lib/reverie              the app
#   ~/.local/bin/reverie              launcher
#   ~/.local/share/applications       desktop entry (app menu, Open With)
#   ~/.local/share/gnome-shell/...    the GNOME Shell extension (on GNOME)
# Usage: ./install.sh [--uninstall]
set -eu
cd "$(dirname "$0")"

APP_ID=io.github.austxu.Reverie
EXT_UUID=reverie@austxu.github.io
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
LIB="$HOME/.local/lib/reverie"
BIN="$HOME/.local/bin"
EXT_DIR="$DATA/gnome-shell/extensions/$EXT_UUID"

if [ "${1:-}" = "--uninstall" ]; then
    pkill -f "^python3 -m reverie" 2>/dev/null || true
    rm -rf "$LIB" "$EXT_DIR" "$BIN/reverie" \
        "$DATA/applications/$APP_ID.desktop" \
        "$DATA/icons/hicolor/256x256/apps/$APP_ID.png" \
        "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/$APP_ID.desktop"
    echo "Reverie is uninstalled. Your library is still in $DATA/reverie; delete it to remove your videos too."
    exit 0
fi

# Check what the app needs at runtime.
missing=""
python3 - <<'EOF' || missing="python3-gi gir1.2-gtk-4.0 gir1.2-adw-1"
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw  # noqa: F401
EOF
python3 -c 'import ctypes; ctypes.CDLL("libmpv.so.2")' 2>/dev/null || missing="$missing libmpv2"
if [ -n "$missing" ]; then
    echo "Reverie needs: $missing"
    echo "On Ubuntu/Debian: sudo apt install $missing"
    echo "On Fedora: sudo dnf install python3-gobject gtk4 libadwaita mpv-libs"
    echo "On Arch: sudo pacman -S python-gobject gtk4 libadwaita mpv"
    exit 1
fi

mkdir -p "$LIB" "$BIN" "$DATA/applications" "$DATA/icons/hicolor/256x256/apps"
rm -rf "$LIB/reverie"
cp -r reverie "$LIB/reverie"
find "$LIB" -name __pycache__ -prune -exec rm -rf {} +

cat > "$BIN/reverie" <<EOF
#!/bin/sh
export PYTHONPATH="$LIB\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -m reverie "\$@"
EOF
chmod +x "$BIN/reverie"

cp data/$APP_ID.desktop "$DATA/applications/"
cp ../docs/icon.png "$DATA/icons/hicolor/256x256/apps/$APP_ID.png"
update-desktop-database "$DATA/applications" 2>/dev/null || true
gtk-update-icon-cache -q -t "$DATA/icons/hicolor" 2>/dev/null || true

case "${XDG_CURRENT_DESKTOP:-}" in
*GNOME*)
    rm -rf "$EXT_DIR"
    mkdir -p "$(dirname "$EXT_DIR")"
    cp -r "gnome-extension/$EXT_UUID" "$EXT_DIR"
    echo "Installed Reverie and its GNOME Shell extension."
    if gnome-extensions info "$EXT_UUID" >/dev/null 2>&1; then
        gnome-extensions enable "$EXT_UUID" && echo "The extension is enabled."
    else
        echo "Log out and back in so GNOME Shell finds the extension, then turn it on:"
        echo "  gnome-extensions enable $EXT_UUID"
    fi
    if gnome-extensions list --enabled 2>/dev/null | grep -q hanabi; then
        echo "Hanabi is also enabled; turn it off so the two don't draw over each other:"
        echo "  gnome-extensions disable hanabi-extension@jeffshee.github.io"
    fi
    ;;
*)
    python3 - <<'EOF' 2>/dev/null || echo "For wlroots compositors (Sway, Hyprland, …) also install gtk4-layer-shell (e.g. sudo apt install gir1.2-gtk4layershell-1.0)."
import gi
gi.require_version("Gtk4LayerShell", "1.0")
EOF
    echo "Installed Reverie."
    ;;
esac

case ":$PATH:" in
*":$BIN:"*) ;;
*) echo "Note: $BIN isn't on your PATH; add it to start Reverie from a terminal." ;;
esac
echo "Start it from your app menu, or run: reverie"
