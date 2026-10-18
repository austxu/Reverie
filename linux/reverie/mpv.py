"""A small ctypes binding to libmpv, plus a player that renders on the GPU into GTK textures.

mpv decodes (with VA-API/NVDEC when available) and draws each frame into an OpenGL texture that
shares a context with GTK. The texture is handed to GTK as a Gdk.GLTexture, so a frame never
leaves the GPU, and one player can be shown by any number of Gtk.Pictures on any number of screens.
"""

import ctypes
import locale
import logging
import os
import threading

from gi.repository import Gdk, GLib, GObject, Graphene

log = logging.getLogger("reverie.playback")

_lib = ctypes.CDLL(os.environ.get("REVERIE_LIBMPV", "libmpv.so.2"))

_p = ctypes.c_void_p
_s = ctypes.c_char_p


def _fn(name, restype, *argtypes):
    f = getattr(_lib, name)
    f.restype = restype
    f.argtypes = list(argtypes)
    return f


mpv_create = _fn("mpv_create", _p)
mpv_initialize = _fn("mpv_initialize", ctypes.c_int, _p)
mpv_terminate_destroy = _fn("mpv_terminate_destroy", None, _p)
mpv_set_option_string = _fn("mpv_set_option_string", ctypes.c_int, _p, _s, _s)
mpv_set_property_string = _fn("mpv_set_property_string", ctypes.c_int, _p, _s, _s)
mpv_get_property_string = _fn("mpv_get_property_string", _p, _p, _s)
mpv_free = _fn("mpv_free", None, _p)
mpv_command = _fn("mpv_command", ctypes.c_int, _p, ctypes.POINTER(_s))
mpv_wait_event = _fn("mpv_wait_event", _p, _p, ctypes.c_double)
mpv_error_string = _fn("mpv_error_string", _s, ctypes.c_int)
_WAKEUP = ctypes.CFUNCTYPE(None, _p)
mpv_set_wakeup_callback = _fn("mpv_set_wakeup_callback", None, _p, _WAKEUP, _p)


class _RenderParam(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("data", _p)]


_GET_PROC = ctypes.CFUNCTYPE(_p, _p, _s)


class _GLInitParams(ctypes.Structure):
    _fields_ = [("get_proc_address", _GET_PROC), ("get_proc_address_ctx", _p)]


class _GLFbo(ctypes.Structure):
    _fields_ = [("fbo", ctypes.c_int), ("w", ctypes.c_int), ("h", ctypes.c_int), ("internal_format", ctypes.c_int)]


mpv_render_context_create = _fn("mpv_render_context_create", ctypes.c_int, ctypes.POINTER(_p), _p, ctypes.POINTER(_RenderParam))
mpv_render_context_set_update_callback = _fn("mpv_render_context_set_update_callback", None, _p, _WAKEUP, _p)
mpv_render_context_update = _fn("mpv_render_context_update", ctypes.c_uint64, _p)
mpv_render_context_render = _fn("mpv_render_context_render", ctypes.c_int, _p, ctypes.POINTER(_RenderParam))
mpv_render_context_free = _fn("mpv_render_context_free", None, _p)

RENDER_API_TYPE, RENDER_OPENGL_INIT, RENDER_OPENGL_FBO, RENDER_FLIP_Y = 1, 2, 3, 4
RENDER_X11_DISPLAY, RENDER_WL_DISPLAY, RENDER_BLOCK_FOR_TARGET_TIME = 8, 9, 12
RENDER_UPDATE_FRAME = 1

EVENT_SHUTDOWN, EVENT_END_FILE, EVENT_FILE_LOADED, EVENT_PLAYBACK_RESTART, EVENT_PROPERTY_CHANGE = 1, 7, 8, 21, 22
END_FILE_REASON_ERROR = 4


class _Event(ctypes.Structure):
    _fields_ = [("event_id", ctypes.c_int), ("error", ctypes.c_int), ("reply_userdata", ctypes.c_uint64), ("data", _p)]


class _EndFile(ctypes.Structure):
    _fields_ = [("reason", ctypes.c_int), ("error", ctypes.c_int)]


class Handle:
    """A bare mpv instance: options, commands, properties and a blocking event loop."""

    def __init__(self, **options):
        # libmpv refuses to start unless numbers are formatted the C way; GTK sets the user's locale.
        locale.setlocale(locale.LC_NUMERIC, "C")
        self.ptr = mpv_create()
        if not self.ptr:
            raise RuntimeError("libmpv couldn't be initialized")
        defaults = {"config": "no", "terminal": "no", "input-default-bindings": "no", "osc": "no"}
        for key, value in {**defaults, **options}.items():
            mpv_set_option_string(self.ptr, key.replace("_", "-").encode(), str(value).encode())
        mpv_initialize(self.ptr)

    def command(self, *args):
        argv = (_s * (len(args) + 1))(*[str(a).encode() for a in args], None)
        return mpv_command(self.ptr, argv)

    def set(self, name, value):
        if isinstance(value, bool):
            value = "yes" if value else "no"
        mpv_set_property_string(self.ptr, name.encode(), str(value).encode())

    def get(self, name):
        ptr = mpv_get_property_string(self.ptr, name.encode())
        if not ptr:
            return None
        try:
            return ctypes.string_at(ptr).decode(errors="replace")
        finally:
            mpv_free(ptr)

    def get_float(self, name):
        try:
            return float(self.get(name))
        except (TypeError, ValueError):
            return None

    def wait_event(self, timeout):
        """Returns (event_id, end_file_reason) for the next event, or (0, None) on timeout."""
        event = _Event.from_address(mpv_wait_event(self.ptr, timeout))
        reason = None
        if event.event_id == EVENT_END_FILE and event.data:
            reason = _EndFile.from_address(event.data).reason
        return event.event_id, reason

    def destroy(self):
        if self.ptr:
            mpv_terminate_destroy(self.ptr)
            self.ptr = None


def grab_frame(video, output, fraction=0.1, max_seconds=3.0, timeout=20.0):
    """Writes a representative frame of `video` to the PNG `output`: a little way in, since many
    clips open on black. Blocking; call it from a worker thread. Returns True on success."""
    handle = Handle(vo="null", ao="null", aid="no", sid="no", pause="yes", hwdec="no")
    try:
        handle.command("loadfile", video)
        deadline = GLib.get_monotonic_time() + timeout * 1e6
        seeked = False
        while GLib.get_monotonic_time() < deadline:
            event, reason = handle.wait_event(1)
            if event == EVENT_FILE_LOADED:
                duration = handle.get_float("duration") or 0
                if handle.get("vid") in (None, "no"):
                    return False
                handle.command("seek", f"{min(duration * fraction, max_seconds):.3f}", "absolute+exact")
                seeked = True
            elif event == EVENT_PLAYBACK_RESTART and seeked:
                return handle.command("screenshot-to-file", output, "video") >= 0 and os.path.exists(output)
            elif event in (EVENT_END_FILE, EVENT_SHUTDOWN):
                return False
        return False
    finally:
        handle.destroy()


def probe(video, timeout=10.0):
    """Returns True if mpv can open `video` and it has a video track. Blocking."""
    handle = Handle(vo="null", ao="null", aid="no", sid="no", pause="yes", hwdec="no")
    try:
        handle.command("loadfile", video)
        deadline = GLib.get_monotonic_time() + timeout * 1e6
        while GLib.get_monotonic_time() < deadline:
            event, _ = handle.wait_event(1)
            if event == EVENT_FILE_LOADED:
                return handle.get("vid") not in (None, "no")
            if event in (EVENT_END_FILE, EVENT_SHUTDOWN):
                return False
        return False
    finally:
        handle.destroy()


# MARK: OpenGL

_egl = ctypes.CDLL("libEGL.so.1")
_egl.eglGetProcAddress.restype = _p
_egl.eglGetProcAddress.argtypes = [_s]


@_GET_PROC
def _get_proc_address(_ctx, name):
    return _egl.eglGetProcAddress(name)


class _GL:
    """The handful of GL entry points the player needs, resolved through EGL."""

    TEXTURE_2D, RGBA, RGBA8, UNSIGNED_BYTE = 0x0DE1, 0x1908, 0x8058, 0x1401
    TEXTURE_MIN_FILTER, TEXTURE_MAG_FILTER, LINEAR = 0x2801, 0x2800, 0x2601
    TEXTURE_WRAP_S, TEXTURE_WRAP_T, CLAMP_TO_EDGE = 0x2802, 0x2803, 0x812F
    FRAMEBUFFER, COLOR_ATTACHMENT0, SYNC_GPU_COMMANDS_COMPLETE = 0x8D40, 0x8CE0, 0x9117

    def __init__(self):
        u, i, ptr = ctypes.c_uint, ctypes.c_int, _p
        sigs = {
            "glGenTextures": (None, i, ctypes.POINTER(u)),
            "glDeleteTextures": (None, i, ctypes.POINTER(u)),
            "glBindTexture": (None, u, u),
            "glTexImage2D": (None, u, i, i, i, i, i, u, u, ptr),
            "glTexParameteri": (None, u, u, i),
            "glGenFramebuffers": (None, i, ctypes.POINTER(u)),
            "glDeleteFramebuffers": (None, i, ctypes.POINTER(u)),
            "glBindFramebuffer": (None, u, u),
            "glFramebufferTexture2D": (None, u, u, u, u, i),
            "glFenceSync": (ptr, u, u),
            "glDeleteSync": (None, ptr),
            "glFlush": (None,),
        }
        for name, (res, *args) in sigs.items():
            address = _egl.eglGetProcAddress(name.encode())
            if not address:
                raise RuntimeError(f"OpenGL function {name} is unavailable")
            setattr(self, name[2:], ctypes.CFUNCTYPE(res, *args)(address))


class _Target:
    """A texture with a framebuffer around it that mpv draws one frame into."""

    def __init__(self, gl, width, height):
        self.gl, self.width, self.height = gl, width, height
        self.busy = False
        self.sync = None
        tex, fbo = ctypes.c_uint(), ctypes.c_uint()
        gl.GenTextures(1, ctypes.byref(tex))
        gl.BindTexture(gl.TEXTURE_2D, tex.value)
        gl.TexImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, width, height, 0, gl.RGBA, gl.UNSIGNED_BYTE, None)
        for param, value in ((gl.TEXTURE_MIN_FILTER, gl.LINEAR), (gl.TEXTURE_MAG_FILTER, gl.LINEAR),
                             (gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE), (gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE)):
            gl.TexParameteri(gl.TEXTURE_2D, param, value)
        gl.BindTexture(gl.TEXTURE_2D, 0)
        gl.GenFramebuffers(1, ctypes.byref(fbo))
        gl.BindFramebuffer(gl.FRAMEBUFFER, fbo.value)
        gl.FramebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex.value, 0)
        gl.BindFramebuffer(gl.FRAMEBUFFER, 0)
        self.texture, self.fbo = tex.value, fbo.value

    def release_sync(self):
        if self.sync:
            self.gl.DeleteSync(self.sync)
            self.sync = None

    def delete(self):
        self.release_sync()
        self.gl.DeleteFramebuffers(1, ctypes.byref(ctypes.c_uint(self.fbo)))
        self.gl.DeleteTextures(1, ctypes.byref(ctypes.c_uint(self.texture)))


def gpointer(gobject):
    """The C pointer behind a PyGObject object."""
    ctypes.pythonapi.PyCapsule_GetPointer.restype = _p
    ctypes.pythonapi.PyCapsule_GetPointer.argtypes = [ctypes.py_object, _s]
    return ctypes.pythonapi.PyCapsule_GetPointer(gobject.__gpointer__, None)


def _native_display(display):
    """Returns (render param, wl_display* or Display*) for GTK's connection, or None."""
    gdk_display = gpointer(display)
    gtk = ctypes.CDLL("libgtk-4.so.1")
    for type_name, symbol, param in (("GdkWaylandDisplay", "gdk_wayland_display_get_wl_display", RENDER_WL_DISPLAY),
                                     ("GdkX11Display", "gdk_x11_display_get_xdisplay", RENDER_X11_DISPLAY)):
        if type(display).__name__ == type_name and hasattr(gtk, symbol):
            getter = getattr(gtk, symbol)
            getter.restype, getter.argtypes = _p, [_p]
            return param, getter(gdk_display)
    return None


class VideoPaintable(GObject.Object, Gdk.Paintable):
    """The player's current frame. Shared by every screen's Gtk.Picture."""

    def __init__(self):
        super().__init__()
        self.texture = None

    def set_texture(self, texture):
        resized = (self.texture is None or texture is None
                   or (texture.get_width(), texture.get_height()) != (self.texture.get_width(), self.texture.get_height()))
        self.texture = texture
        if resized:
            self.invalidate_size()
        self.invalidate_contents()

    def do_snapshot(self, snapshot, width, height):
        if self.texture is not None:
            snapshot.append_texture(self.texture, Graphene.Rect().init(0, 0, width, height))

    def do_get_intrinsic_width(self):
        return self.texture.get_width() if self.texture else 0

    def do_get_intrinsic_height(self):
        return self.texture.get_height() if self.texture else 0

    def do_get_intrinsic_aspect_ratio(self):
        return self.texture.get_width() / self.texture.get_height() if self.texture else 0.0


class Player(GObject.Object):
    """Loops one video. Frames are rendered on the GPU into `paintable`.

    mpv renders on a dedicated thread, where it may block until each frame's display time (that's
    how it paces playback); the main loop only wraps finished frames for GTK.

    Signals: `first-frame` once a frame is ready, `failed(reason)` when playback breaks.
    """

    __gsignals__ = {
        "first-frame": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "failed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    MAX_TARGETS = 4

    def __init__(self, path, muted=True, playing=False):
        super().__init__()
        self.path = path
        self.paintable = VideoPaintable()
        self.frames_rendered = 0
        self._targets = []
        self._render = None
        self._destroyed = False
        self._lock = threading.Lock()
        self._events_pending = False
        self._wake = threading.Condition()
        self._update = False
        self._running = True
        self._error = None

        # Contexts created from the display share textures and sync objects with GTK's renderer.
        # The render thread owns one; the other only labels the textures handed to GTK.
        display = Gdk.Display.get_default()
        self._context = display.create_gl_context()
        self._context.realize()
        self._texture_context = display.create_gl_context()
        self._texture_context.realize()

        self._mpv = Handle(
            vo="libmpv",
            hwdec=os.environ.get("REVERIE_HWDEC", "auto-safe"),
            loop_file="inf",
            pause="no" if playing else "yes",
            mute="yes",
            aid="no" if muted else "auto",
            sid="no",
            audio_display="no",
            profile="fast",
            keep_open="yes",
            # Wallpapers loop forever from local disk; no need to read far ahead.
            demuxer_max_bytes="32MiB",
            msg_level="all=warn",
        )
        ready = threading.Event()
        self._thread = threading.Thread(target=self._render_loop, args=(_native_display(display), ready),
                                        name="reverie-render", daemon=True)
        self._thread.start()
        ready.wait()
        if self._error:
            self._mpv.destroy()
            raise RuntimeError(f"mpv couldn't set up OpenGL rendering: {self._error}")

        # mpv calls this from its own threads; hop to the main loop, coalescing repeats.
        self._wakeup_cb = _WAKEUP(lambda _: self._schedule_events())
        mpv_set_wakeup_callback(self._mpv.ptr, self._wakeup_cb, None)
        self._mpv.command("loadfile", path)

    def _schedule_events(self):
        with self._lock:
            if self._events_pending or self._destroyed:
                return
            self._events_pending = True
        GLib.idle_add(self._drain_events)

    # MARK: Control

    def set_playing(self, playing):
        if self._mpv.ptr:
            self._mpv.set("pause", not playing)

    def set_muted(self, muted):
        if self._mpv.ptr:
            # A silent wallpaper doesn't open the audio device at all, so devices coming and going
            # (sleep, headphones) can't disturb playback.
            self._mpv.set("aid", "no" if muted else "auto")
            self._mpv.set("mute", False)

    def position(self):
        return self._mpv.get_float("time-pos") if self._mpv.ptr else None

    def hwdec(self):
        return self._mpv.get("hwdec-current") if self._mpv.ptr else None

    def destroy(self):
        with self._lock:
            if self._destroyed:
                return
            self._destroyed = True
        if self._mpv.ptr:
            mpv_set_wakeup_callback(self._mpv.ptr, _WAKEUP(), None)
        with self._wake:
            self._running = False
            self._wake.notify()
        self._thread.join()  # Frees the render context, which must happen before the core goes.
        self._mpv.destroy()
        self.paintable.set_texture(None)

    # MARK: Render thread

    def _render_loop(self, native, ready):
        self._context.make_current()
        try:
            self._gl = _GL()
            self._init_params = _GLInitParams(_get_proc_address, None)
            api = ctypes.c_char_p(b"opengl")
            params = [
                _RenderParam(RENDER_API_TYPE, ctypes.cast(api, _p)),
                _RenderParam(RENDER_OPENGL_INIT, ctypes.cast(ctypes.pointer(self._init_params), _p)),
            ]
            # VA-API needs the window system's display to hand decoded frames to OpenGL without a copy.
            if native:
                display = _p(native[1])
                params.append(_RenderParam(native[0], display))
            params.append(_RenderParam(0, None))
            render = _p()
            result = mpv_render_context_create(ctypes.byref(render), self._mpv.ptr,
                                               (_RenderParam * len(params))(*params))
            if result < 0:
                raise RuntimeError(mpv_error_string(result).decode())
            self._render = render
            self._update_cb = _WAKEUP(lambda _: self._poke())
            mpv_render_context_set_update_callback(self._render, self._update_cb, None)
        except (RuntimeError, OSError) as error:
            self._error = error
            Gdk.GLContext.clear_current()
            ready.set()
            return
        ready.set()

        while True:
            with self._wake:
                while not self._update and self._running:
                    self._wake.wait()
                if not self._running:
                    break
                self._update = False
            if mpv_render_context_update(self._render) & RENDER_UPDATE_FRAME:
                self._render_frame()

        mpv_render_context_set_update_callback(self._render, _WAKEUP(), None)
        mpv_render_context_free(self._render)
        self._render = None
        with self._lock:
            for target in [t for t in self._targets if not t.busy]:
                target.delete()
                self._targets.remove(target)
        Gdk.GLContext.clear_current()

    def _poke(self):
        with self._wake:
            self._update = True
            self._wake.notify()

    def _render_frame(self):
        width = int(self._mpv.get_float("dwidth") or 0)
        height = int(self._mpv.get_float("dheight") or 0)
        if width <= 0 or height <= 0:
            return
        target = self._free_target(width, height)
        if target is None:
            return  # GTK still holds every texture; drop this frame rather than stall.
        fbo = _GLFbo(target.fbo, width, height, _GL.RGBA8)
        flip = ctypes.c_int(0)
        params = (_RenderParam * 3)(
            _RenderParam(RENDER_OPENGL_FBO, ctypes.cast(ctypes.pointer(fbo), _p)),
            _RenderParam(RENDER_FLIP_Y, ctypes.cast(ctypes.pointer(flip), _p)),
            _RenderParam(0, None),
        )
        mpv_render_context_render(self._render, params)  # Waits for the frame's display time.
        target.sync = self._gl.FenceSync(_GL.SYNC_GPU_COMMANDS_COMPLETE, 0)
        self._gl.Flush()
        GLib.idle_add(self._publish, target, width, height, priority=GLib.PRIORITY_HIGH)

    def _free_target(self, width, height):
        with self._lock:
            for target in list(self._targets):
                if target.busy:
                    continue
                if (target.width, target.height) == (width, height):
                    target.release_sync()
                    target.busy = True
                    return target
                target.delete()
                self._targets.remove(target)
            if len(self._targets) >= self.MAX_TARGETS:
                return None
            target = _Target(self._gl, width, height)
            target.busy = True
            self._targets.append(target)
            return target

    # MARK: Main thread

    def _publish(self, target, width, height):
        """Hands a finished frame to GTK."""
        if self._destroyed:
            self._release(target)
            return GLib.SOURCE_REMOVE
        builder = Gdk.GLTextureBuilder()
        builder.set_context(self._texture_context)
        builder.set_id(target.texture)
        builder.set_width(width)
        builder.set_height(height)
        builder.set_format(Gdk.MemoryFormat.R8G8B8A8_PREMULTIPLIED)
        if target.sync:
            builder.set_sync(target.sync)
        # GTK calls the destroy notify once it's done with the texture. `data` only needs to be
        # non-NULL; the callback is a closure over the target.
        texture = builder.build(lambda *_: self._release(target), 1)
        self.paintable.set_texture(texture)
        self.frames_rendered += 1
        if self.frames_rendered == 1:
            self.emit("first-frame")
        return GLib.SOURCE_REMOVE

    def _release(self, target):
        """GTK is done sampling this texture; it can take another frame. The render thread deletes
        the old sync object when it reuses the target."""
        with self._lock:
            target.busy = False
            orphaned = self._destroyed and not self._thread.is_alive()
            if orphaned and target in self._targets:
                self._targets.remove(target)
        if orphaned:
            self._texture_context.make_current()
            target.delete()

    # MARK: Events

    def _drain_events(self):
        with self._lock:
            self._events_pending = False
        while self._mpv.ptr and not self._destroyed:
            event, reason = self._mpv.wait_event(0)
            if event == 0:
                break
            if event == EVENT_END_FILE and reason == END_FILE_REASON_ERROR:
                self.emit("failed", "the video couldn't be decoded")
            elif event == EVENT_FILE_LOADED:
                log.info("Playing %s (hwdec: %s)", os.path.basename(self.path), self.hwdec())
        return GLib.SOURCE_REMOVE
