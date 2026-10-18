"""The wallpaper library, settings, on-disk locations, and frame extraction."""

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field

from gi.repository import Gdk, GdkPixbuf, Graphene, Gsk, Gtk

from . import mpv


def _xdg(variable, fallback):
    return os.path.join(os.environ.get(variable) or os.path.expanduser(fallback), "reverie")


class Storage:
    """On-disk locations: data under ~/.local/share/reverie, settings under ~/.config/reverie."""

    root = _xdg("XDG_DATA_HOME", "~/.local/share")
    config = _xdg("XDG_CONFIG_HOME", "~/.config")

    @classmethod
    def directory(cls, name):
        path = os.path.join(cls.root, name)
        os.makedirs(path, exist_ok=True)
        return path

    @classmethod
    def library_file(cls):
        os.makedirs(cls.root, exist_ok=True)
        return os.path.join(cls.root, "library.json")

    @classmethod
    def settings_file(cls):
        os.makedirs(cls.config, exist_ok=True)
        return os.path.join(cls.config, "settings.json")

    @classmethod
    def videos(cls):
        return cls.directory("Videos")

    @classmethod
    def bin(cls):
        return cls.directory("bin")

    @classmethod
    def thumbnail_path(cls, wallpaper_id):
        return os.path.join(cls.directory("Thumbnails"), f"{wallpaper_id}.jpg")

    @classmethod
    def still_path(cls, wallpaper_id):
        return os.path.join(cls.directory("Stills"), f"{wallpaper_id}.png")

    @classmethod
    def stills(cls):
        return cls.directory("Stills")


def _write_json(path, value):
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(value, f, indent=2)
    os.replace(tmp, path)


@dataclass
class Wallpaper:
    """A video the user has used as a wallpaper."""

    id: str
    title: str
    path: str
    source: str = "file"  # "file" or "youtube"
    url: str | None = None
    added_at: float = field(default_factory=time.time)

    @staticmethod
    def new(title, path, source="file", url=None):
        return Wallpaper(id=str(uuid.uuid4()).upper(), title=title, path=path, source=source, url=url)

    @property
    def is_downloaded(self):
        return self.source == "youtube"

    def resolved_path(self):
        return self.path if os.path.isfile(self.path) else None


def load_library():
    try:
        with open(Storage.library_file()) as f:
            return [Wallpaper(**{k: v for k, v in item.items() if k in Wallpaper.__dataclass_fields__})
                    for item in json.load(f)]
    except (OSError, ValueError, TypeError):
        return []


def save_library(library):
    _write_json(Storage.library_file(), [asdict(w) for w in library])


class Settings:
    """Small JSON-backed settings store with defaults."""

    DEFAULTS = {
        "enabled": False,
        "current_id": None,
        "user_paused": False,
        "scaling": "fill",
        "play_audio": False,
        "pause_when_covered": True,
        "pause_on_battery": False,
        "match_desktop_picture": True,
        "original_desktop_pictures": None,
    }

    def __init__(self):
        self._values = dict(self.DEFAULTS)
        try:
            with open(Storage.settings_file()) as f:
                self._values.update(json.load(f))
        except (OSError, ValueError):
            pass

    def __getitem__(self, key):
        return self._values.get(key, self.DEFAULTS.get(key))

    def __setitem__(self, key, value):
        if self._values.get(key) == value:
            return
        self._values[key] = value
        _write_json(Storage.settings_file(), self._values)


# MARK: Frames


class Frames:
    """Stills, thumbnails and blurred backdrops, all derived from one extracted frame."""

    @staticmethod
    def prepare(wallpaper, video):
        """Creates the full-resolution still and the thumbnail if they don't exist yet. Blocking."""
        still = Storage.still_path(wallpaper.id)
        if not os.path.exists(still):
            tmp = f"{still}.tmp.png"
            if not mpv.grab_frame(video, tmp):
                return False
            os.replace(tmp, still)
        thumb = Storage.thumbnail_path(wallpaper.id)
        if not os.path.exists(thumb):
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(still, 640, 400, True)
                pixbuf.savev(thumb, "jpeg", ["quality"], ["85"])
            except Exception:  # noqa: BLE001 - a missing thumbnail is cosmetic
                pass
        return True

    @staticmethod
    def remove_cache(wallpaper_id):
        for path in (Storage.thumbnail_path(wallpaper_id),):
            if os.path.exists(path):
                os.remove(path)
        for name in os.listdir(Storage.stills()):
            if name.startswith(wallpaper_id):
                os.remove(os.path.join(Storage.stills(), name))

    @staticmethod
    def _renderer():
        renderer = Gsk.CairoRenderer()
        renderer.realize_for_display(Gdk.Display.get_default())
        return renderer

    @staticmethod
    def _append_blurred(snapshot, texture, width, height):
        """A soft, slightly dimmed copy of `texture` covering width×height."""
        scale = max(width / texture.get_width(), height / texture.get_height())
        # Overscan so the blur doesn't pull transparent edges in.
        w, h = texture.get_width() * scale * 1.15, texture.get_height() * scale * 1.15
        bounds = Graphene.Rect().init(0, 0, width, height)
        snapshot.push_clip(bounds)
        matrix = Graphene.Matrix().init_from_float([
            0.86, -0.04, -0.04, 0,
            -0.04, 0.86, -0.04, 0,
            -0.04, -0.04, 0.86, 0,
            0, 0, 0, 1,
        ])
        snapshot.push_color_matrix(matrix, Graphene.Vec4().init(0, 0, 0, 0))
        snapshot.push_blur(max(width, height) / 20)
        snapshot.append_scaled_texture(texture, Gsk.ScalingFilter.LINEAR,
                                       Graphene.Rect().init((width - w) / 2, (height - h) / 2, w, h))
        snapshot.pop()
        snapshot.pop()
        snapshot.pop()

    @classmethod
    def blurred(cls, still_path):
        """A small blurred backdrop for a fitted video; the screen's upscaling adds to the softness."""
        try:
            texture = Gdk.Texture.new_from_filename(still_path)
        except Exception:  # noqa: BLE001
            return None
        width = 320
        height = max(1, round(width * texture.get_height() / texture.get_width()))
        snapshot = Gtk.Snapshot()
        cls._append_blurred(snapshot, texture, width, height)
        node = snapshot.to_node()
        renderer = cls._renderer()
        try:
            return renderer.render_texture(node, Graphene.Rect().init(0, 0, width, height))
        finally:
            renderer.unrealize()

    @classmethod
    def fitted_still(cls, wallpaper_id, width, height):
        """The desktop picture for Fit: the whole frame centered on its blurred copy, at the screen's
        pixel size so the lock screen matches the live wallpaper."""
        path = os.path.join(Storage.stills(), f"{wallpaper_id}-fit-{width}x{height}.png")
        if os.path.exists(path):
            return path
        try:
            texture = Gdk.Texture.new_from_filename(Storage.still_path(wallpaper_id))
        except Exception:  # noqa: BLE001
            return None
        snapshot = Gtk.Snapshot()
        cls._append_blurred(snapshot, texture, width, height)
        scale = min(width / texture.get_width(), height / texture.get_height())
        w, h = texture.get_width() * scale, texture.get_height() * scale
        snapshot.append_scaled_texture(texture, Gsk.ScalingFilter.TRILINEAR,
                                       Graphene.Rect().init((width - w) / 2, (height - h) / 2, w, h))
        renderer = cls._renderer()
        try:
            result = renderer.render_texture(snapshot.to_node(), Graphene.Rect().init(0, 0, width, height))
            return path if result.save_to_png(path) else None
        finally:
            renderer.unrealize()

