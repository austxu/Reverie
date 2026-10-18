"""The Reverie window: the same controls as the macOS menu, plus the settings dialog."""

from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gsk, Gtk

from .desktop import supports_desktop_picture

_CSS = b"""
.reverie-card { border-radius: 12px; background: alpha(currentColor, 0.06); }
.reverie-card.drop-target { box-shadow: inset 0 0 0 2px var(--accent-bg-color, @accent_bg_color); }
.reverie-card picture.off { filter: grayscale(1) opacity(0.55); }
.reverie-shade { background: linear-gradient(to bottom, transparent, alpha(black, 0.45)); padding: 10px; }
.reverie-title { color: white; font-weight: 500; text-shadow: 0 1px 2px alpha(black, 0.35); }
.reverie-tile { padding: 2px; border-radius: 8px; min-width: 0; min-height: 0; }
.reverie-tile.selected { box-shadow: inset 0 0 0 2px var(--accent-bg-color, @accent_bg_color); }
.reverie-add { border-radius: 6px; min-width: 64px; min-height: 40px; padding: 0; margin: 2px; }
.reverie-box { border-radius: 8px; background: alpha(currentColor, 0.06); padding: 10px; }
"""

VIDEO_TYPES = ["video/*"]


class Thumbnail(Gtk.Widget):
    """A fixed-size, rounded thumbnail that fills its frame (Gtk.Picture would ask for the
    texture's full size)."""

    def __init__(self, texture, width, height, radius=6):
        super().__init__()
        self.texture, self.width, self.height, self.radius = texture, width, height, radius

    def do_measure(self, orientation, _for_size):
        size = self.width if orientation == Gtk.Orientation.HORIZONTAL else self.height
        return size, size, -1, -1

    def do_snapshot(self, snapshot):
        width, height = self.get_width(), self.get_height()
        bounds = Graphene.Rect().init(0, 0, width, height)
        rounded = Gsk.RoundedRect()
        rounded.init_from_rect(bounds, self.radius)
        snapshot.push_rounded_clip(rounded)
        if self.texture is None:
            snapshot.append_color(Gdk.RGBA(red=0.5, green=0.5, blue=0.5, alpha=0.2), bounds)
        else:
            tw, th = self.texture.get_width(), self.texture.get_height()
            scale = max(width / tw, height / th)
            w, h = tw * scale, th * scale
            snapshot.append_scaled_texture(self.texture, Gsk.ScalingFilter.TRILINEAR,
                                           Graphene.Rect().init((width - w) / 2, (height - h) / 2, w, h))
        snapshot.pop()


class ReverieWindow(Adw.ApplicationWindow):
    def __init__(self, app, model):
        super().__init__(application=app, title="Reverie", default_width=380, resizable=False)
        self.model = model
        provider = Gtk.CssProvider()
        provider.load_from_data(_CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        # Closing the window leaves the wallpaper running, like closing the macOS menu.
        self.set_hide_on_close(True)

        header = Adw.HeaderBar()
        menu = Gio.Menu()
        menu.append("Settings…", "app.settings")
        menu.append("About Reverie", "app.about")
        menu.append("Quit Reverie", "app.quit")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Menu"))

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                       margin_start=14, margin_end=14, margin_bottom=14, margin_top=2)
        body.append(self._build_header())
        body.append(self._build_preview())
        self.recents = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER,
                                          hscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        self.recents.set_propagate_natural_height(True)
        body.append(self.recents)
        self.link_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(self.link_area)
        self.notice = Gtk.Label(wrap=True, xalign=0, css_classes=["dim-label"])
        self.notice_row = Gtk.Box(spacing=8, visible=False)
        self.notice_row.append(Gtk.Image(icon_name="dialog-information-symbolic", valign=Gtk.Align.START))
        self.notice_row.append(self.notice)
        body.append(self.notice_row)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(body)
        self.set_content(view)

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        drop.connect("enter", lambda *_: self.card.add_css_class("drop-target") or Gdk.DragAction.COPY)
        drop.connect("leave", lambda *_: self.card.remove_css_class("drop-target"))
        self.add_controller(drop)

        for name, handler in (("show-in-files", self.show_in_files), ("remove", model.remove)):
            action = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))
            action.connect("activate", lambda _a, param, h=handler: h(param.get_string()))
            self.add_action(action)

        self._link_state = None
        self._recents_key = None
        model.connect("changed", lambda *_: self.refresh())
        self.refresh()

    # MARK: Building

    def _build_header(self):
        row = Gtk.Box(spacing=8)
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        labels.append(Gtk.Label(label="Live Wallpaper", xalign=0, css_classes=["heading"]))
        self.status = Gtk.Label(xalign=0, css_classes=["dim-label", "caption"])
        labels.append(self.status)
        row.append(labels)
        self.switch = Gtk.Switch(valign=Gtk.Align.CENTER, tooltip_text="Live Wallpaper")
        self._switch_handler = self.switch.connect("notify::active", self._on_switch)
        row.append(self.switch)
        return row

    def _build_preview(self):
        self.card = Gtk.Overlay(css_classes=["reverie-card"], overflow=Gtk.Overflow.HIDDEN)
        frame = Gtk.AspectFrame(ratio=16 / 10, obey_child=False, child=self.card)

        self.preview = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        self.card.set_child(self.preview)

        empty = Gtk.Button(css_classes=["flat"], hexpand=True, vexpand=True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, valign=Gtk.Align.CENTER)
        box.append(Gtk.Image(icon_name="video-x-generic-symbolic", pixel_size=32, css_classes=["dim-label"]))
        box.append(Gtk.Label(label="Choose a Video", css_classes=["heading"]))
        box.append(Gtk.Label(label="or drop a video file here", css_classes=["dim-label", "caption"]))
        empty.set_child(box)
        empty.connect("clicked", lambda *_: self.choose_file())
        self.empty = empty
        self.card.add_overlay(empty)

        shade = Gtk.Box(spacing=8, valign=Gtk.Align.END, css_classes=["reverie-shade"])
        self.title = Gtk.Label(xalign=0, hexpand=True, ellipsize=3, css_classes=["reverie-title"],
                               valign=Gtk.Align.END)
        shade.append(self.title)
        self.pause = Gtk.Button(css_classes=["circular", "osd"], valign=Gtk.Align.END)
        self.pause.connect("clicked", lambda *_: self.model.toggle_pause())
        shade.append(self.pause)
        self.shade = shade
        self.card.add_overlay(shade)
        return frame

    # MARK: Refreshing

    def refresh(self):
        model = self.model
        current = model.current
        self.status.set_label(model.status_text)
        with self.switch.handler_block(self._switch_handler):
            self.switch.set_active(model.enabled)

        thumbnail = model.thumbnails.get(current.id) if current else None
        self.preview.set_paintable(thumbnail)
        if model.enabled:
            self.preview.remove_css_class("off")
        else:
            self.preview.add_css_class("off")
        self.empty.set_visible(thumbnail is None)
        self.shade.set_visible(thumbnail is not None)
        if current:
            self.title.set_label(current.title)
        self.pause.set_visible(model.enabled)
        self.pause.set_icon_name("media-playback-start-symbolic" if model.paused_by_user
                                 else "media-playback-pause-symbolic")
        self.pause.set_tooltip_text("Play" if model.paused_by_user else "Pause")

        self._refresh_recents()
        self._refresh_link()
        self.notice_row.set_visible(model.notice is not None)
        self.notice.set_label(model.notice or "")

    def _refresh_recents(self):
        model = self.model
        key = ([w.id for w in model.library], [w.id in model.thumbnails for w in model.library],
               model.current_id if model.enabled else None)
        if key == self._recents_key:
            return
        self._recents_key = key
        self.recents.set_visible(bool(model.library))
        strip = Gtk.Box(spacing=8)
        for wallpaper in model.library:
            strip.append(self._tile(wallpaper))
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add a Video…", css_classes=["reverie-add"])
        add.connect("clicked", lambda *_: self.choose_file())
        strip.append(add)
        self.recents.set_child(strip)

    def _tile(self, wallpaper):
        model = self.model
        button = Gtk.Button(css_classes=["flat", "reverie-tile"], tooltip_text=wallpaper.title)
        button.set_child(Thumbnail(model.thumbnails.get(wallpaper.id), 64, 40))
        if wallpaper.id == model.current_id and model.enabled:
            button.add_css_class("selected")
        button.connect("clicked", lambda *_: model.select(wallpaper.id))

        menu = Gio.Menu()
        menu.append("Show in Files", f"win.show-in-files('{wallpaper.id}')")
        menu.append("Delete Download" if wallpaper.is_downloaded else "Remove from Reverie",
                    f"win.remove('{wallpaper.id}')")

        def show_menu(*_):
            popover = Gtk.PopoverMenu(menu_model=menu, has_arrow=True)
            popover.set_parent(button)
            popover.connect("closed", lambda p: GLib.idle_add(p.unparent))
            popover.popup()

        click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        click.connect("pressed", show_menu)
        button.add_controller(click)
        return button

    def _refresh_link(self):
        state = self.model.download_state
        if state == self._link_state:
            return
        kind = state[0]
        same_kind = self._link_state is not None and self._link_state[0] == kind
        self._link_state = state
        if same_kind and kind in ("installing", "downloading"):
            self._update_progress(state)
            return
        child = self.link_area.get_first_child()
        while child:
            self.link_area.remove(child)
            child = self.link_area.get_first_child()
        if kind == "idle":
            self.link_area.append(self._link_entry())
        elif kind == "needs-tool":
            self.link_area.append(self._consent())
        elif kind in ("installing", "downloading"):
            self.link_area.append(self._progress_box())
            self._update_progress(state)
        elif kind == "failed":
            self.link_area.append(self._failure(state[1]))

    def _link_entry(self):
        entry = Gtk.Entry(placeholder_text="Paste a YouTube link", primary_icon_name="insert-link-symbolic")

        def update_icon(*_):
            has_text = bool(entry.get_text().strip())
            entry.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY,
                                          "folder-download-symbolic" if has_text else None)
            entry.set_icon_tooltip_text(Gtk.EntryIconPosition.SECONDARY, "Download and set as wallpaper")

        def submit(*_):
            if entry.get_text().strip():
                self.model.submit_link(entry.get_text())

        entry.connect("changed", update_icon)
        entry.connect("activate", submit)
        entry.connect("icon-press", lambda _e, pos: submit() if pos == Gtk.EntryIconPosition.SECONDARY else None)
        return entry

    def _consent(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, css_classes=["reverie-box"])
        box.append(Gtk.Label(label="Downloading from YouTube uses yt-dlp, a free, open-source tool. "
                                   "Install it now? (about 40 MB)", wrap=True, xalign=0))
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        not_now = Gtk.Button(label="Not Now")
        not_now.connect("clicked", lambda *_: self.model.cancel_download())
        install = Gtk.Button(label="Install", css_classes=["suggested-action"])
        install.connect("clicked", lambda *_: self.model.install_tool_and_download())
        buttons.append(not_now)
        buttons.append(install)
        box.append(buttons)
        return box

    def _progress_box(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["reverie-box"])
        row = Gtk.Box(spacing=8)
        self.progress_label = Gtk.Label(xalign=0, hexpand=True, css_classes=["numeric"])
        row.append(self.progress_label)
        cancel = Gtk.Button(icon_name="window-close-symbolic", css_classes=["flat", "circular"], tooltip_text="Cancel")
        cancel.connect("clicked", lambda *_: self.model.cancel_download())
        row.append(cancel)
        box.append(row)
        self.progress_bar = Gtk.ProgressBar()
        box.append(self.progress_bar)
        return box

    def _update_progress(self, state):
        kind, fraction = state
        if kind == "installing":
            self.progress_label.set_label("Installing downloader…")
        else:
            self.progress_label.set_label(f"Downloading… {int(fraction * 100)}%" if fraction > 0 else "Preparing download…")
        if fraction > 0:
            self.progress_bar.set_fraction(fraction)
        else:
            self.progress_bar.pulse()

    def _failure(self, message):
        box = Gtk.Box(spacing=8, css_classes=["reverie-box"])
        box.append(Gtk.Image(icon_name="dialog-warning-symbolic", css_classes=["warning"], valign=Gtk.Align.START))
        box.append(Gtk.Label(label=message, wrap=True, xalign=0, hexpand=True))
        ok = Gtk.Button(label="OK", valign=Gtk.Align.CENTER)
        ok.connect("clicked", lambda *_: self.model.dismiss_download_message())
        box.append(ok)
        return box

    # MARK: Actions

    def _on_switch(self, switch, _pspec):
        if switch.get_active() and self.model.current is None and not self.model.library:
            with switch.handler_block(self._switch_handler):
                switch.set_active(False)
            self.choose_file()
            return
        self.model.set_enabled(switch.get_active())

    def _on_drop(self, _target, files, _x, _y):
        self.card.remove_css_class("drop-target")
        paths = [f.get_path() for f in files.get_files() if f.get_path()]
        if not paths:
            return False
        self.model.add_file(paths[0])
        return True

    def choose_file(self):
        dialog = Gtk.FileDialog(title="Choose a Video", accept_label="Set as Wallpaper")
        video = Gtk.FileFilter(name="Videos")
        for mime in VIDEO_TYPES:
            video.add_mime_type(mime)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(video)
        dialog.set_filters(filters)
        dialog.set_default_filter(video)

        def done(dialog, result):
            try:
                file = dialog.open_finish(result)
            except GLib.Error:
                return
            if file and file.get_path():
                self.model.add_file(file.get_path())

        dialog.open(self, None, done)

    def show_in_files(self, wallpaper_id):
        wallpaper = next((w for w in self.model.library if w.id == wallpaper_id), None)
        if wallpaper and wallpaper.resolved_path():
            Gtk.FileLauncher(file=Gio.File.new_for_path(wallpaper.path)).open_containing_folder(self, None, None)


class SettingsDialog(Adw.PreferencesDialog):
    def __init__(self, model):
        super().__init__(title="Settings", content_width=440, search_enabled=False)
        self.model = model
        page = Adw.PreferencesPage()
        self.add(page)

        general = Adw.PreferencesGroup()
        login = Adw.SwitchRow(title="Open at Login", active=model.launch_at_login)
        login.connect("notify::active", lambda row, _: model.set_launch_at_login(row.get_active()))
        general.add(login)
        page.add(general)

        playback = Adw.PreferencesGroup(title="Playback")
        scaling = Adw.ComboRow(title="Scaling", model=Gtk.StringList.new(["Fill Screen", "Fit to Screen"]))
        scaling.set_selected(1 if model.settings["scaling"] == "fit" else 0)
        self.scaling_footer = playback

        def on_scaling(row, _):
            model.set_setting("scaling", "fit" if row.get_selected() == 1 else "fill")
            self._describe_scaling()

        scaling.connect("notify::selected", on_scaling)
        playback.add(scaling)
        sound = Adw.SwitchRow(title="Play Sound", active=model.settings["play_audio"])
        sound.connect("notify::active", lambda row, _: model.set_setting("play_audio", row.get_active()))
        playback.add(sound)
        page.add(playback)
        self._describe_scaling()

        pausing = Adw.PreferencesGroup(
            title="Pause Automatically",
            description="Reverie always pauses while your computer is locked, asleep, or in Power Saver mode.")
        covered = Adw.SwitchRow(title="When the desktop is hidden", active=model.settings["pause_when_covered"])
        covered.connect("notify::active", lambda row, _: model.set_setting("pause_when_covered", row.get_active()))
        pausing.add(covered)
        battery = Adw.SwitchRow(title="On battery power", active=model.settings["pause_on_battery"])
        battery.connect("notify::active", lambda row, _: model.set_setting("pause_on_battery", row.get_active()))
        pausing.add(battery)
        page.add(pausing)

        picture = Adw.PreferencesGroup(
            description="Uses a still from your video as the desktop and lock screen picture, so they match."
            if supports_desktop_picture() else "Matching the desktop picture is available on GNOME.")
        match = Adw.SwitchRow(title="Match desktop picture", active=model.settings["match_desktop_picture"],
                              sensitive=supports_desktop_picture())
        match.connect("notify::active", lambda row, _: model.set_setting("match_desktop_picture", row.get_active()))
        picture.add(match)
        page.add(picture)

    def _describe_scaling(self):
        self.scaling_footer.set_description(
            "Fill Screen trims the edges of videos shaped differently from your display."
            if self.model.settings["scaling"] != "fit"
            else "Fit to Screen shows the whole video, softly extending it to the edges.")
