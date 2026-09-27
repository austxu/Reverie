<p align="center">
  <img src="docs/icon.png" width="128" alt="Reverie icon">
</p>

<h1 align="center">Reverie</h1>

<p align="center">Live video wallpapers for macOS — a native menu bar app, inspired by <a href="https://github.com/jeffshee/gnome-ext-hanabi">Hanabi</a> for GNOME.</p>

<p align="center">
  <img src="docs/menu-light.png" width="300" alt="Reverie menu (light)">
  &nbsp;
  <img src="docs/menu-dark.png" width="300" alt="Reverie menu (dark)">
</p>

## Features

- **Any MP4 or MOV as your wallpaper** — plays beneath your desktop icons, on every Space and every display, and never intercepts clicks.
- **YouTube links** — paste a link and Reverie downloads a silent, hardware-friendly copy using [yt-dlp](https://github.com/yt-dlp/yt-dlp), installed on demand the first time you ask.
- **Smart pausing** — keeps playing while any part of the wallpaper is visible (even through the translucent menu bar and Dock), and pauses when it's fully hidden by a full-screen app, while locked or asleep, in Low Power Mode, and optionally on battery.
- **Fill or Fit** — Fit shows the whole video and extends it to the screen edges with a soft blur instead of black bars.
- **Matching lock screen** — sets a still from your video as the real desktop picture, so the lock screen and Mission Control match. Your original picture comes back when you turn Reverie off.
- **Small and quiet** — one shared hardware decoder for all displays. On an M2: ~0% CPU paused, ~1% for 1080p, ~2% for 4K60, 40–90 MB of memory.
- Crossfades between videos, recent-videos strip, drag and drop, Open With → Reverie, Open at Login, light and dark mode.

<p align="center">
  <img src="docs/settings-light.png" width="420" alt="Reverie settings">
</p>

## Requirements

- macOS 27 or later, Apple silicon
- To build: Xcode **or** just the Command Line Tools (`xcode-select --install`)

## Build and install

```bash
git clone https://github.com/austxu/Reverie.git
cd Reverie
./build.sh --install
```

This builds `build/Reverie.app` (ad-hoc signed), copies it to `/Applications`, and launches it. Use `./build.sh` alone to build without installing.

## Usage

1. Click the Reverie icon in the menu bar.
2. Click **+** (or **Choose a Video**), drag a video onto the menu, or right-click a file in Finder → **Open With → Reverie**. You can also paste a YouTube link.
3. Click a thumbnail to switch videos; right-click one to show it in Finder or remove it.
4. **Settings…** has scaling, sound, automatic pausing, desktop-picture matching, and Open at Login.

From the terminal: `open -a Reverie path/to/video.mov`.

**Tip:** for a perfect fit in either mode, use a video with the same shape as your display (e.g. 2560×1600 for a 16:10 MacBook) encoded as H.264 or HEVC. WebM isn't supported.

## How it works

Reverie places a borderless, click-through window at the desktop window level on each screen — above the system wallpaper, below Finder's icons — and plays the video there with `AVPlayerLayer`. A single `AVQueuePlayer` + `AVPlayerLooper` feeds every screen, so the video is decoded once. Visibility comes from the window server's occlusion state.

| File | Role |
|---|---|
| `Sources/Reverie/WallpaperEngine.swift` | Desktop-level windows, shared looping player, crossfades |
| `Sources/Reverie/PauseMonitor.swift` | Lock, sleep, power, and visibility → pause reasons |
| `Sources/Reverie/AppModel.swift` | App state, settings, library, desktop picture sync |
| `Sources/Reverie/YouTubeDownloader.swift` | On-demand yt-dlp install and downloads |
| `Sources/Reverie/MenuView.swift`, `SettingsView.swift` | SwiftUI interface |
| `tools/make_icon.swift`, `tools/make_test_video.swift` | Icon and sample-video generators |

## Development notes

- Use `@ViewState` rather than `@State` in views. In the macOS 27 SDK `@State` is a macro whose compiler plugin ships only with full Xcode; `ViewState` is a typealias to the same property wrapper, so the project also builds with just the Command Line Tools.
- Render the UI to PNGs (light and dark) without a screen-recording permission:
  `REVERIE_SNAPSHOT=/tmp/shots build/Reverie.app/Contents/MacOS/Reverie`
- Playback log: `/usr/bin/log show --last 5m --info --predicate 'process == "Reverie" AND category == "playback"'`
- Make a sample video: `swift tools/make_test_video.swift samples/test.mov`

## Limitations

- macOS doesn't allow custom video on the lock screen, so Reverie shows a matching still there.
- The matching desktop picture is applied to the current Space; the live video itself appears on all Spaces.
- Downloaded videos are for your personal use — respect the terms of the sites you download from.

## License

[MIT](LICENSE)
