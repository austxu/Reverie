"""The application: single instance, command line, actions, and the D-Bus API used by the GNOME
panel menu and the Shell extension."""

import json
import logging

from gi.repository import Adw, Gio, GLib

from .desktop import APP_ID
from .model import AppModel
from .window import ReverieWindow, SettingsDialog

log = logging.getLogger("reverie")

OBJECT_PATH = "/io/github/austxu/Reverie"
INTERFACE_XML = """
<node>
  <interface name="io.github.austxu.Reverie1">
    <method name="GetState"><arg type="s" name="state" direction="out"/></method>
    <method name="SetEnabled"><arg type="b" name="enabled" direction="in"/></method>
    <method name="TogglePause"/>
    <method name="Select"><arg type="s" name="id" direction="in"/></method>
    <method name="ShowWindow"/>
    <method name="SetCovered"><arg type="b" name="covered" direction="in"/></method>
    <method name="Quit"/>
    <signal name="StateChanged"><arg type="s" name="state"/></signal>
  </interface>
</node>
"""


class ReverieApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.model = None
        self.window = None
        self._registration = 0
        self._connection = None
        self._state_pending = False
        self.add_main_option("background", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Start without opening the window", None)
        self.add_main_option("verbose", ord("v"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Log playback details", None)
        self.connect("handle-local-options", self._on_local_options)
        self._background = False

    def _on_local_options(self, _app, options):
        if options.contains("verbose"):
            logging.getLogger().setLevel(logging.INFO)
        self._background = options.contains("background")
        return -1

    # MARK: Lifecycle

    def do_startup(self):
        Adw.Application.do_startup(self)
        # Keep running in the background with the window closed, like a menu bar app.
        self.hold()
        self.model = AppModel()
        self.model.connect("changed", lambda *_: self._schedule_state_signal())
        for name, callback, accels in (
            ("settings", lambda *_: self.show_settings(), ["<Control>comma"]),
            ("about", lambda *_: self.show_about(), []),
            ("quit", lambda *_: self.quit(), ["<Control>q"]),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)
        self.model.start()

    def do_activate(self):
        # The first launch with --background (Open at Login) stays out of the way.
        if self._background and self.window is None:
            self._background = False
            return
        self.show_window()

    def do_open(self, files, _n_files, _hint):
        paths = [f.get_path() for f in files if f.get_path()]
        if paths:
            self.model.add_file(paths[0])
        if not self._background:
            self.show_window()
        self._background = False

    def show_window(self):
        if self.window is None:
            self.window = ReverieWindow(self, self.model)
        self.window.present()

    def show_settings(self):
        self.show_window()
        SettingsDialog(self.model).present(self.window)

    def show_about(self):
        self.show_window()
        Adw.AboutDialog(application_name="Reverie", application_icon=APP_ID, developer_name="Austin Xu",
                        comments="Live video wallpapers", website="https://github.com/austxu/Reverie",
                        license_type=7).present(self.window)  # Gtk.License.MIT_X11

    # MARK: D-Bus

    def do_dbus_register(self, connection, object_path):
        Adw.Application.do_dbus_register(self, connection, object_path)
        node = Gio.DBusNodeInfo.new_for_xml(INTERFACE_XML)
        self._connection = connection
        self._registration = connection.register_object(OBJECT_PATH, node.interfaces[0], self._on_method_call)
        return True

    def do_dbus_unregister(self, connection, object_path):
        if self._registration:
            connection.unregister_object(self._registration)
            self._registration = 0
        Adw.Application.do_dbus_unregister(self, connection, object_path)

    def _on_method_call(self, _conn, _sender, _path, _iface, method, params, invocation):
        model = self.model
        args = params.unpack()
        if method == "GetState":
            invocation.return_value(GLib.Variant("(s)", (json.dumps(model.state()),)))
            return
        if method == "SetEnabled":
            model.set_enabled(args[0])
        elif method == "TogglePause":
            model.toggle_pause()
        elif method == "Select":
            model.select(args[0])
        elif method == "ShowWindow":
            self.show_window()
        elif method == "SetCovered":
            model.set_covered(args[0])
        elif method == "Quit":
            GLib.idle_add(self.quit)
        invocation.return_value(None)

    def _schedule_state_signal(self):
        # Coalesce bursts of changes (e.g. download progress) into one signal per main loop turn.
        if self._state_pending or not self._connection:
            return
        self._state_pending = True

        def emit():
            self._state_pending = False
            self._connection.emit_signal(None, OBJECT_PATH, "io.github.austxu.Reverie1", "StateChanged",
                                         GLib.Variant("(s)", (json.dumps(self.model.state()),)))
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(100, emit)
