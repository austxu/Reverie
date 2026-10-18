"""Wallpaper windows and the shared looping player.

One window per monitor sits beneath the desktop. On GNOME the Reverie Shell extension takes the
windows and shows them as the desktop background (and in the overview); on wlroots compositors,
KDE and others gtk4-layer-shell puts them on the background layer. A single Player feeds every
screen, so the video is decoded once no matter how many displays are attached.
"""

import logging
import os
import time

import ctypes

from gi.repository import Gdk, Gio, GLib, GObject, Gtk

from .library import Frames
from .mpv import Player, gpointer

log = logging.getLogger("reverie.playback")

SHELL_BUS_NAME = "io.github.austxu.Reverie.Shell"
WINDOW_TITLE_PREFIX = "reverie-wallpaper|"

_CSS = b"""
window.reverie-wallpaper { background: transparent; }
.reverie-root { opacity: 0; transition: opacity 800ms ease-in-out; }
.reverie-root.shown { opacity: 1; }
.reverie-root.fast { transition: none; }
.reverie-layer { opacity: 0; transition: opacity 700ms ease-in-out; }
.reverie-layer.shown { opacity: 1; }
.reverie-layer.instant { transition: none; }
"""


def _is_gnome():
    return "GNOME" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper().split(":")


def _layer_shell():
    """The Gtk4LayerShell module if the compositor supports wlr-layer-shell, else None."""
    try:
        import gi
        gi.require_version("Gtk4LayerShell", "1.0")
        from gi.repository import Gtk4LayerShell
    except (ImportError, ValueError):
        return None
    return Gtk4LayerShell if Gtk4LayerShell.is_supported() else None


def _clear_input_region(surface):
    """Gives `surface` an empty input region (via C: PyGObject needs python3-gi-cairo for this)."""
    cairo = ctypes.CDLL("libcairo.so.2")
    gtk = ctypes.CDLL("libgtk-4.so.1")
    cairo.cairo_region_create.restype = ctypes.c_void_p
    cairo.cairo_region_destroy.argtypes = [ctypes.c_void_p]
    gtk.gdk_surface_set_input_region.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    region = cairo.cairo_region_create()
    gtk.gdk_surface_set_input_region(gpointer(surface), region)
    cairo.cairo_region_destroy(region)


class Layer(Gtk.Overlay):
    """One video on one screen: its backdrop (the still, or a blurred copy around a fitted video)
    with the live picture on top. Layers crossfade when the video changes."""

    def __init__(self, player, fit, backdrop):
        super().__init__(hexpand=True, vexpand=True)
        self.player = player
        self.add_css_class("reverie-layer")
        self.backdrop = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True, paintable=backdrop)
        self.picture = Gtk.Picture(paintable=player.paintable, can_shrink=True,
                                   content_fit=Gtk.ContentFit.CONTAIN if fit else Gtk.ContentFit.COVER)
        self.set_child(self.backdrop)
        self.add_overlay(self.picture)

    def configure(self, fit, backdrop):
        self.picture.set_content_fit(Gtk.ContentFit.CONTAIN if fit else Gtk.ContentFit.COVER)
        self.backdrop.set_paintable(backdrop)


class WallpaperWindow(Gtk.Window):
    """A borderless, click-through window covering one monitor, beneath everything else."""

    def __init__(self, monitor, layer_shell):
        connector = monitor.get_connector() or "unknown"
        super().__init__(title=WINDOW_TITLE_PREFIX + connector, decorated=False, focusable=False,
                         can_focus=False)
        self.monitor = monitor
        self.connector = connector
        self.add_css_class("reverie-wallpaper")
        geometry = monitor.get_geometry()
        self.set_default_size(geometry.width, geometry.height)

        self.root = Gtk.Overlay(hexpand=True, vexpand=True)
        self.root.add_css_class("reverie-root")
        self.root.set_child(Gtk.Box())
        self.set_child(self.root)

        if layer_shell:
            ls = layer_shell
            ls.init_for_window(self)
            ls.set_namespace(self, "reverie")
            ls.set_layer(self, ls.Layer.BACKGROUND)
            ls.set_monitor(self, monitor)
            for edge in (ls.Edge.TOP, ls.Edge.BOTTOM, ls.Edge.LEFT, ls.Edge.RIGHT):
                ls.set_anchor(self, edge, True)
            ls.set_exclusive_zone(self, -1)
            ls.set_keyboard_mode(self, ls.KeyboardMode.NONE)
        self.connect("realize", self._on_realize)

    def _on_realize(self, _window):
        # Never intercept clicks meant for the desktop.
        _clear_input_region(self.get_surface())

    def show_on_monitor(self, layer_shell):
        if not layer_shell:
            # The Shell extension minimizes this window and shows it as the background.
            self.fullscreen_on_monitor(self.monitor)
        self.present()

    @property
    def layers(self):
        layers, child = [], self.root.get_first_child()
        while child is not None:
            if isinstance(child, Layer):
                layers.append(child)
            child = child.get_next_sibling()
        return layers

    def add_layer(self, player, fit, backdrop, visible):
        layer = Layer(player, fit, backdrop)
        if visible:
            layer.add_css_class("instant")
            layer.add_css_class("shown")
        self.root.add_overlay(layer)
        return layer


class WallpaperEngine(GObject.Object):
    """Owns the wallpaper windows and a single looping player shared by all screens."""

    __gsignals__ = {
        # The compositor integration came or went (e.g. the GNOME extension was turned on or off).
        "availability-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self):
        super().__init__()
        provider = Gtk.CssProvider()
        provider.load_from_data(_CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.windows = {}
        self.player = None
        self._video = None
        self._still = None
        self._still_texture = None
        self._blurred = None
        self._fit = False
        self._muted = True
        self._playing = False
        self._recoveries = []
        self._recovering = False
        self._watchdog = 0
        self._shell_present = False
        self.watchdog_enabled = True

        self.layer_shell = None if _is_gnome() else _layer_shell()
        self.backend = "gnome" if _is_gnome() else ("layer-shell" if self.layer_shell else None)
        if self.backend == "gnome":
            Gio.bus_watch_name(Gio.BusType.SESSION, SHELL_BUS_NAME, Gio.BusNameWatcherFlags.NONE,
                               self._on_shell_appeared, self._on_shell_vanished)
        Gdk.Display.get_default().get_monitors().connect("items-changed", lambda *_: self._sync_windows())

    # MARK: Availability

    @property
    def available(self):
        if self.backend == "gnome":
            return self._shell_present
        return self.backend is not None

    @property
    def unavailable_reason(self):
        if self.backend == "gnome":
            return "Turn on the Reverie extension in the Extensions app to show your wallpaper."
        return "This desktop doesn't support background layers (wlr-layer-shell), so Reverie can't draw the wallpaper."

    def _on_shell_appeared(self, *_):
        self._shell_present = True
        log.info("GNOME Shell extension is active")
        if self._video and self.player is None:
            self.show(self._video, self._still)
        self.emit("availability-changed")

    def _on_shell_vanished(self, *_):
        was_present = self._shell_present
        self._shell_present = False
        # Without the extension these would be ordinary full-screen windows covering everything.
        for window in self.windows.values():
            window.destroy()
        self.windows.clear()
        if self.player:
            self.player.destroy()
            self.player = None
        if was_present:
            log.info("GNOME Shell extension went away")
            self.emit("availability-changed")

    # MARK: Settings

    @property
    def fit(self):
        return self._fit

    @fit.setter
    def fit(self, fit):
        if fit == self._fit:
            return
        self._fit = fit
        for window in self.windows.values():
            for layer in window.layers:
                layer.configure(fit, self._backdrop)

    @property
    def _backdrop(self):
        return self._blurred if self._fit else self._still_texture

    @property
    def muted(self):
        return self._muted

    @muted.setter
    def muted(self, muted):
        self._muted = muted
        if self.player:
            self.player.set_muted(muted)

    @property
    def playing(self):
        return self._playing

    @playing.setter
    def playing(self, playing):
        self._playing = playing
        self._apply_rate()

    @property
    def is_active(self):
        return self.player is not None

    @property
    def screen_count(self):
        return Gdk.Display.get_default().get_monitors().get_n_items()

    # MARK: Showing videos

    def show(self, video, still):
        """Shows `video` on every screen, crossfading from the previous video if there was one."""
        self._video, self._still = video, still
        try:
            self._still_texture = Gdk.Texture.new_from_filename(still) if still else None
        except GLib.Error:
            self._still_texture = None
        self._blurred = Frames.blurred(still) if still else None
        if not self.available:
            return True  # Shown once the compositor integration appears.
        first_appearance = self.player is None or not self.windows
        try:
            player = Player(video, muted=self._muted, playing=self._playing)
        except (RuntimeError, OSError) as error:
            log.error("Couldn't start playback: %s", error)
            return False
        player.connect("failed", lambda _p, reason: self._recover(reason))
        old = self.player
        self.player = player
        existing = set(self.windows)
        self._sync_windows()  # Windows created now already show `player`; their whole window fades in.
        layers = [w.add_layer(player, self._fit, self._backdrop, visible=False)
                  for key, w in self.windows.items() if key in existing]
        self._apply_rate()

        revealed = False

        def reveal(*_):
            nonlocal revealed
            if revealed or self.player is not player:
                return GLib.SOURCE_REMOVE
            revealed = True
            for layer in layers:
                if first_appearance:
                    layer.add_css_class("instant")
                layer.add_css_class("shown")
            for window in self.windows.values():
                window.root.add_css_class("shown")
            GLib.timeout_add(800, self._retire, old, player)
            return GLib.SOURCE_REMOVE

        # Fade the new video in once its first frame is ready, or after a short grace period.
        player.connect("first-frame", reveal)
        GLib.timeout_add(1500, reveal)
        return True

    def _retire(self, old, current):
        """Removes the previous video's layers after the crossfade and frees its decoder."""
        for window in self.windows.values():
            for layer in window.layers:
                if layer.player is not current and (old is None or layer.player is old):
                    window.root.remove_overlay(layer)
        if old is not None and old is not self.player:
            old.destroy()
        return GLib.SOURCE_REMOVE

    def stop(self):
        """Fades the wallpaper out and tears everything down."""
        self._video = None
        self._stop_watchdog()
        closing = list(self.windows.values())
        self.windows.clear()
        player, self.player = self.player, None
        for window in closing:
            window.root.remove_css_class("shown")

        def finish():
            for window in closing:
                window.destroy()
            if player:
                player.destroy()
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(850, finish)

    # MARK: Playback health

    def _apply_rate(self):
        if self.player:
            self.player.set_playing(self._playing)
        if self._playing and self.player and self.watchdog_enabled:
            self._start_watchdog()
        else:
            self._stop_watchdog()

    def _start_watchdog(self):
        if self._watchdog:
            return
        last, stuck = None, 0

        def check():
            nonlocal last, stuck
            if not self.player or not self._playing or not self.watchdog_enabled:
                self._watchdog = 0
                return GLib.SOURCE_REMOVE
            position = self.player.position()
            stuck = stuck + 1 if position is not None and position == last else 0
            last = position
            if stuck >= 2:
                stuck = 0
                self._recover("playback stalled")
            return GLib.SOURCE_CONTINUE

        self._watchdog = GLib.timeout_add_seconds(2, check)

    def _stop_watchdog(self):
        if self._watchdog:
            GLib.source_remove(self._watchdog)
            self._watchdog = 0

    def _recover(self, reason):
        """Rebuilds the player after a failure, crossfading over the last frame. Retries back off
        (1, 2, 4… s) and stop after a few attempts a minute so a persistent failure can't spin."""
        if self._recovering or self.player is None:
            return
        now = time.monotonic()
        self._recoveries = [t for t in self._recoveries if now - t < 60]
        if len(self._recoveries) >= 5:
            log.error("Playback failed (%s); giving up after repeated failures", reason)
            return
        delay = 2 ** len(self._recoveries)
        self._recoveries.append(now)
        self._recovering = True
        log.warning("Playback failed (%s); rebuilding player in %ds", reason, delay)

        def rebuild():
            self._recovering = False
            if self._video and self.player is not None:
                self.show(self._video, self._still)
            return GLib.SOURCE_REMOVE

        GLib.timeout_add_seconds(delay, rebuild)

    # MARK: Windows

    def _sync_windows(self):
        """Creates or removes windows so there is exactly one per connected monitor."""
        if self.player is None or not self.available:
            return
        monitors = Gdk.Display.get_default().get_monitors()
        current = {}
        for i in range(monitors.get_n_items()):
            monitor = monitors.get_item(i)
            current[monitor.get_connector() or str(i)] = monitor
        for key in list(self.windows):
            if key not in current or self.windows[key].monitor is not current[key]:
                self.windows.pop(key).destroy()
        for key, monitor in current.items():
            if key in self.windows:
                continue
            window = WallpaperWindow(monitor, self.layer_shell)
            window.add_layer(self.player, self._fit, self._backdrop, visible=True)
            self.windows[key] = window
            window.show_on_monitor(self.layer_shell)
