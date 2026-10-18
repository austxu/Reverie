"""Watches the session and reports why the wallpaper shouldn't be playing right now."""

import logging
from enum import IntEnum

from gi.repository import Gio, GLib

log = logging.getLogger("reverie.pause")


class PauseReason(IntEnum):
    """Reasons the wallpaper pauses on its own, ordered by how they're reported to the user."""

    LOCKED = 0
    ASLEEP = 1
    LOW_POWER = 2
    BATTERY = 3
    COVERED = 4

    @property
    def message(self):
        return {
            PauseReason.LOCKED: "Paused while locked",
            PauseReason.ASLEEP: "Paused while the display sleeps",
            PauseReason.LOW_POWER: "Paused in Power Saver mode",
            PauseReason.BATTERY: "Paused on battery power",
            PauseReason.COVERED: "Paused while the desktop is hidden",
        }[self]


def _proxy(bus, name, path, interface, on_change):
    """A properties proxy that calls `on_change` whenever its properties change. None if unavailable."""
    try:
        proxy = Gio.DBusProxy.new_for_bus_sync(
            bus, Gio.DBusProxyFlags.DO_NOT_AUTO_START, None, name, path, interface, None)
    except GLib.Error as error:
        log.debug("No %s: %s", interface, error.message)
        return None
    proxy.connect("g-properties-changed", lambda *_: on_change())
    return proxy


def _get(proxy, name):
    if proxy is None:
        return None
    value = proxy.get_cached_property(name)
    return value.unpack() if value is not None else None


class PauseMonitor:
    """Combines lock, sleep, power and visibility state into a set of PauseReasons.

    Sources: logind (lock and suspend), the session's screensaver interface, GNOME's display power
    state, UPower (battery), power-profiles-daemon (Power Saver), and the compositor integration,
    which reports when no part of the wallpaper is visible.
    """

    def __init__(self, on_change):
        self.on_change = on_change
        self.pause_when_covered = True
        self.pause_on_battery = False
        self.reasons = set()
        self._covered = False
        self._screensaver = False
        self._sleeping = False
        self._subscriptions = []

        evaluate = self.evaluate
        system = Gio.BusType.SYSTEM
        session = Gio.BusType.SESSION
        self._session = _proxy(system, "org.freedesktop.login1", "/org/freedesktop/login1/session/auto",
                               "org.freedesktop.login1.Session", evaluate)
        self._upower = _proxy(system, "org.freedesktop.UPower", "/org/freedesktop/UPower",
                              "org.freedesktop.UPower", evaluate)
        self._profiles = (_proxy(system, "org.freedesktop.UPower.PowerProfiles", "/org/freedesktop/UPower/PowerProfiles",
                                 "org.freedesktop.UPower.PowerProfiles", evaluate)
                          or _proxy(system, "net.hadess.PowerProfiles", "/net/hadess/PowerProfiles",
                                    "net.hadess.PowerProfiles", evaluate))
        self._display = _proxy(session, "org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig",
                               "org.gnome.Mutter.DisplayConfig", evaluate)

        try:
            system_bus = Gio.bus_get_sync(system, None)
            self._subscribe(system_bus, "org.freedesktop.login1.Manager", "PrepareForSleep", self._on_sleep)
        except GLib.Error:
            pass
        session_bus = Gio.bus_get_sync(session, None)
        for interface in ("org.gnome.ScreenSaver", "org.freedesktop.ScreenSaver"):
            self._subscribe(session_bus, interface, "ActiveChanged", self._on_screensaver)

    def _subscribe(self, bus, interface, signal, handler):
        def callback(_conn, _sender, _path, _iface, _signal, params):
            handler(params.unpack()[0])
        self._subscriptions.append(bus.signal_subscribe(None, interface, signal, None, None,
                                                        Gio.DBusSignalFlags.NONE, callback))

    def _on_sleep(self, sleeping):
        self._sleeping = sleeping
        self.evaluate()

    def _on_screensaver(self, active):
        self._screensaver = active
        self.evaluate()

    def set_covered(self, covered):
        self._covered = covered
        self.evaluate()

    @property
    def on_battery(self):
        return bool(_get(self._upower, "OnBattery"))

    def evaluate(self):
        reasons = set()
        if self._screensaver or _get(self._session, "LockedHint"):
            reasons.add(PauseReason.LOCKED)
        power_save = _get(self._display, "PowerSaveMode")
        if self._sleeping or (power_save is not None and power_save != 0):
            reasons.add(PauseReason.ASLEEP)
        if _get(self._profiles, "ActiveProfile") == "power-saver":
            reasons.add(PauseReason.LOW_POWER)
        if self.pause_on_battery and self.on_battery:
            reasons.add(PauseReason.BATTERY)
        if self.pause_when_covered and not reasons and self._covered:
            reasons.add(PauseReason.COVERED)
        if reasons != self.reasons:
            self.reasons = reasons
            self.on_change(reasons)
