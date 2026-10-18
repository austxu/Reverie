import SwiftUI

/// SwiftUI's `@State` property wrapper under another name. In the macOS 27 SDK, `@State` resolves to a
/// macro whose plugin ships only with full Xcode; the alias keeps the build working with Command Line Tools.
typealias ViewState<Value> = SwiftUI.State<Value>

struct MenuView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.openSettings) private var openSettings
    @ViewState private var isDropTargeted = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header
            PreviewCard(isDropTargeted: isDropTargeted)
            if !model.library.isEmpty { RecentsStrip() }
            LinkField()
            if let notice = model.notice {
                Label(notice, systemImage: "exclamationmark.circle")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .transition(.opacity.combined(with: .move(edge: .top)))
            }
            Divider().padding(.horizontal, -2)
            VStack(spacing: 0) {
                MenuRow(title: "Settings…", shortcut: "⌘,") { showSettings() }
                    .keyboardShortcut(",", modifiers: .command)
                MenuRow(title: "Quit Reverie", shortcut: "⌘Q") { NSApp.terminate(nil) }
                    .keyboardShortcut("q", modifiers: .command)
            }
            .padding(.horizontal, -6)
        }
        .padding(14)
        .frame(width: 312)
        .animation(.smooth(duration: 0.25), value: model.notice)
        .animation(.smooth(duration: 0.25), value: model.library.isEmpty)
        .dropDestination(for: URL.self) { urls, _ in
            guard let url = urls.first else { return false }
            model.add(fileAt: url)
            return true
        } isTargeted: { isDropTargeted = $0 }
    }

    private var header: some View {
        HStack(alignment: .center) {
            VStack(alignment: .leading, spacing: 1) {
                Text("Live Wallpaper")
                    .font(.headline)
                Text(model.statusText)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .contentTransition(.opacity)
                    .animation(.default, value: model.statusText)
            }
            Spacer()
            Toggle("Live Wallpaper", isOn: Binding(get: { model.isEnabled }, set: { model.setEnabled($0) }))
                .toggleStyle(.switch)
                .labelsHidden()
                .controlSize(.small)
        }
    }

    private func showSettings() {
        // Close the menu first so the Settings window isn't hidden behind it.
        NSApp.windows.first { $0.className.contains("MenuBarExtraWindow") }?.close()
        NSApp.activate()
        openSettings()
    }
}

// MARK: - Preview

private struct PreviewCard: View {
    @Environment(AppModel.self) private var model
    let isDropTargeted: Bool

    var body: some View {
        // A fixed-ratio base shape sizes the card; content is layered on top so a filled
        // image can never widen the popover.
        Rectangle()
            .fill(.quaternary.opacity(0.5))
            .aspectRatio(16 / 10, contentMode: .fit)
            .overlay {
                if let wallpaper = model.current, let image = model.thumbnail(for: wallpaper) {
                    Image(nsImage: image)
                        .resizable()
                        .aspectRatio(contentMode: .fill)
                        .saturation(model.isEnabled ? 1 : 0)
                        .opacity(model.isEnabled ? 1 : 0.55)
                        .transition(.opacity)
                        .id(wallpaper.id)
                }
            }
            .overlay {
                if let wallpaper = model.current, model.thumbnail(for: wallpaper) != nil {
                    overlay(for: wallpaper)
                } else {
                    emptyState
                }
            }
        .clipShape(.rect(cornerRadius: 12, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .strokeBorder(isDropTargeted ? Color.accentColor : Color.primary.opacity(0.08),
                              lineWidth: isDropTargeted ? 2 : 1)
        }
        .animation(.smooth(duration: 0.35), value: model.currentID)
        .animation(.smooth(duration: 0.35), value: model.isEnabled)
        .animation(.smooth(duration: 0.2), value: isDropTargeted)
    }

    private func overlay(for wallpaper: Wallpaper) -> some View {
        VStack {
            Spacer()
            HStack(alignment: .bottom) {
                Text(wallpaper.title)
                    .font(.callout.weight(.medium))
                    .foregroundStyle(.white)
                    .lineLimit(1)
                    .shadow(color: .black.opacity(0.35), radius: 2, y: 1)
                Spacer(minLength: 8)
                if model.isEnabled {
                    Button { model.togglePause() } label: {
                        Image(systemName: model.isPausedByUser ? "play.fill" : "pause.fill")
                            .font(.system(size: 13, weight: .semibold))
                            .foregroundStyle(.white)
                            .contentTransition(.symbolEffect(.replace))
                            .frame(width: 32, height: 32)
                    }
                    .buttonStyle(.plain)
                    .glassEffect(.regular.interactive(), in: .circle)
                    .help(model.isPausedByUser ? "Play" : "Pause")
                    .transition(.scale.combined(with: .opacity))
                }
            }
            .padding(10)
            .background(alignment: .bottom) {
                LinearGradient(colors: [.clear, .black.opacity(0.45)], startPoint: .top, endPoint: .bottom)
                    .frame(height: 64)
            }
        }
    }

    private var emptyState: some View {
        Button { model.chooseFile() } label: {
            VStack(spacing: 8) {
                Image(systemName: "play.rectangle.on.rectangle")
                    .font(.system(size: 28, weight: .light))
                    .foregroundStyle(.secondary)
                VStack(spacing: 2) {
                    Text("Choose a Video")
                        .font(.callout.weight(.medium))
                    Text("or drop an MP4 or MOV file here")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
    }
}

// MARK: - Recents

private struct RecentsStrip: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        ScrollView(.horizontal) {
            HStack(spacing: 8) {
                ForEach(model.library) { wallpaper in
                    RecentTile(wallpaper: wallpaper)
                }
                AddTile()
            }
            .padding(3)
        }
        .scrollIndicators(.never)
        .padding(-3)
    }
}

private struct RecentTile: View {
    @Environment(AppModel.self) private var model
    let wallpaper: Wallpaper
    @ViewState private var isHovering = false

    private var isSelected: Bool { wallpaper.id == model.currentID && model.isEnabled }

    var body: some View {
        Button { model.select(wallpaper) } label: {
            Group {
                if let image = model.thumbnail(for: wallpaper) {
                    Image(nsImage: image).resizable().aspectRatio(contentMode: .fill)
                } else {
                    Rectangle().fill(.quaternary)
                }
            }
            .frame(width: 64, height: 40)
            .clipShape(.rect(cornerRadius: 6, style: .continuous))
            .overlay {
                RoundedRectangle(cornerRadius: 6, style: .continuous)
                    .strokeBorder(Color.primary.opacity(0.1), lineWidth: 0.5)
            }
            .padding(2.5)
            .overlay {
                RoundedRectangle(cornerRadius: 8.5, style: .continuous)
                    .strokeBorder(Color.accentColor, lineWidth: 2)
                    .opacity(isSelected ? 1 : 0)
            }
            .scaleEffect(isHovering && !isSelected ? 1.04 : 1)
        }
        .buttonStyle(.plain)
        .onHover { isHovering = $0 }
        .animation(.smooth(duration: 0.2), value: isHovering)
        .animation(.smooth(duration: 0.2), value: isSelected)
        .help(wallpaper.title)
        .contextMenu {
            Button("Show in Finder") { model.revealInFinder(wallpaper) }
            Divider()
            Button(wallpaper.isDownloaded ? "Delete Download" : "Remove from Reverie", role: .destructive) {
                model.remove(wallpaper)
            }
        }
    }
}

private struct AddTile: View {
    @Environment(AppModel.self) private var model
    @ViewState private var isHovering = false

    var body: some View {
        Button { model.chooseFile() } label: {
            Image(systemName: "plus")
                .font(.system(size: 14, weight: .medium))
                .foregroundStyle(isHovering ? .primary : .secondary)
                .frame(width: 64, height: 40)
                .background(.quaternary.opacity(isHovering ? 0.9 : 0.5), in: .rect(cornerRadius: 6, style: .continuous))
                .padding(2.5)
        }
        .buttonStyle(.plain)
        .onHover { isHovering = $0 }
        .animation(.smooth(duration: 0.15), value: isHovering)
        .help("Add a Video…")
    }
}

// MARK: - YouTube link

private struct LinkField: View {
    @Environment(AppModel.self) private var model
    @ViewState private var text = ""
    @FocusState private var focused: Bool

    var body: some View {
        Group {
            switch model.downloadState {
            case .idle:
                field
            case .needsTool:
                consent
            case .installingTool(let progress):
                progressRow(title: "Installing downloader…", progress: progress)
            case .downloading(let progress):
                progressRow(title: progress > 0 ? "Downloading… \(Int(progress * 100))%" : "Preparing download…",
                            progress: progress > 0 ? progress : nil)
            case .failed(let message):
                failure(message)
            }
        }
        .animation(.smooth(duration: 0.25), value: model.downloadState)
        .onChange(of: model.downloadState) { _, state in
            if case .downloading = state { text = "" }
        }
    }

    private var field: some View {
        HStack(spacing: 8) {
            Image(systemName: "link")
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(.secondary)
            TextField("Paste a YouTube link", text: $text)
                .textFieldStyle(.plain)
                .focused($focused)
                .onSubmit(submit)
            if !text.isEmpty {
                Button(action: submit) {
                    Image(systemName: "arrow.down.circle.fill")
                        .font(.system(size: 16))
                        .foregroundStyle(Color.accentColor)
                }
                .buttonStyle(.plain)
                .help("Download and set as wallpaper")
                .transition(.scale.combined(with: .opacity))
            }
        }
        .padding(.horizontal, 10)
        .frame(height: 30)
        .background(.quaternary.opacity(0.5), in: .rect(cornerRadius: 8, style: .continuous))
        .animation(.smooth(duration: 0.15), value: text.isEmpty)
    }

    private var consent: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Downloading from YouTube uses yt-dlp, a free, open-source tool. Install it now? (about 35 MB)")
                .font(.callout)
                .fixedSize(horizontal: false, vertical: true)
            HStack {
                Spacer()
                Button("Not Now") { model.cancelDownload() }
                Button("Install") { model.installToolAndDownload() }
                    .keyboardShortcut(.defaultAction)
            }
            .controlSize(.small)
        }
        .padding(10)
        .background(.quaternary.opacity(0.5), in: .rect(cornerRadius: 8, style: .continuous))
    }

    private func progressRow(title: String, progress: Double?) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(title)
                    .font(.callout)
                    .monospacedDigit()
                    .contentTransition(.numericText())
                Spacer()
                Button { model.cancelDownload() } label: {
                    Image(systemName: "xmark.circle.fill")
                        .foregroundStyle(.secondary)
                }
                .buttonStyle(.plain)
                .help("Cancel")
            }
            if let progress {
                ProgressView(value: progress)
                    .controlSize(.small)
            } else {
                ProgressView()
                    .progressViewStyle(.linear)
                    .controlSize(.small)
            }
        }
        .padding(10)
        .background(.quaternary.opacity(0.5), in: .rect(cornerRadius: 8, style: .continuous))
    }

    private func failure(_ message: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(.orange)
            Text(message)
                .font(.callout)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 4)
            Button("OK") { model.dismissDownloadMessage() }
                .controlSize(.small)
        }
        .padding(10)
        .background(.quaternary.opacity(0.5), in: .rect(cornerRadius: 8, style: .continuous))
    }

    private func submit() {
        guard !text.trimmingCharacters(in: .whitespaces).isEmpty else { return }
        model.submitLink(text)
    }
}

// MARK: - Menu row

/// A full-width row that highlights on hover, like the items in system menu bar extras.
private struct MenuRow: View {
    let title: String
    var shortcut: String? = nil
    let action: () -> Void
    @ViewState private var isHovering = false

    var body: some View {
        Button(action: action) {
            HStack {
                Text(title)
                Spacer()
                if let shortcut {
                    Text(shortcut)
                        .foregroundStyle(.tertiary)
                }
            }
            .padding(.horizontal, 8)
            .frame(height: 24)
            .contentShape(.rect)
            .background(isHovering ? Color.primary.opacity(0.1) : .clear, in: .rect(cornerRadius: 6, style: .continuous))
        }
        .buttonStyle(.plain)
        .onHover { isHovering = $0 }
    }
}
