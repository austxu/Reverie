import AppKit
import AVFoundation
import CoreImage

/// A video the user has used as a wallpaper.
struct Wallpaper: Codable, Identifiable, Hashable {
    enum Source: Codable, Hashable {
        case file
        case youtube(URL)
    }

    let id: UUID
    var title: String
    var source: Source
    /// Bookmark for user files so the entry survives the file being moved or renamed.
    var bookmark: Data?
    var path: String
    var addedAt: Date

    var isDownloaded: Bool { if case .youtube = source { true } else { false } }

    /// Resolves the current location of the video, following moves via the bookmark.
    func resolvedURL() -> URL? {
        if let bookmark {
            var stale = false
            if let url = try? URL(resolvingBookmarkData: bookmark, options: [.withoutUI], bookmarkDataIsStale: &stale),
               FileManager.default.fileExists(atPath: url.path) {
                return url
            }
        }
        let url = URL(fileURLWithPath: path)
        return FileManager.default.fileExists(atPath: url.path) ? url : nil
    }
}

/// On-disk locations, all under ~/Library/Application Support/Reverie.
enum Storage {
    static let root: URL = {
        let url = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Reverie", isDirectory: true)
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }()

    static func directory(_ name: String) -> URL {
        let url = root.appendingPathComponent(name, isDirectory: true)
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    static var libraryFile: URL { root.appendingPathComponent("library.json") }
    static var videos: URL { directory("Videos") }
    static var thumbnails: URL { directory("Thumbnails") }
    static var stills: URL { directory("Stills") }
    static var bin: URL { directory("bin") }

    static func thumbnailURL(for id: UUID) -> URL { thumbnails.appendingPathComponent("\(id.uuidString).jpg") }
    static func stillURL(for id: UUID) -> URL { stills.appendingPathComponent("\(id.uuidString).png") }
}

/// Frame extraction for thumbnails and the matching desktop picture.
enum Frames {
    /// A representative frame: a little way in, since many clips open on black.
    static func image(from url: URL, maxSize: CGSize) async -> CGImage? {
        let asset = AVURLAsset(url: url)
        let duration = (try? await asset.load(.duration).seconds) ?? 0
        let generator = AVAssetImageGenerator(asset: asset)
        generator.appliesPreferredTrackTransform = true
        generator.maximumSize = maxSize
        generator.requestedTimeToleranceBefore = CMTime(seconds: 0.5, preferredTimescale: 600)
        generator.requestedTimeToleranceAfter = CMTime(seconds: 0.5, preferredTimescale: 600)
        let time = CMTime(seconds: duration.isFinite ? min(duration * 0.1, 3) : 0, preferredTimescale: 600)
        return try? await generator.image(at: time).image
    }

    @discardableResult
    static func write(_ image: CGImage, to url: URL, type: NSBitmapImageRep.FileType) -> Bool {
        let rep = NSBitmapImageRep(cgImage: image)
        let props: [NSBitmapImageRep.PropertyKey: Any] = type == .jpeg ? [.compressionFactor: 0.85] : [:]
        guard let data = rep.representation(using: type, properties: props) else { return false }
        return (try? data.write(to: url, options: .atomic)) != nil
    }

    /// Creates the thumbnail and full-resolution still for a wallpaper if they don't exist yet.
    static func prepare(_ wallpaper: Wallpaper, video url: URL) async {
        let thumb = Storage.thumbnailURL(for: wallpaper.id)
        if !FileManager.default.fileExists(atPath: thumb.path),
           let image = await image(from: url, maxSize: CGSize(width: 640, height: 400)) {
            write(image, to: thumb, type: .jpeg)
        }
        let still = Storage.stillURL(for: wallpaper.id)
        if !FileManager.default.fileExists(atPath: still.path),
           let image = await image(from: url, maxSize: .zero) {
            write(image, to: still, type: .png)
        }
    }

    /// A small, soft, slightly dimmed copy of `image` used to fill the bands around a fitted video.
    /// Blurring a 320 px copy is cheap, and the layer's upscaling adds to the softness.
    static func blurred(_ image: CGImage) -> CGImage? {
        let source = CIImage(cgImage: image)
        let scale = 320 / max(source.extent.width, 1)
        let small = source.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
        let output = small.clampedToExtent()
            .applyingGaussianBlur(sigma: 16)
            .applyingFilter("CIColorControls", parameters: [kCIInputSaturationKey: 1.1])
            .applyingFilter("CIExposureAdjust", parameters: [kCIInputEVKey: -0.3])
            .cropped(to: small.extent)
        return CIContext(options: [.cacheIntermediates: false]).createCGImage(output, from: small.extent)
    }

    /// The desktop picture for "Fit to Screen": the whole frame centered on its blurred copy, rendered
    /// at the screen's exact pixel size so the lock screen matches the live wallpaper.
    static func fittedStill(for id: UUID, pixelSize: CGSize) -> URL? {
        let url = Storage.stills.appendingPathComponent("\(id.uuidString)-fit-\(Int(pixelSize.width))x\(Int(pixelSize.height)).png")
        if FileManager.default.fileExists(atPath: url.path) { return url }
        guard let still = NSImage(contentsOf: Storage.stillURL(for: id))?.cgImage(forProposedRect: nil, context: nil, hints: nil),
              let backdrop = blurred(still),
              let context = CGContext(data: nil, width: Int(pixelSize.width), height: Int(pixelSize.height),
                                      bitsPerComponent: 8, bytesPerRow: 0, space: CGColorSpace(name: CGColorSpace.sRGB)!,
                                      bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else { return nil }
        let canvas = CGRect(origin: .zero, size: pixelSize)
        let imageSize = CGSize(width: still.width, height: still.height)
        context.interpolationQuality = .high
        context.draw(backdrop, in: AVMakeRect(aspectRatio: imageSize, insideRect: canvas).aspectFilling(canvas))
        context.draw(still, in: AVMakeRect(aspectRatio: imageSize, insideRect: canvas))
        guard let image = context.makeImage(), write(image, to: url, type: .png) else { return nil }
        return url
    }

    static func removeCache(for id: UUID) {
        try? FileManager.default.removeItem(at: Storage.thumbnailURL(for: id))
        let stills = (try? FileManager.default.contentsOfDirectory(at: Storage.stills, includingPropertiesForKeys: nil)) ?? []
        stills.filter { $0.lastPathComponent.hasPrefix(id.uuidString) }.forEach { try? FileManager.default.removeItem(at: $0) }
    }
}

private extension CGRect {
    /// The smallest rect with this rect's aspect ratio that covers `bounds`, centered on it.
    func aspectFilling(_ bounds: CGRect) -> CGRect {
        let scale = max(bounds.width / width, bounds.height / height)
        let size = CGSize(width: width * scale, height: height * scale)
        return CGRect(x: bounds.midX - size.width / 2, y: bounds.midY - size.height / 2, width: size.width, height: size.height)
    }
}
