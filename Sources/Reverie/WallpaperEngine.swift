import AppKit
import AVFoundation
import QuartzCore

/// A borderless window that lives at desktop level on one screen: above the system wallpaper,
/// below Finder's desktop icons and every normal window, present on all Spaces, click-through.
final class WallpaperWindow: NSWindow {
    let displayID: CGDirectDisplayID
    private let backdrop = CALayer()

    init(screen: NSScreen) {
        displayID = screen.displayID
        super.init(contentRect: screen.frame, styleMask: .borderless, backing: .buffered, defer: false)
        level = NSWindow.Level(Int(CGWindowLevelForKey(.desktopWindow)))
        collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle, .fullScreenNone]
        ignoresMouseEvents = true
        isReleasedWhenClosed = false
        hasShadow = false
        isOpaque = true
        backgroundColor = .black
        animationBehavior = .none
        setFrame(screen.frame, display: false)

        let view = NSView(frame: NSRect(origin: .zero, size: screen.frame.size))
        view.wantsLayer = true
        view.layer = CALayer()
        view.layer?.backgroundColor = NSColor.black.cgColor
        backdrop.frame = view.bounds
        backdrop.autoresizingMask = [.layerWidthSizable, .layerHeightSizable]
        backdrop.contentsGravity = .resizeAspectFill
        view.layer?.addSublayer(backdrop)
        contentView = view
    }

    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }

    var playerLayers: [AVPlayerLayer] {
        contentView?.layer?.sublayers?.compactMap { $0 as? AVPlayerLayer } ?? []
    }

    /// The image under the video: the still frame (so there is never a black flash) or, when the
    /// video is fitted, a blurred copy that fills the bands around it.
    func setBackdrop(_ image: CGImage?, animated: Bool) {
        CATransaction.begin()
        CATransaction.setDisableActions(!animated)
        CATransaction.setAnimationDuration(0.7)
        backdrop.contents = image
        CATransaction.commit()
    }

    /// Adds a layer for `player` on top of whatever is currently showing.
    func addLayer(for player: AVPlayer, gravity: AVLayerVideoGravity, visible: Bool) -> AVPlayerLayer {
        let layer = AVPlayerLayer(player: player)
        layer.frame = contentView?.bounds ?? .zero
        layer.autoresizingMask = [.layerWidthSizable, .layerHeightSizable]
        layer.videoGravity = gravity
        layer.opacity = visible ? 1 : 0
        contentView?.layer?.addSublayer(layer)
        return layer
    }

    func setGravity(_ gravity: AVLayerVideoGravity) {
        CATransaction.begin()
        CATransaction.setAnimationDuration(0.5)
        playerLayers.forEach { $0.videoGravity = gravity }
        CATransaction.commit()
    }
}

extension NSScreen {
    var displayID: CGDirectDisplayID {
        (deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value ?? 0
    }
}

/// Owns the wallpaper windows and a single looping player shared by all screens,
/// so the video is decoded once no matter how many displays are attached.
@MainActor
final class WallpaperEngine {
    private var windows: [CGDirectDisplayID: WallpaperWindow] = [:]
    private var player: AVQueuePlayer?
    private var looper: AVPlayerLooper?
    private var readyObservation: NSKeyValueObservation?
    private var still: CGImage?
    private var blurredStill: CGImage?

    var gravity: AVLayerVideoGravity = .resizeAspectFill {
        didSet {
            guard gravity != oldValue else { return }
            windows.values.forEach { $0.setGravity(gravity); $0.setBackdrop(backdropImage, animated: true) }
        }
    }

    private var backdropImage: CGImage? { gravity == .resizeAspect ? blurredStill : still }

    var isMuted = true {
        didSet { player?.isMuted = isMuted }
    }

    /// Whether the video should currently be advancing. The app model decides this.
    var isPlaying = false {
        didSet { applyRate() }
    }

    var isActive: Bool { player != nil }

    /// True when at least one wallpaper window has any visible pixels.
    var isVisibleOnAnyScreen: Bool {
        windows.values.contains { $0.occlusionState.contains(.visible) }
    }

    init() {
        NotificationCenter.default.addObserver(
            forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self, self.isActive else { return }
                self.syncWindowsWithScreens()
            }
        }
    }

    /// Shows `url` on every screen, crossfading from the previous video if there was one.
    func show(videoURL url: URL, still stillURL: URL?) {
        still = stillURL.flatMap { NSImage(contentsOf: $0)?.cgImage(forProposedRect: nil, context: nil, hints: nil) }
        blurredStill = still.flatMap(Frames.blurred)
        let firstAppearance = windows.isEmpty
        syncWindowsWithScreens()

        let item = AVPlayerItem(asset: AVURLAsset(url: url))
        let newPlayer = AVQueuePlayer()
        newPlayer.isMuted = isMuted
        newPlayer.preventsDisplaySleepDuringVideoPlayback = false
        newPlayer.allowsExternalPlayback = false
        newPlayer.actionAtItemEnd = .advance
        let newLooper = AVPlayerLooper(player: newPlayer, templateItem: item)

        let oldPlayer = player
        let layers = windows.values.map { $0.addLayer(for: newPlayer, gravity: gravity, visible: false) }
        player = newPlayer
        looper = newLooper
        applyRate()

        if firstAppearance {
            windows.values.forEach { $0.alphaValue = 0; $0.orderFront(nil) }
        }

        // Fade the new video in once its first frame is on screen (or after a short grace period).
        var revealed = false
        let reveal = { [weak self] in
            guard let self, !revealed, self.player === newPlayer else { return }
            revealed = true
            self.readyObservation = nil
            CATransaction.begin()
            CATransaction.setAnimationDuration(firstAppearance ? 0 : 0.7)
            CATransaction.setAnimationTimingFunction(CAMediaTimingFunction(name: .easeInEaseOut))
            CATransaction.setCompletionBlock { [weak self] in
                MainActor.assumeIsolated {
                    oldPlayer?.pause()
                    // A newer video may have started fading in meanwhile; leave its layers alone.
                    guard let self, self.player === newPlayer else { return }
                    for window in self.windows.values {
                        window.playerLayers.filter { $0.player !== newPlayer }.forEach { $0.removeFromSuperlayer() }
                    }
                }
            }
            layers.forEach { $0.opacity = 1 }
            // Crossfade the backdrop together with the video (it shows around a fitted video).
            self.windows.values.forEach { $0.setBackdrop(self.backdropImage, animated: !firstAppearance) }
            CATransaction.commit()
            if firstAppearance { self.fadeWindows(to: 1) }
        }
        readyObservation = layers.first?.observe(\.isReadyForDisplay, options: [.initial, .new]) { layer, _ in
            if layer.isReadyForDisplay { DispatchQueue.main.async { MainActor.assumeIsolated { reveal() } } }
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { MainActor.assumeIsolated { reveal() } }
    }

    /// Fades the wallpaper out and tears everything down, revealing the system desktop picture.
    func stop() {
        readyObservation = nil
        let closing = Array(windows.values)
        windows.removeAll()
        let oldPlayer = player
        player = nil
        looper = nil
        NSAnimationContext.runAnimationGroup({ context in
            context.duration = 0.6
            closing.forEach { $0.animator().alphaValue = 0 }
        }, completionHandler: {
            MainActor.assumeIsolated {
                oldPlayer?.pause()
                closing.forEach { $0.close() }
            }
        })
    }

    private func applyRate() {
        guard let player else { return }
        if isPlaying { player.play() } else { player.pause() }
    }

    private func fadeWindows(to alpha: CGFloat) {
        NSAnimationContext.runAnimationGroup { context in
            context.duration = 0.8
            context.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
            windows.values.forEach { $0.animator().alphaValue = alpha }
        }
    }

    /// Creates, moves, or removes windows so there is exactly one per connected screen.
    private func syncWindowsWithScreens() {
        let screens = NSScreen.screens
        let ids = Set(screens.map(\.displayID))
        for (id, window) in windows where !ids.contains(id) {
            window.close()
            windows[id] = nil
        }
        for screen in screens {
            if let window = windows[screen.displayID] {
                window.setFrame(screen.frame, display: true)
                continue
            }
            let window = WallpaperWindow(screen: screen)
            window.setBackdrop(player == nil ? still : backdropImage, animated: false)
            if let player { _ = window.addLayer(for: player, gravity: gravity, visible: true) }
            windows[screen.displayID] = window
            if player != nil { window.orderFront(nil) }
        }
    }
}
