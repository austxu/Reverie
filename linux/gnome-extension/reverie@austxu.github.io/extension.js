// Reverie for GNOME Shell.
//
// The Reverie app draws the video into one ordinary window per monitor, titled
// "reverie-wallpaper|<connector>". This extension keeps those windows minimized and out of the
// window lists, and shows a clone of each one inside every background actor for its monitor: the
// desktop, the overview's workspaces and the lock screen. It also tells the app when no part of the
// desktop is visible so playback can pause, and adds a panel menu with the app's main controls.

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import St from 'gi://St';

import {Extension, InjectionManager} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Background from 'resource:///org/gnome/shell/ui/background.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import * as Workspace from 'resource:///org/gnome/shell/ui/workspace.js';
import * as WorkspaceThumbnail from 'resource:///org/gnome/shell/ui/workspaceThumbnail.js';

const APP_ID = 'io.github.austxu.Reverie';
const SHELL_BUS_NAME = 'io.github.austxu.Reverie.Shell';
const OBJECT_PATH = '/io/github/austxu/Reverie';
const TITLE_PREFIX = 'reverie-wallpaper|';

const ReverieProxy = Gio.DBusProxy.makeProxyWrapper(`
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
</node>`);

function isWallpaperWindow(metaWindow) {
    return !!metaWindow?.title?.startsWith(TITLE_PREFIX) &&
        (metaWindow.get_wm_class() === APP_ID || metaWindow.get_gtk_application_id?.() === APP_ID);
}

/** Removes rectangle `r` from each rectangle in `rects`, returning what is left. */
function subtract(rects, r) {
    const out = [];
    for (const a of rects) {
        if (r.x >= a.x + a.width || r.x + r.width <= a.x || r.y >= a.y + a.height || r.y + r.height <= a.y) {
            out.push(a);
            continue;
        }
        const top = Math.max(a.y, r.y);
        const bottom = Math.min(a.y + a.height, r.y + r.height);
        if (r.y > a.y)
            out.push({x: a.x, y: a.y, width: a.width, height: r.y - a.y});
        if (r.y + r.height < a.y + a.height)
            out.push({x: a.x, y: r.y + r.height, width: a.width, height: a.y + a.height - r.y - r.height});
        if (r.x > a.x)
            out.push({x: a.x, y: top, width: r.x - a.x, height: bottom - top});
        if (r.x + r.width < a.x + a.width)
            out.push({x: r.x + r.width, y: top, width: a.x + a.width - r.x - r.width, height: bottom - top});
    }
    return out;
}

/** Tracks the app's wallpaper windows and mirrors them into every background actor. */
class WallpaperMirror {
    constructor() {
        this._actors = new Set();
        this._windows = new Map(); // MetaWindow -> {signals}
        this._pending = new Map(); // MetaWindow -> signal ids, for windows not yet identifiable
        this._displaySignals = [
            global.display.connect('window-created', (_d, w) => this._consider(w)),
        ];
        for (const actor of global.get_window_actors())
            this._consider(actor.meta_window);
    }

    destroy() {
        this._displaySignals.forEach(id => global.display.disconnect(id));
        for (const [window, {signals, timeout}] of this._windows) {
            signals.forEach(id => window.disconnect(id));
            if (timeout)
                GLib.source_remove(timeout);
        }
        this._windows.clear();
        for (const window of [...this._pending.keys()])
            this._stopWaiting(window);
        for (const actor of this._actors)
            this._removeClone(actor);
        this._actors.clear();
    }

    get hasWindows() {
        return this._windows.size > 0;
    }

    addBackgroundActor(actor) {
        if (this._actors.has(actor))
            return;
        this._actors.add(actor);
        actor.connect('destroy', () => this._actors.delete(actor));
        this._attach(actor);
    }

    _consider(window) {
        if (!window || this._windows.has(window) || this._pending.has(window))
            return;
        if (isWallpaperWindow(window)) {
            this._adopt(window);
            return;
        }
        if (window.title && window.get_wm_class())
            return;
        // A Wayland window's title and app ID can arrive just after the window; look again when they do.
        const recheck = () => {
            if (!isWallpaperWindow(window) && !(window.title && window.get_wm_class()))
                return;
            this._stopWaiting(window);
            if (isWallpaperWindow(window))
                this._adopt(window);
        };
        this._pending.set(window, [
            window.connect('notify::title', recheck),
            window.connect('notify::wm-class', recheck),
            window.connect('unmanaged', () => this._stopWaiting(window)),
        ]);
    }

    _stopWaiting(window) {
        this._pending.get(window)?.forEach(id => window.disconnect(id));
        this._pending.delete(window);
    }

    _adopt(window) {
        // Not hide_from_window_list(): Mutter won't minimize windows that are out of the window list.
        // The injections below keep these windows out of the overview, switchers and dash instead.
        window.stick();
        const actor = window.get_compositor_private();
        if (actor && !window.minimized && !(actor.width > 0))
            Main.wm.skipNextEffect(actor); // No map animation.
        const signals = [
            window.connect('shown', () => this._minimizeWhenReady(window)),
            window.connect('notify::minimized', () => this._minimizeWhenReady(window)),
            window.connect('unmanaged', () => this._forget(window)),
            window.connect('notify::title', () => this._refresh()),
        ];
        this._windows.set(window, {signals, timeout: 0});
        this._minimizeWhenReady(window);
        this._refresh();
    }

    /** A minimized window stops receiving configures, so it keeps the size it had. Minimize only
     * once it has drawn a full-screen frame (or after a short grace period). The window starts
     * transparent, so it's invisible until then. */
    _minimizeWhenReady(window) {
        const entry = this._windows.get(window);
        if (!entry || window.minimized || entry.timeout)
            return;
        let tries = 0;
        entry.timeout = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 50, () => {
            if (!this._windows.has(window))
                return GLib.SOURCE_REMOVE;
            const monitor = global.display.get_monitor_geometry(this._monitorFor(window));
            const surface = window.get_compositor_private()?.get_children().at(-1);
            const ready = window.fullscreen && surface?.width === monitor.width && surface?.height === monitor.height;
            if (!ready && ++tries < 60)
                return GLib.SOURCE_CONTINUE;
            entry.timeout = 0;
            this._keepMinimized(window);
            return GLib.SOURCE_REMOVE;
        });
    }

    _keepMinimized(window) {
        if (window.minimized)
            return;
        const actor = window.get_compositor_private();
        if (actor?.visible)
            Main.wm.skipNextEffect(actor);
        window.minimize();
    }

    _forget(window) {
        const entry = this._windows.get(window);
        if (!entry)
            return;
        entry.signals.forEach(id => window.disconnect(id));
        if (entry.timeout)
            GLib.source_remove(entry.timeout);
        this._windows.delete(window);
        this._refresh();
    }

    _monitorFor(window) {
        const connector = window.title.slice(TITLE_PREFIX.length);
        try {
            const index = global.backend.get_monitor_manager().get_monitor_for_connector(connector);
            if (index >= 0)
                return index;
        } catch (e) {
            // Fall back to where the window was placed.
        }
        return window.get_monitor();
    }

    _windowForMonitor(monitor) {
        for (const window of this._windows.keys()) {
            if (this._monitorFor(window) === monitor)
                return window;
        }
        return null;
    }

    _refresh() {
        for (const actor of this._actors)
            this._attach(actor);
    }

    _removeClone(actor) {
        actor._reverieClone?.destroy();
        actor._reverieClone = null;
    }

    _attach(actor) {
        const window = this._windowForMonitor(actor.monitor);
        const source = window?.get_compositor_private() ?? null;
        if (actor._reverieClone?.source === source && source)
            return;
        this._removeClone(actor);
        if (!source)
            return;
        const clone = new Clutter.Clone({source, x: 0, y: 0});
        clone.add_constraint(new Clutter.BindConstraint({source: actor, coordinate: Clutter.BindCoordinate.SIZE}));
        actor.add_child(clone);
        actor._reverieClone = clone;
    }
}

/** Reports to the app when no part of the desktop is visible on any monitor. */
class CoverageWatcher {
    constructor(onChange) {
        this._onChange = onChange;
        this.covered = false;
        this._timeout = 0;
        const queue = () => this._queue();
        this._signals = [
            [global.display, global.display.connect('restacked', queue)],
            [global.display, global.display.connect('window-created', queue)],
            [global.display, global.display.connect('grab-op-end', queue)],
            [global.workspace_manager, global.workspace_manager.connect('active-workspace-changed', queue)],
            [global.window_manager, global.window_manager.connect('size-changed', queue)],
            [global.window_manager, global.window_manager.connect('minimize', queue)],
            [global.window_manager, global.window_manager.connect('unminimize', queue)],
            [global.window_manager, global.window_manager.connect('map', queue)],
            [global.window_manager, global.window_manager.connect('destroy', queue)],
            [Main.overview, Main.overview.connect('showing', queue)],
            [Main.overview, Main.overview.connect('hidden', queue)],
            [Main.layoutManager, Main.layoutManager.connect('monitors-changed', queue)],
        ];
        // Window moves don't all have a cheap signal, so also look gently once a second.
        this._poll = GLib.timeout_add_seconds(GLib.PRIORITY_LOW, 1, () => {
            this._evaluate();
            return GLib.SOURCE_CONTINUE;
        });
        this._evaluate();
    }

    destroy() {
        this._signals.forEach(([object, id]) => object.disconnect(id));
        if (this._timeout)
            GLib.source_remove(this._timeout);
        GLib.source_remove(this._poll);
    }

    _queue() {
        if (this._timeout)
            return;
        this._timeout = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 150, () => {
            this._timeout = 0;
            this._evaluate();
            return GLib.SOURCE_REMOVE;
        });
    }

    _isCovered() {
        if (Main.overview.visible || Main.layoutManager.monitors.length === 0)
            return false;
        const workspace = global.workspace_manager.get_active_workspace();
        const windows = global.get_window_actors()
            .map(actor => actor.meta_window)
            .filter(w => w && !w.minimized && w.showing_on_its_workspace() && !isWallpaperWindow(w) &&
                w.get_window_type() !== Meta.WindowType.DESKTOP &&
                (w.is_on_all_workspaces() || w.get_workspace() === workspace));
        // The desktop counts as hidden when windows cover each monitor's work area; panels and docks
        // that reserve space (the top bar, a fixed dock) count as covering too.
        for (let i = 0; i < Main.layoutManager.monitors.length; i++) {
            let visible = [workspace.get_work_area_for_monitor(i)];
            for (const window of windows) {
                visible = subtract(visible, window.get_frame_rect());
                if (visible.length === 0)
                    break;
            }
            if (visible.length > 0)
                return false;
        }
        return true;
    }

    _evaluate() {
        const covered = this._isCovered();
        if (covered === this.covered)
            return;
        this.covered = covered;
        this._onChange(covered);
    }
}

const ReverieIndicator = GObject.registerClass(
class ReverieIndicator extends PanelMenu.Button {
    _init(extension) {
        super._init(0.5, 'Reverie');
        this._extension = extension;
        this.add_child(new St.Icon({
            gicon: Gio.icon_new_for_string(`${extension.path}/icons/reverie-symbolic.svg`),
            style_class: 'system-status-icon',
        }));

        this._toggle = new PopupMenu.PopupSwitchMenuItem('Live Wallpaper', false);
        this._toggle.connect('toggled', (_item, state) => extension.call('SetEnabled', state));
        this.menu.addMenuItem(this._toggle);

        this._status = new PopupMenu.PopupMenuItem('', {reactive: false, style_class: 'reverie-status'});
        this.menu.addMenuItem(this._status);

        this._recents = new PopupMenu.PopupBaseMenuItem({reactive: false, can_focus: false});
        this._strip = new St.BoxLayout({style_class: 'reverie-strip', x_expand: true});
        this._recents.add_child(this._strip);
        this.menu.addMenuItem(this._recents);

        this._pause = new PopupMenu.PopupMenuItem('Pause');
        this._pause.connect('activate', () => extension.call('TogglePause'));
        this.menu.addMenuItem(this._pause);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._open = new PopupMenu.PopupMenuItem('Open Reverie…');
        this._open.connect('activate', () => extension.openApp());
        this.menu.addMenuItem(this._open);
        this._quit = new PopupMenu.PopupMenuItem('Quit Reverie');
        this._quit.connect('activate', () => extension.call('Quit'));
        this.menu.addMenuItem(this._quit);

        this.update(null);
    }

    update(state) {
        const running = state !== null;
        this._toggle.visible = running;
        this._pause.visible = running && state.enabled;
        this._quit.visible = running;
        this._recents.visible = running && state.library.length > 0;
        this._open.label.text = running ? 'Open Reverie…' : 'Start Reverie';
        this._status.label.text = running ? state.status : 'Reverie isn’t running';
        if (!running)
            return;
        this._toggle.setToggleState(state.enabled);
        this._pause.label.text = state.paused ? 'Resume' : 'Pause';

        this._strip.destroy_all_children();
        for (const wallpaper of state.library.slice(0, 5)) {
            const selected = state.enabled && wallpaper.id === state.current;
            const tile = new St.Button({
                style_class: selected ? 'reverie-tile selected' : 'reverie-tile',
                accessible_name: wallpaper.title,
                can_focus: true,
            });
            if (GLib.file_test(wallpaper.thumbnail, GLib.FileTest.EXISTS)) {
                const uri = GLib.filename_to_uri(wallpaper.thumbnail, null);
                tile.set_child(new St.Bin({
                    style_class: 'reverie-thumb',
                    style: `background-image: url("${uri}");`,
                }));
            }
            tile.connect('clicked', () => this._extension.call('Select', wallpaper.id));
            this._strip.add_child(tile);
        }
    }
});

export default class ReverieExtension extends Extension {
    enable() {
        this._injections = new InjectionManager();
        this._mirror = new WallpaperMirror();
        this._injectShell();
        // Recreate the desktop backgrounds through the injected method so they get the video, and
        // adopt the ones that already exist elsewhere (the overview's workspaces).
        Main.layoutManager._updateBackgrounds();
        this._adoptExistingBackgrounds(Main.layoutManager.uiGroup);

        this._proxy = null;
        this._state = null;
        this._indicator = new ReverieIndicator(this);
        Main.panel.addToStatusArea(this.uuid, this._indicator);
        this._sessionId = Main.sessionMode.connect('updated', () => this._syncSessionMode());
        this._syncSessionMode();

        this._coverage = new CoverageWatcher(covered => this.call('SetCovered', covered));
        this._watch = Gio.bus_watch_name(Gio.BusType.SESSION, APP_ID, Gio.BusNameWatcherFlags.NONE,
            () => this._onAppAppeared(), () => this._onAppVanished());
        // Tells the app the Shell side is ready; it creates its wallpaper windows only then.
        this._ownName = Gio.bus_own_name(Gio.BusType.SESSION, SHELL_BUS_NAME, Gio.BusNameOwnerFlags.NONE,
            null, null, null);
    }

    disable() {
        Gio.bus_unown_name(this._ownName);
        Gio.bus_unwatch_name(this._watch);
        this._coverage.destroy();
        this._coverage = null;
        if (this._proxy && this._stateSignal)
            this._proxy.disconnectSignal(this._stateSignal);
        this._proxy = null;
        Main.sessionMode.disconnect(this._sessionId);
        this._indicator.destroy();
        this._indicator = null;
        this._injections.clear();
        this._injections = null;
        this._mirror.destroy();
        this._mirror = null;
        Main.layoutManager._updateBackgrounds();
    }

    _adoptExistingBackgrounds(actor) {
        if (actor instanceof Meta.BackgroundActor)
            this._mirror.addBackgroundActor(actor);
        for (const child of actor.get_children())
            this._adoptExistingBackgrounds(child);
    }

    _syncSessionMode() {
        this._indicator.visible = !Main.sessionMode.isLocked;
    }

    _injectShell() {
        const mirror = this._mirror;
        this._injections.overrideMethod(Background.BackgroundManager.prototype, '_createBackgroundActor',
            original => function (...args) {
                const actor = original.call(this, ...args);
                mirror.addBackgroundActor(actor);
                return actor;
            });

        // Keep the wallpaper windows out of the overview, window switchers and the dash.
        const hidden = original => function (window, ...args) {
            return !isWallpaperWindow(window) && original.call(this, window, ...args);
        };
        this._injections.overrideMethod(Workspace.Workspace.prototype, '_isOverviewWindow', hidden);
        this._injections.overrideMethod(WorkspaceThumbnail.WorkspaceThumbnail.prototype, '_isOverviewWindow',
            original => function (win, ...args) {
                return !isWallpaperWindow(win.get_meta_window?.() ?? win) && original.call(this, win, ...args);
            });
        this._injections.overrideMethod(Meta.Display.prototype, 'get_tab_list',
            original => function (...args) {
                return original.call(this, ...args).filter(w => !isWallpaperWindow(w));
            });
        this._injections.overrideMethod(Shell.App.prototype, 'get_windows',
            original => function (...args) {
                return original.call(this, ...args).filter(w => !isWallpaperWindow(w));
            });
        this._injections.overrideMethod(Shell.AppSystem.prototype, 'get_running',
            original => function (...args) {
                return original.call(this, ...args).filter(app => app.get_id() !== `${APP_ID}.desktop` ||
                    app.get_windows().length > 0);
            });
        const extension = this;
        this._injections.overrideMethod(Shell.App.prototype, 'activate',
            original => function (...args) {
                if (this.get_id() === `${APP_ID}.desktop` && extension._proxy) {
                    extension.call('ShowWindow');
                    return;
                }
                original.call(this, ...args);
            });
    }

    // MARK: The app

    _onAppAppeared() {
        this._proxy = new ReverieProxy(Gio.DBus.session, APP_ID, OBJECT_PATH, (proxy, error) => {
            if (error) {
                console.warn(`Reverie: couldn't connect to the app: ${error.message}`);
                return;
            }
            this._stateSignal = proxy.connectSignal('StateChanged', (_p, _sender, [json]) => this._setState(json));
            proxy.GetStateRemote(([json], err) => {
                if (!err)
                    this._setState(json);
            });
            this.call('SetCovered', this._coverage?.covered ?? false);
        });
    }

    _onAppVanished() {
        this._proxy = null;
        this._stateSignal = 0;
        this._setState(null);
    }

    _setState(json) {
        try {
            this._state = json ? JSON.parse(json) : null;
        } catch (e) {
            this._state = null;
        }
        this._indicator?.update(this._state);
    }

    call(method, ...args) {
        if (!this._proxy)
            return;
        this._proxy[`${method}Remote`](...args, (_result, error) => {
            if (error)
                console.warn(`Reverie: ${method} failed: ${error.message}`);
        });
    }

    openApp() {
        if (this._proxy) {
            this.call('ShowWindow');
            return;
        }
        const app = Shell.AppSystem.get_default().lookup_app(`${APP_ID}.desktop`);
        if (app) {
            app.launch(0, -1, Shell.AppLaunchGpu.APP_PREF);
            return;
        }
        try {
            GLib.spawn_command_line_async('reverie');
        } catch (e) {
            Main.notifyError('Reverie isn’t installed', 'Install the Reverie app to use live wallpapers.');
        }
    }
}
