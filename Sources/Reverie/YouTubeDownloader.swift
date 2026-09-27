import Foundation

/// Downloads online videos with yt-dlp. The tool isn't bundled; it's fetched from the official
/// GitHub release the first time the user asks for it.
@MainActor
final class YouTubeDownloader {
    enum Failure: LocalizedError {
        case invalidLink, toolInstall(String), download(String)

        var errorDescription: String? {
            switch self {
            case .invalidLink: "That doesn't look like a video link."
            case .toolInstall(let detail): "Couldn't install the downloader. \(detail)"
            case .download(let detail): detail
            }
        }
    }

    struct Result {
        let file: URL
        let title: String
    }

    static let toolURL = Storage.bin.appendingPathComponent("yt-dlp")
    static let releaseURL = URL(string: "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_macos")!

    static var isToolInstalled: Bool { FileManager.default.isExecutableFile(atPath: toolURL.path) }

    private var process: Process?

    static func validate(_ text: String) -> URL? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = URL(string: trimmed.contains("://") ? trimmed : "https://\(trimmed)"),
              let host = url.host(), host.contains("."), url.scheme?.hasPrefix("http") == true else { return nil }
        return url
    }

    func installTool(progress: @escaping (Double) -> Void) async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            let delegate = InstallDelegate(destination: Self.toolURL, progress: progress) { error in
                if let error { continuation.resume(throwing: Failure.toolInstall(error.localizedDescription)) }
                else { continuation.resume() }
            }
            let session = URLSession(configuration: .ephemeral, delegate: delegate, delegateQueue: .main)
            session.downloadTask(with: Self.releaseURL).resume()
            session.finishTasksAndInvalidate()
        }
    }

    /// Downloads the best video-only H.264 stream (wallpapers are silent, and H.264 is hardware
    /// decoded everywhere), falling back to other MP4 formats AVFoundation can play.
    func download(_ link: URL, id: UUID, progress: @escaping (Double) -> Void) async throws -> Result {
        let process = Process()
        process.executableURL = Self.toolURL
        process.arguments = [
            // `--print` implies `--quiet`, so progress has to be requested explicitly.
            "--no-playlist", "--no-warnings", "--newline", "--no-simulate", "--no-part", "--progress",
            "-f", "bv*[vcodec^=avc1][height<=2160]/bv*[vcodec^=hvc1]/b[ext=mp4]/bv*[ext=mp4]",
            "-o", Storage.videos.appendingPathComponent("\(id.uuidString).%(ext)s").path,
            "--progress-template", "download:REVERIE-PROGRESS %(progress.downloaded_bytes)s %(progress.total_bytes,progress.total_bytes_estimate)s",
            "--print", "before_dl:REVERIE-TITLE %(title)s",
            "--print", "after_move:REVERIE-PATH %(filepath)s",
            link.absoluteString,
        ]
        let output = Pipe()
        let errors = Pipe()
        process.standardOutput = output
        process.standardError = errors
        self.process = process

        var title = "Video"
        var path: String?
        var errorText = ""

        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                var buffer = ""
                output.fileHandleForReading.readabilityHandler = { handle in
                    let chunk = String(decoding: handle.availableData, as: UTF8.self)
                    DispatchQueue.main.async {
                        buffer += chunk
                        while let newline = buffer.firstIndex(of: "\n") {
                            let line = String(buffer[..<newline])
                            buffer.removeSubrange(...newline)
                            if line.hasPrefix("REVERIE-PROGRESS ") {
                                let parts = line.split(separator: " ")
                                if parts.count == 3, let done = Double(parts[1]), let total = Double(parts[2]), total > 0 {
                                    progress(min(done / total, 1))
                                }
                            } else if line.hasPrefix("REVERIE-TITLE ") {
                                title = String(line.dropFirst("REVERIE-TITLE ".count))
                            } else if line.hasPrefix("REVERIE-PATH ") {
                                path = String(line.dropFirst("REVERIE-PATH ".count))
                            }
                        }
                    }
                }
                errors.fileHandleForReading.readabilityHandler = { handle in
                    let chunk = String(decoding: handle.availableData, as: UTF8.self)
                    DispatchQueue.main.async { errorText += chunk }
                }
                process.terminationHandler = { process in
                    // Let the last readability callbacks land before reading the results.
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) {
                        output.fileHandleForReading.readabilityHandler = nil
                        errors.fileHandleForReading.readabilityHandler = nil
                        if process.terminationStatus == 0, let path, FileManager.default.fileExists(atPath: path) {
                            continuation.resume(returning: Result(file: URL(fileURLWithPath: path), title: title))
                        } else if process.terminationReason == .uncaughtSignal {
                            continuation.resume(throwing: CancellationError())
                        } else {
                            continuation.resume(throwing: Failure.download(Self.friendlyError(errorText)))
                        }
                    }
                }
                do {
                    try process.run()
                } catch {
                    continuation.resume(throwing: Failure.download(error.localizedDescription))
                }
            }
        } onCancel: {
            process.terminate()
        }
    }

    func cancel() {
        process?.terminate()
    }

    private static func friendlyError(_ text: String) -> String {
        let line = text.split(separator: "\n").last { $0.contains("ERROR") }.map(String.init) ?? ""
        if line.contains("Unsupported URL") { return "That site isn't supported." }
        if line.contains("Private video") || line.contains("Sign in") { return "That video isn't publicly available." }
        if line.contains("Requested format is not available") { return "No playable version of that video was found." }
        if line.isEmpty { return "The download didn't finish." }
        return line.replacingOccurrences(of: "ERROR: ", with: "")
    }
}

/// Drives the yt-dlp download: reports progress, moves the finished file into place and marks it executable.
private final class InstallDelegate: NSObject, URLSessionDownloadDelegate {
    let destination: URL
    let progress: (Double) -> Void
    var completion: ((Error?) -> Void)?
    private var moveError: Error?

    init(destination: URL, progress: @escaping (Double) -> Void, completion: @escaping (Error?) -> Void) {
        self.destination = destination
        self.progress = progress
        self.completion = completion
    }

    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didWriteData bytesWritten: Int64,
                    totalBytesWritten: Int64, totalBytesExpectedToWrite: Int64) {
        guard totalBytesExpectedToWrite > 0 else { return }
        progress(Double(totalBytesWritten) / Double(totalBytesExpectedToWrite))
    }

    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didFinishDownloadingTo location: URL) {
        do {
            guard (downloadTask.response as? HTTPURLResponse)?.statusCode == 200 else {
                throw URLError(.badServerResponse)
            }
            try? FileManager.default.removeItem(at: destination)
            try FileManager.default.moveItem(at: location, to: destination)
            try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: destination.path)
        } catch {
            moveError = error
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        completion?(error ?? moveError)
        completion = nil
    }
}
