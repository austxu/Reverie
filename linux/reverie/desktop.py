"""Desktop integration: the matching desktop picture and Open at Login."""

import os
import shlex
import shutil
import sys

from gi.repository import Gio, GLib

APP_ID = "io.github.austxu.Reverie"

_BACKGROUND_KEYS = (
    ("org.gnome.desktop.background", "picture-uri"),
    ("org.gnome.desktop.background", "picture-uri-dark"),
    ("org.gnome.desktop.screensaver", "picture-uri"),
)


def _schema(schema_id):
    source = Gio.SettingsSchemaSource.get_default()
    return Gio.Settings.new(schema_id) if source and source.lookup(schema_id, True) else None


def supports_desktop_picture():
    return _schema("org.gnome.desktop.background") is not None


def current_desktop_pictures():
    pictures = {}
    for schema_id, key in _BACKGROUND_KEYS:
        settings = _schema(schema_id)
        if settings and settings.settings_schema.has_key(key):
            pictures[f"{schema_id} {key}"] = settings.get_string(key)
    return pictures


def set_desktop_picture(path):
    """Sets `path` as the GNOME desktop and lock screen picture (light and dark)."""
    uri = GLib.filename_to_uri(path)
    for schema_id, key in _BACKGROUND_KEYS:
        settings = _schema(schema_id)
        if settings and settings.settings_schema.has_key(key):
            settings.set_string(key, uri)
    Gio.Settings.sync()


def restore_desktop_pictures(originals):
    for name, uri in (originals or {}).items():
        schema_id, key = name.split(" ", 1)
        settings = _schema(schema_id)
        if settings and settings.settings_schema.has_key(key):
            settings.set_string(key, uri)
    Gio.Settings.sync()


# MARK: Open at Login


def _autostart_file():
    config = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(config, "autostart", f"{APP_ID}.desktop")


def launch_at_login():
    return os.path.exists(_autostart_file())


def set_launch_at_login(enabled):
    path = _autostart_file()
    if not enabled:
        if os.path.exists(path):
            os.remove(path)
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    command = shutil.which("reverie") or f"{shlex.quote(sys.executable)} -m reverie"
    with open(path, "w") as f:
        f.write(
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Reverie\n"
            "Comment=Live video wallpapers\n"
            f"Exec={command} --background\n"
            f"Icon={APP_ID}\n"
            "NoDisplay=true\n"
            "X-GNOME-Autostart-Phase=Applications\n"
        )
