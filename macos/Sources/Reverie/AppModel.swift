import AppKit
import AVFoundation
import Observation
import OSLog
import ServiceManagement
import UniformTypeIdentifiers

enum Scaling: String, CaseIterable, Identifiable {
    case fill, fit
    var id: Self { self }
    var title: String { self == .fill ? "Fill Screen" : "Fit to Screen" }
    var gravity: AVLayerVideoGravity { self == .fill ? .resizeAspectFill : .resizeAspect }
}

enum DownloadState: Equatable {
    case idle
    case needsTool(URL)
    case installingTool(Double)
    case downloading(Double)
    case failed(String)
}

@Observable
@MainActor
final class AppModel {
    static let shared = AppModel()

    private(set) var library: [Wallpaper] = []
    private(set) var currentID: UUID? { didSet { defaults.set(currentID?.uuidString, forKey: "currentID") } }
    private(set) var isEnabled = false { didSet { defaults.set(isEnabled, forKey: "enabled") } }
    private(set) var isPausedByUser = false { didSet { defaults.set(isPausedByUser, forKey: "userPaused") } }
    private(set) var pauseReasons: Set<PauseReason> = []
    private(set) var thumbnails: [UUID: NSImage] = [:]
    private(set) var downloadState: DownloadState = .idle
    /// A short-lived message shown in the menu, e.g. when a file can't be played.
    private(set) var notice: String?

    var scaling: Scaling = .fill {
        didSet { defaults.set(scaling.rawValue, forKey: "scaling"); engine.gravity = scaling.gravity; refreshDesktopPicture() }
    }
    var playAudio = false {
        didSet { defaults.set(playAudio, forKey: "playAudio"); engine.isMuted = !playAudio }
    }
    var pauseWhenCovered = true {
        didSet { defaults.set(pauseWhenCovered, forKey: "pauseWhenCovered"); monitor.pauseWhenCovered = pauseWhenCovered }
    }
    var pauseOnBattery = false {
        didSet { defaults.set(pauseOnBattery, forKey: "pauseOnBattery"); monitor.pauseOnBattery = pauseOnBattery }
    }
    var matchDesktopPicture = true {
        didSet {
            defaults.set(matchDesktopPicture, forKey: "matchDesktopPicture")
            matchDesktopPicture ? refreshDesktopPicture() : restoreDesktopPictures()
        }
    }
    var launchAtLogin: Bool {
        get { _launchAtLogin }
        set {
            do {
                if newValue { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
            } catch {
                showNotice("Couldn't change the login item. \(error.localizedDescription)")
            }
            _launchAtLogin = SMAppService.mainApp.status == .enabled
        }
    }
    private var _launchAtLogin = SMAppService.mainApp.status == .enabled

    var current: Wallpaper? { library.first { $0.id == currentID } }
    var isPlaying: Bool { isEnabled && !isPausedByUser && pauseReasons.isEmpty }

    var statusText: String {
        guard isEnabled, current != nil else { return "Off" }
        if isPausedByUser { return "Paused" }
        if let reason = pauseReasons.min() { return reason.message }
        let count = NSScreen.screens.count
        return count == 1 ? "Playing" : "Playing on \(count) displays"
    }

    private let log = Logger(subsystem: "com.austinxu.Reverie", category: "playback")
    private let defaults = UserDefaults.standard
    private let engine = WallpaperEngine()
    private let monitor = PauseMonitor()
    private let downloader = YouTubeDownloader()
    private var downloadTask: Task<Void, Never>?
    private var noticeTask: Task<Void, Never>?

    private init() {}

    // MARK: Lifecycle

    func start() {
        // Hook up the monitor before applying settings: their didSets trigger its first evaluation.
        monitor.isWallpaperVisible = { [engine] in engine.isVisibleOnAnyScreen }
        monitor.onChange = { [weak self] reasons in
            self?.pauseReasons = reasons
            self?.updatePlayback()
        }

        defaults.register(defaults: ["pauseWhenCovered": true, "matchDesktopPicture": true, "scaling": Scaling.fill.rawValue])
        scaling = Scaling(rawValue: defaults.string(forKey: "scaling") ?? "") ?? .fill
        playAudio = defaults.bool(forKey: "playAudio")
        pauseWhenCovered = defaults.bool(forKey: "pauseWhenCovered")
        pauseOnBattery = defaults.bool(forKey: "pauseOnBattery")
        matchDesktopPicture = defaults.bool(forKey: "matchDesktopPicture")
        isPausedByUser = defaults.bool(forKey: "userPaused")
        currentID = defaults.string(forKey: "currentID").flatMap(UUID.init)
        loadLibrary()

        if defaults.bool(forKey: "enabled"), current != nil {
            isEnabled = true
            apply()
        }
    }

    // MARK: Choosing wallpapers

    func chooseFile() {
        let panel = NSOpenPanel()
        panel.title = "Choose a Video"
        panel.prompt = "Set as Wallpaper"
        panel.allowedContentTypes = [.movie]
        panel.allowsMultipleSelection = false
        NSApp.activate()
        guard panel.runModal() == .OK, let url = panel.url else { return }
        add(fileAt: url)
    }

    func add(fileAt url: URL) {
        Task {
            let asset = AVURLAsset(url: url)
            let playable = (try? await asset.load(.isPlayable)) ?? false
            let hasVideo = !((try? await asset.loadTracks(withMediaType: .video)) ?? []).isEmpty
            guard playable, hasVideo else {
                showNotice("Reverie can't play “\(url.lastPathComponent)”. Try an MP4 or MOV file.")
                return
            }
            if let existing = library.first(where: { $0.resolvedURL()?.standardizedFileURL == url.standardizedFileURL }) {
                select(existing)
                return
            }
            let wallpaper = Wallpaper(
                id: UUID(), title: url.deletingPathExtension().lastPathComponent, source: .file,
                bookmark: try? url.bookmarkData(), path: url.path, addedAt: .now)
            insert(wallpaper)
        }
    }

    func select(_ wallpaper: Wallpaper) {
        guard wallpaper.id != currentID || !isEnabled else { return }
        currentID = wallpaper.id
        isEnabled = true
        isPausedByUser = false
        apply()
    }

    func remove(_ wallpaper: Wallpaper) {
        library.removeAll { $0.id == wallpaper.id }
        thumbnails[wallpaper.id] = nil
        Frames.removeCache(for: wallpaper.id)
        if wallpaper.isDownloaded { try? FileManager.default.removeItem(atPath: wallpaper.path) }
        saveLibrary()
        if wallpaper.id == currentID {
            if let next = library.first {
                currentID = nil
                select(next)
            } else {
                currentID = nil
                setEnabled(false)
            }
        }
    }

    func revealInFinder(_ wallpaper: Wallpaper) {
        guard let url = wallpaper.resolvedURL() else { return }
        NSWorkspace.shared.activateFileViewerSelecting([url])
    }

    func setEnabled(_ enabled: Bool) {
        guard enabled != isEnabled else { return }
        if enabled {
            guard current ?? library.first != nil else { chooseFile(); return }
            if current == nil { currentID = library.first?.id }
            isEnabled = true
            apply()
        } else {
            isEnabled = false
            engine.stop()
            restoreDesktopPictures()
        }
    }

    func togglePause() {
        isPausedByUser.toggle()
        updatePlayback()
    }

    // MARK: Downloading

    func submitLink(_ text: String) {
        guard let url = YouTubeDownloader.validate(text) else {
            downloadState = .failed(YouTubeDownloader.Failure.invalidLink.errorDescription!)
            return
        }
        if YouTubeDownloader.isToolInstalled { download(url) } else { downloadState = .needsTool(url) }
    }

    func installToolAndDownload() {
        guard case .needsTool(let url) = downloadState else { return }
        downloadState = .installingTool(0)
        downloadTask = Task {
            do {
                try await downloader.installTool { self.downloadState = .installingTool($0) }
                download(url)
            } catch {
                downloadState = .failed(error.localizedDescription)
            }
        }
    }

    func cancelDownload() {
        downloadTask?.cancel()
        downloader.cancel()
        downloadState = .idle
    }

    func dismissDownloadMessage() {
        if case .downloading = downloadState { return }
        if case .installingTool = downloadState { return }
        downloadState = .idle
    }

    private func download(_ link: URL) {
        let id = UUID()
        downloadState = .downloading(0)
        downloadTask = Task {
            do {
                let result = try await downloader.download(link, id: id) { fraction in
                    if case .downloading = self.downloadState { self.downloadState = .downloading(fraction) }
                }
                downloadState = .idle
                insert(Wallpaper(id: id, title: result.title, source: .youtube(link), bookmark: nil,
                                 path: result.file.path, addedAt: .now))
            } catch is CancellationError {
                downloadState = .idle
            } catch {
                if case .idle = downloadState { return }
                downloadState = .failed(error.localizedDescription)
            }
        }
    }

    // MARK: Internals

    private func insert(_ wallpaper: Wallpaper) {
        library.insert(wallpaper, at: 0)
        saveLibrary()
        select(wallpaper)
    }

    /// Puts the current wallpaper on screen: prepares its frames, then hands it to the engine.
    private func apply() {
        guard let wallpaper = current else { return }
        guard let url = wallpaper.resolvedURL() else {
            showNotice("“\(wallpaper.title)” can't be found. It may have been moved or deleted.")
            return
        }
        Task {
            await Frames.prepare(wallpaper, video: url)
            loadThumbnail(for: wallpaper)
            guard currentID == wallpaper.id, isEnabled else { return }
            engine.gravity = scaling.gravity
            engine.isMuted = !playAudio
            engine.show(videoURL: url, still: Storage.stillURL(for: wallpaper.id))
            updatePlayback()
            refreshDesktopPicture()
        }
    }

    private func updatePlayback() {
        guard engine.isPlaying != isPlaying else { return }
        engine.isPlaying = isPlaying
        log.notice("Playback \(self.isPlaying ? "resumed" : "paused", privacy: .public): \(self.statusText, privacy: .public)")
    }

    func thumbnail(for wallpaper: Wallpaper) -> NSImage? {
        if let image = thumbnails[wallpaper.id] { return image }
        return nil
    }

    private func loadThumbnail(for wallpaper: Wallpaper) {
        guard thumbnails[wallpaper.id] == nil,
              let image = NSImage(contentsOf: Storage.thumbnailURL(for: wallpaper.id)) else { return }
        thumbnails[wallpaper.id] = image
    }

    private func showNotice(_ message: String) {
        notice = message
        noticeTask?.cancel()
        noticeTask = Task {
            try? await Task.sleep(for: .seconds(5))
            if !Task.isCancelled { notice = nil }
        }
    }

    private func loadLibrary() {
        guard let data = try? Data(contentsOf: Storage.libraryFile),
              let saved = try? JSONDecoder().decode([Wallpaper].self, from: data) else { return }
        library = saved
        for wallpaper in saved {
            loadThumbnail(for: wallpaper)
            if thumbnails[wallpaper.id] == nil, let url = wallpaper.resolvedURL() {
                Task { await Frames.prepare(wallpaper, video: url); loadThumbnail(for: wallpaper) }
            }
        }
    }

    private func saveLibrary() {
        guard let data = try? JSONEncoder().encode(library) else { return }
        try? data.write(to: Storage.libraryFile, options: .atomic)
    }

    // MARK: Desktop picture

    /// Sets the wallpaper's first frame as the real desktop picture, so the lock screen,
    /// Mission Control and the moment before Reverie launches all match the video.
    private func refreshDesktopPicture() {
        guard matchDesktopPicture, isEnabled, let wallpaper = current else { return }
        let still = Storage.stillURL(for: wallpaper.id)
        guard FileManager.default.fileExists(atPath: still.path) else { return }
        rememberOriginalDesktopPictures()
        let options: [NSWorkspace.DesktopImageOptionKey: Any] = [
            .imageScaling: NSImageScaling.scaleProportionallyUpOrDown.rawValue,
            .allowClipping: true,
        ]
        for screen in NSScreen.screens {
            let pixels = CGSize(width: screen.frame.width * screen.backingScaleFactor,
                                height: screen.frame.height * screen.backingScaleFactor)
            let picture = scaling == .fit ? Frames.fittedStill(for: wallpaper.id, pixelSize: pixels) ?? still : still
            try? NSWorkspace.shared.setDesktopImageURL(picture, for: screen, options: options)
        }
    }

    private func rememberOriginalDesktopPictures() {
        guard defaults.dictionary(forKey: "originalDesktopPictures") == nil else { return }
        var originals: [String: String] = [:]
        for screen in NSScreen.screens {
            if let url = NSWorkspace.shared.desktopImageURL(for: screen), !url.path.hasPrefix(Storage.stills.path) {
                originals[String(screen.displayID)] = url.path
            }
        }
        defaults.set(originals, forKey: "originalDesktopPictures")
    }

    private func restoreDesktopPictures() {
        guard let originals = defaults.dictionary(forKey: "originalDesktopPictures") as? [String: String] else { return }
        let fallback = originals.values.first
        for screen in NSScreen.screens {
            guard let path = originals[String(screen.displayID)] ?? fallback else { continue }
            let options = NSWorkspace.shared.desktopImageOptions(for: screen) ?? [:]
            try? NSWorkspace.shared.setDesktopImageURL(URL(fileURLWithPath: path), for: screen, options: options)
        }
        defaults.removeObject(forKey: "originalDesktopPictures")
    }
}
