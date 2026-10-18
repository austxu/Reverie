"""Downloads online videos with yt-dlp. A yt-dlp already on the PATH is used as is; otherwise the
official standalone build is fetched from GitHub the first time the user asks for it."""

import os
import platform
import shutil
import threading
import urllib.request
from urllib.parse import urlparse

from gi.repository import Gio, GLib

from .library import Storage


class DownloadError(Exception):
    pass


def validate(text):
    """Returns the link as a URL string if it looks like a web address, else None."""
    text = text.strip()
    url = text if "://" in text else f"https://{text}"
    parsed = urlparse(url)
    if not parsed.scheme.startswith("http") or "." not in (parsed.hostname or ""):
        return None
    return url


def _bundled_tool():
    return os.path.join(Storage.bin(), "yt-dlp")


def tool_path():
    return shutil.which("yt-dlp") or (_bundled_tool() if os.access(_bundled_tool(), os.X_OK) else None)


def is_tool_installed():
    return tool_path() is not None


def _release_url():
    asset = "yt-dlp_linux_aarch64" if platform.machine() in ("aarch64", "arm64") else "yt-dlp_linux"
    return f"https://github.com/yt-dlp/yt-dlp/releases/latest/download/{asset}"


def install_tool(progress, done):
    """Downloads yt-dlp on a worker thread. Calls `progress(fraction)` and finally `done(error or None)`
    on the main loop."""

    def work():
        destination = _bundled_tool()
        tmp = destination + ".download"
        try:
            with urllib.request.urlopen(_release_url(), timeout=30) as response, open(tmp, "wb") as out:
                total = int(response.headers.get("Content-Length") or 0)
                received = 0
                while chunk := response.read(256 * 1024):
                    out.write(chunk)
                    received += len(chunk)
                    if total:
                        GLib.idle_add(progress, received / total)
            os.chmod(tmp, 0o755)
            os.replace(tmp, destination)
            GLib.idle_add(done, None)
        except Exception as error:  # noqa: BLE001 - reported to the user
            if os.path.exists(tmp):
                os.remove(tmp)
            GLib.idle_add(done, DownloadError(f"Couldn't install the downloader. {error}"))

    threading.Thread(target=work, daemon=True).start()


class Download:
    """One yt-dlp run. Reports progress, then `done(result, error)` where result is (path, title)."""

    # The best video-only H.264 stream (wallpapers are silent, and H.264 is hardware decoded almost
    # everywhere), then VP9 and AV1, which recent GPUs also decode, then anything playable.
    FORMAT = ("bv*[vcodec^=avc1][height<=2160]/bv*[vcodec^=vp09][height<=2160]/"
              "bv*[vcodec^=av01][height<=2160]/b[ext=mp4]/bv*/b")

    def __init__(self, link, wallpaper_id, progress, done):
        self._progress, self._done = progress, done
        self._title, self._path, self._errors = "Video", None, []
        self._cancelled = False
        self._finished = False
        self._exited = False
        self._streams_open = 2
        argv = [
            tool_path(),
            # `--print` implies `--quiet`, so progress has to be requested explicitly.
            "--no-playlist", "--no-warnings", "--newline", "--no-simulate", "--no-part", "--progress",
            "-f", self.FORMAT,
            "-o", os.path.join(Storage.videos(), f"{wallpaper_id}.%(ext)s"),
            "--progress-template",
            "download:REVERIE-PROGRESS %(progress.downloaded_bytes)s %(progress.total_bytes,progress.total_bytes_estimate)s",
            "--print", "before_dl:REVERIE-TITLE %(title)s",
            "--print", "after_move:REVERIE-PATH %(filepath)s",
            link,
        ]
        self._process = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE)
        self._read(Gio.DataInputStream.new(self._process.get_stdout_pipe()), self._on_line)
        self._read(Gio.DataInputStream.new(self._process.get_stderr_pipe()), self._errors.append)
        self._process.wait_async(None, self._on_exit)

    def _read(self, stream, handler):
        def on_line(source, result):
            try:
                line, _ = source.read_line_finish_utf8(result)
            except GLib.Error:
                line = None
            if line is None:
                self._streams_open -= 1
                self._maybe_finish()
                return
            handler(line)
            source.read_line_async(GLib.PRIORITY_DEFAULT, None, on_line)

        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, on_line)

    def _on_line(self, line):
        if line.startswith("REVERIE-PROGRESS "):
            parts = line.split()
            try:
                done, total = float(parts[1]), float(parts[2])
                if total > 0:
                    self._progress(min(done / total, 1))
            except (IndexError, ValueError):
                pass
        elif line.startswith("REVERIE-TITLE "):
            self._title = line[len("REVERIE-TITLE "):]
        elif line.startswith("REVERIE-PATH "):
            self._path = line[len("REVERIE-PATH "):]

    def _on_exit(self, process, result):
        try:
            process.wait_finish(result)
        except GLib.Error:
            pass
        self._exited = True
        self._maybe_finish()

    def _maybe_finish(self):
        # Wait for both the exit and the last lines of output.
        if self._finished or self._streams_open > 0 or not self._exited:
            return
        self._finished = True
        if self._cancelled:
            self._done(None, None)
        elif self._process.get_successful() and self._path and os.path.exists(self._path):
            self._done((self._path, self._title), None)
        else:
            self._done(None, DownloadError(self._friendly_error()))

    def cancel(self):
        self._cancelled = True
        self._process.force_exit()

    def _friendly_error(self):
        line = next((l for l in reversed(self._errors) if "ERROR" in l), "")
        if "Unsupported URL" in line:
            return "That site isn't supported."
        if "Private video" in line or "Sign in" in line:
            return "That video isn't publicly available."
        if "Requested format is not available" in line:
            return "No playable version of that video was found."
        if not line:
            return "The download didn't finish."
        return line.replace("ERROR: ", "")
