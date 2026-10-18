"""App state: the library, settings, playback and downloads. The UI and D-Bus clients observe it."""

import logging
import os
import threading

from gi.repository import Gdk, GLib, GObject

from . import desktop, downloader
from .engine import WallpaperEngine
from .library import Frames, Settings, Storage, Wallpaper, load_library, save_library
from .mpv import probe
from .pause import PauseMonitor

log = logging.getLogger("reverie.playback")


def _in_background(work, then):
    """Runs `work()` on a worker thread and `then(result)` on the main loop."""
    def run():
        result = work()
        GLib.idle_add(lambda: then(result) and False)
    threading.Thread(target=run, daemon=True).start()


class AppModel(GObject.Object):
    """Signals: `changed` whenever anything shown to the user changes."""

    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self):
        super().__init__()
        self.settings = Settings()
        self.library = []
        self.thumbnails = {}
        self.pause_reasons = set()
        # Download state: ("idle",), ("needs-tool", url), ("installing", fraction),
        # ("downloading", fraction), ("failed", message).
        self.download_state = ("idle",)
        self.notice = None
        self._notice_timer = 0
        self._download = None
        self.engine = WallpaperEngine()
        self.engine.connect("availability-changed", lambda *_: self._on_availability_changed())
        self.monitor = PauseMonitor(self._on_pause_reasons)

    # MARK: Lifecycle

    def start(self):
        self.monitor.pause_when_covered = self.settings["pause_when_covered"]
        self.monitor.pause_on_battery = self.settings["pause_on_battery"]
        self.monitor.evaluate()
        self.engine.fit = self.settings["scaling"] == "fit"
        self.engine.muted = not self.settings["play_audio"]
        self._load_library()
        if self.settings["enabled"] and self.current:
            self._apply()

    @property
    def current_id(self):
        return self.settings["current_id"]

    @property
    def current(self):
        return next((w for w in self.library if w.id == self.current_id), None)

    @property
    def enabled(self):
        return self.settings["enabled"]

    @property
    def paused_by_user(self):
        return self.settings["user_paused"]

    @property
    def is_playing(self):
        return self.enabled and not self.paused_by_user and not self.pause_reasons

    @property
    def status_text(self):
        if not self.enabled or self.current is None:
            return "Off"
        if not self.engine.available:
            return self.engine.unavailable_status
        if self.paused_by_user:
            return "Paused"
        if self.pause_reasons:
            return min(self.pause_reasons).message
        count = self.engine.screen_count
        return "Playing" if count == 1 else f"Playing on {count} displays"

    def _changed(self):
        self.emit("changed")

    # MARK: Settings

    def set_setting(self, key, value):
        self.settings[key] = value
        if key == "scaling":
            self.engine.fit = value == "fit"
            self._refresh_desktop_picture()
        elif key == "play_audio":
            self.engine.muted = not value
        elif key == "pause_when_covered":
            self.monitor.pause_when_covered = value
            self.monitor.evaluate()
        elif key == "pause_on_battery":
            self.monitor.pause_on_battery = value
            self.monitor.evaluate()
        elif key == "match_desktop_picture":
            if value:
                self._refresh_desktop_picture()
            else:
                self._restore_desktop_pictures()
        self._changed()

    @property
    def launch_at_login(self):
        return desktop.launch_at_login()

    def set_launch_at_login(self, enabled):
        try:
            desktop.set_launch_at_login(enabled)
        except OSError as error:
            self.show_notice(f"Couldn't change the login item. {error.strerror}")
        self._changed()

    # MARK: Choosing wallpapers

    def add_file(self, path):
        path = os.path.abspath(path)
        existing = next((w for w in self.library if w.resolved_path() and os.path.samefile(w.path, path)), None) \
            if os.path.exists(path) else None
        if existing:
            self.select(existing.id)
            return

        def added(playable):
            if not playable:
                self.show_notice(f"Reverie can't play “{os.path.basename(path)}”.")
                return
            title = os.path.splitext(os.path.basename(path))[0]
            self._insert(Wallpaper.new(title, path))

        _in_background(lambda: os.path.isfile(path) and probe(path), added)

    def select(self, wallpaper_id):
        if wallpaper_id == self.current_id and self.enabled:
            return
        if not any(w.id == wallpaper_id for w in self.library):
            return
        self.settings["current_id"] = wallpaper_id
        self.settings["enabled"] = True
        self.settings["user_paused"] = False
        self._apply()
        self._changed()

    def remove(self, wallpaper_id):
        wallpaper = next((w for w in self.library if w.id == wallpaper_id), None)
        if wallpaper is None:
            return
        self.library.remove(wallpaper)
        self.thumbnails.pop(wallpaper.id, None)
        Frames.remove_cache(wallpaper.id)
        if wallpaper.is_downloaded and os.path.exists(wallpaper.path):
            os.remove(wallpaper.path)
        save_library(self.library)
        if wallpaper.id == self.current_id:
            self.settings["current_id"] = None
            if self.library:
                self.select(self.library[0].id)
            else:
                self.set_enabled(False)
        self._changed()

    def set_enabled(self, enabled):
        if enabled == self.enabled:
            return
        if enabled:
            if self.current is None and not self.library:
                return
            if self.current is None:
                self.settings["current_id"] = self.library[0].id
            self.settings["enabled"] = True
            self._apply()
        else:
            self.settings["enabled"] = False
            self.engine.stop()
            self._restore_desktop_pictures()
        self._changed()

    def toggle_pause(self):
        self.settings["user_paused"] = not self.paused_by_user
        self._update_playback()
        self._changed()

    def set_covered(self, covered):
        self.monitor.set_covered(covered)

    # MARK: Downloading

    def submit_link(self, text):
        url = downloader.validate(text)
        if url is None:
            self.download_state = ("failed", "That doesn't look like a video link.")
        elif downloader.is_tool_installed():
            self._start_download(url)
        else:
            self.download_state = ("needs-tool", url)
        self._changed()

    def install_tool_and_download(self):
        if self.download_state[0] != "needs-tool":
            return
        url = self.download_state[1]
        self.download_state = ("installing", 0.0)
        self._changed()

        def progress(fraction):
            if self.download_state[0] == "installing":
                self.download_state = ("installing", fraction)
                self._changed()

        def done(error):
            if self.download_state[0] != "installing":
                return
            if error:
                self.download_state = ("failed", str(error))
                self._changed()
            else:
                self._start_download(url)

        downloader.install_tool(progress, done)

    def cancel_download(self):
        if self._download:
            self._download.cancel()
            self._download = None
        self.download_state = ("idle",)
        self._changed()

    def dismiss_download_message(self):
        if self.download_state[0] in ("failed", "needs-tool"):
            self.download_state = ("idle",)
            self._changed()

    def _start_download(self, link):
        wallpaper_id = Wallpaper.new_id()
        self.download_state = ("downloading", 0.0)
        self._changed()

        def progress(fraction):
            if self.download_state[0] == "downloading":
                self.download_state = ("downloading", fraction)
                self._changed()

        def done(result, error):
            self._download = None
            if result:
                path, title = result
                self.download_state = ("idle",)
                wallpaper = Wallpaper(id=wallpaper_id, title=title, path=path, source="youtube", url=link)
                self._insert(wallpaper)
            elif error and self.download_state[0] == "downloading":
                self.download_state = ("failed", str(error))
            else:
                self.download_state = ("idle",)
            self._changed()

        try:
            self._download = downloader.Download(link, wallpaper_id, progress, done)
        except GLib.Error as error:
            self.download_state = ("failed", f"Couldn't start the downloader. {error.message}")
            self._changed()

    # MARK: Internals

    def _insert(self, wallpaper):
        self.library.insert(0, wallpaper)
        save_library(self.library)
        self.select(wallpaper.id)
        self._changed()

    def _apply(self):
        """Puts the current wallpaper on screen: prepares its frames, then hands it to the engine."""
        wallpaper = self.current
        if wallpaper is None:
            return
        video = wallpaper.resolved_path()
        if video is None:
            self.show_notice(f"“{wallpaper.title}” can't be found. It may have been moved or deleted.")
            return

        def ready(_ok):
            self._load_thumbnail(wallpaper)
            if self.current_id != wallpaper.id or not self.enabled:
                return
            if not self.engine.available:
                self.show_notice(self.engine.unavailable_reason)
            still = Storage.still_path(wallpaper.id)
            self.engine.show(video, still if os.path.exists(still) else None)
            self._update_playback()
            self._refresh_desktop_picture()
            self._changed()

        _in_background(lambda: Frames.prepare(wallpaper, video), ready)

    def _on_pause_reasons(self, reasons):
        self.pause_reasons = reasons
        self._update_playback()
        self._changed()

    def _on_availability_changed(self):
        if not self.engine.available and self.enabled:
            self.show_notice(self.engine.unavailable_reason)
        elif self.engine.available and self.notice == self.engine.unavailable_reason:
            self.notice = None
        self._changed()

    def _update_playback(self):
        if self.engine.playing != self.is_playing:
            self.engine.playing = self.is_playing
            log.info("Playback %s: %s", "resumed" if self.is_playing else "paused", self.status_text)

    def _load_thumbnail(self, wallpaper):
        if wallpaper.id in self.thumbnails:
            return
        path = Storage.thumbnail_path(wallpaper.id)
        if os.path.exists(path):
            try:
                self.thumbnails[wallpaper.id] = Gdk.Texture.new_from_filename(path)
                self._changed()
            except GLib.Error:
                pass

    def show_notice(self, message):
        self.notice = message
        if self._notice_timer:
            GLib.source_remove(self._notice_timer)

        def clear():
            self._notice_timer = 0
            self.notice = None
            self._changed()
            return GLib.SOURCE_REMOVE

        self._notice_timer = GLib.timeout_add_seconds(6, clear)
        self._changed()

    def _load_library(self):
        self.library = load_library()
        for wallpaper in self.library:
            self._load_thumbnail(wallpaper)
            video = wallpaper.resolved_path()
            if wallpaper.id not in self.thumbnails and video:
                _in_background(lambda w=wallpaper, v=video: Frames.prepare(w, v),
                               lambda _ok, w=wallpaper: self._load_thumbnail(w))

    # MARK: Desktop picture

    def _refresh_desktop_picture(self):
        """Sets a still from the video as the real desktop picture, so the lock screen and the moment
        before Reverie starts match the video."""
        wallpaper = self.current
        if not (self.settings["match_desktop_picture"] and self.enabled and wallpaper
                and desktop.supports_desktop_picture()):
            return
        still = Storage.still_path(wallpaper.id)
        if not os.path.exists(still):
            return
        if self.settings["original_desktop_pictures"] is None:
            originals = {k: v for k, v in desktop.current_desktop_pictures().items()
                         if Storage.stills() not in v}
            self.settings["original_desktop_pictures"] = originals
        picture = still
        if self.settings["scaling"] == "fit":
            monitors = Gdk.Display.get_default().get_monitors()
            if monitors.get_n_items():
                monitor = monitors.get_item(0)
                geometry, scale = monitor.get_geometry(), monitor.get_scale()
                picture = Frames.fitted_still(wallpaper.id, round(geometry.width * scale),
                                              round(geometry.height * scale)) or still
        desktop.set_desktop_picture(picture)

    def _restore_desktop_pictures(self):
        originals = self.settings["original_desktop_pictures"]
        if originals is None:
            return
        desktop.restore_desktop_pictures(originals)
        self.settings["original_desktop_pictures"] = None

    # MARK: D-Bus state

    def state(self):
        """A JSON-friendly snapshot for the GNOME panel menu."""
        return {
            "enabled": self.enabled,
            "paused": self.paused_by_user,
            "playing": self.is_playing,
            "status": self.status_text,
            "current": self.current_id,
            "library": [{"id": w.id, "title": w.title, "thumbnail": Storage.thumbnail_path(w.id)}
                        for w in self.library],
        }
