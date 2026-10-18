"""Entry point: `python3 -m reverie [--background] [video]`."""

import logging
import os
import sys


def _preload_layer_shell():
    """gtk4-layer-shell must be loaded before libwayland-client, so re-exec with it preloaded when
    we'll need it (Wayland, not GNOME, library installed)."""
    if os.environ.get("REVERIE_PRELOADED") or not os.environ.get("WAYLAND_DISPLAY"):
        return
    if "GNOME" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper().split(":"):
        return
    for directory in ("/usr/lib/x86_64-linux-gnu", "/usr/lib/aarch64-linux-gnu", "/usr/lib64", "/usr/lib"):
        library = os.path.join(directory, "libgtk4-layer-shell.so.0")
        if os.path.exists(library):
            env = dict(os.environ, REVERIE_PRELOADED="1")
            env["LD_PRELOAD"] = ":".join(filter(None, [library, env.get("LD_PRELOAD")]))
            os.execve(sys.executable, [sys.executable, "-m", "reverie", *sys.argv[1:]], env)


def main():
    _preload_layer_shell()
    # GTK's GL renderer imports the player's GL textures directly; other renderers would copy them.
    os.environ.setdefault("GSK_RENDERER", "gl")
    logging.basicConfig(level=logging.WARNING, format="reverie: %(message)s")

    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("Gsk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import GLib

    GLib.set_prgname("io.github.austxu.Reverie")
    GLib.set_application_name("Reverie")

    from .app import ReverieApp
    return ReverieApp().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
