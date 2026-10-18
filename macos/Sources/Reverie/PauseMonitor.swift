import AppKit
import IOKit.ps

/// Reasons the wallpaper pauses on its own. Ordered by how they're reported to the user.
enum PauseReason: Int, Comparable, CaseIterable {
    case locked, asleep, lowPower, battery, covered

    static func < (a: Self, b: Self) -> Bool { a.rawValue < b.rawValue }

    var message: String {
        switch self {
        case .locked: "Paused while locked"
        case .asleep: "Paused while the display sleeps"
        case .lowPower: "Paused in Low Power Mode"
        case .battery: "Paused on battery power"
        case .covered: "Paused while the desktop is hidden"
        }
    }
}

/// Watches the system and reports why the wallpaper shouldn't be playing right now.
@MainActor
final class PauseMonitor {
    var pauseWhenCovered = true { didSet { evaluate() } }
    var pauseOnBattery = false { didSet { evaluate() } }

    /// Lets the monitor ask the engine whether any wallpaper pixels are visible at all.
    var isWallpaperVisible: () -> Bool = { true }
    var onChange: (Set<PauseReason>) -> Void = { _ in }

    private(set) var reasons: Set<PauseReason> = []
    private var locked = false
    private var screensaver = false
    private var asleep = false
    private var timer: Timer?

    init() {
        let workspace = NSWorkspace.shared.notificationCenter
        let distributed = DistributedNotificationCenter.default()
        let observe: (NotificationCenter, String, @escaping () -> Void) -> Void = { center, name, action in
            center.addObserver(forName: Notification.Name(name), object: nil, queue: .main) { _ in
                MainActor.assumeIsolated { action(); self.evaluate() }
            }
        }
        observe(distributed, "com.apple.screenIsLocked") { self.locked = true }
        observe(distributed, "com.apple.screenIsUnlocked") { self.locked = false }
        observe(distributed, "com.apple.screensaver.didstart") { self.screensaver = true }
        observe(distributed, "com.apple.screensaver.didstop") { self.screensaver = false }
        observe(workspace, NSWorkspace.screensDidSleepNotification.rawValue) { self.asleep = true }
        observe(workspace, NSWorkspace.screensDidWakeNotification.rawValue) { self.asleep = false }
        observe(workspace, NSWorkspace.willSleepNotification.rawValue) { self.asleep = true }
        observe(workspace, NSWorkspace.didWakeNotification.rawValue) { self.asleep = false }
        observe(workspace, NSWorkspace.activeSpaceDidChangeNotification.rawValue) {}
        observe(workspace, NSWorkspace.didActivateApplicationNotification.rawValue) {}
        observe(NotificationCenter.default, Notification.Name.NSProcessInfoPowerStateDidChange.rawValue) {}
        observe(NotificationCenter.default, NSWindow.didChangeOcclusionStateNotification.rawValue) {}

        // The power source has no cheap notification, and occlusion can lag a Space switch, so also poll gently.
        let timer = Timer(timeInterval: 1.0, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.evaluate() }
        }
        timer.tolerance = 0.4
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
    }

    func evaluate() {
        var next: Set<PauseReason> = []
        if locked || screensaver { next.insert(.locked) }
        if asleep { next.insert(.asleep) }
        if ProcessInfo.processInfo.isLowPowerModeEnabled { next.insert(.lowPower) }
        if pauseOnBattery && Self.isOnBattery { next.insert(.battery) }
        // The window server tracks occlusion per pixel, so this only fires when no part of the wallpaper
        // shows anywhere — not even through the translucent menu bar or Dock (e.g. a full-screen app).
        if pauseWhenCovered && next.isEmpty && !isWallpaperVisible() { next.insert(.covered) }
        guard next != reasons else { return }
        reasons = next
        onChange(next)
    }

    static var isOnBattery: Bool {
        guard let info = IOPSCopyPowerSourcesInfo()?.takeRetainedValue(),
              let type = IOPSGetProvidingPowerSourceType(info)?.takeUnretainedValue() else { return false }
        return (type as String) == kIOPMBatteryPowerKey
    }
}
