import SwiftUI

@main
struct ReverieApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        MenuBarExtra {
            MenuView()
                .environment(AppModel.shared)
        } label: {
            Image(systemName: "sparkles.tv")
                .accessibilityLabel("Reverie")
        }
        .menuBarExtraStyle(.window)

        Settings {
            SettingsView()
                .environment(AppModel.shared)
        }
        .windowResizability(.contentSize)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    // Start before any files handed to us at launch are delivered to `application(_:open:)`.
    func applicationWillFinishLaunching(_ notification: Notification) {
        MainActor.assumeIsolated {
            AppModel.shared.start()
            Snapshot.runIfRequested()
        }
    }

    /// "Open With → Reverie" in Finder, or `open -a Reverie video.mov`.
    func application(_ application: NSApplication, open urls: [URL]) {
        guard let url = urls.first(where: \.isFileURL) else { return }
        MainActor.assumeIsolated { AppModel.shared.add(fileAt: url) }
    }
}
