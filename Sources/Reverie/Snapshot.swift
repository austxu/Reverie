import SwiftUI

/// Development aid: `REVERIE_SNAPSHOT=<dir> Reverie` renders the menu and Settings views in light
/// and dark appearance to PNGs, then quits. Lets UI changes be reviewed without screen recording.
enum Snapshot {
    @MainActor
    static func runIfRequested() {
        guard let dir = ProcessInfo.processInfo.environment["REVERIE_SNAPSHOT"] else { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
            MainActor.assumeIsolated {
                for (name, appearance) in [("light", NSAppearance.Name.aqua), ("dark", .darkAqua)] {
                    render(MenuView(), background: true, appearance: appearance, to: "\(dir)/menu-\(name).png")
                    render(SettingsView(), background: false, appearance: appearance, to: "\(dir)/settings-\(name).png")
                }
                exit(0)
            }
        }
    }

    @MainActor
    private static func render(_ view: some View, background: Bool, appearance: NSAppearance.Name, to path: String) {
        let root = view.environment(AppModel.shared)
            .background(background ? AnyShapeStyle(.regularMaterial) : AnyShapeStyle(.background))
        let host = NSHostingView(rootView: root)
        host.appearance = NSAppearance(named: appearance)
        let window = NSWindow(contentRect: .zero, styleMask: .borderless, backing: .buffered, defer: false)
        window.contentView = host
        window.setContentSize(host.fittingSize)
        window.appearance = NSAppearance(named: appearance)
        host.layoutSubtreeIfNeeded()
        guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { return }
        host.cacheDisplay(in: host.bounds, to: rep)
        try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: path))
    }
}
