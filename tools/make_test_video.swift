#!/usr/bin/swift
import AVFoundation
import CoreGraphics
import CoreText
import Foundation

// Parse command-line arguments
let args = CommandLine.arguments
guard args.count == 2 else {
    print("Usage: swift make_test_video.swift <output.mov>")
    exit(1)
}

let outputPath = args[1]

// Video parameters
let width: Int32 = 1920
let height: Int32 = 1080
let fps: Int32 = 30
let frameCount = 240  // 8 seconds at 30 fps
let bitrate = 8_000_000  // 8 Mbps

// Function to draw text into a CGContext using CoreText
func drawTextInContext(_ context: CGContext, _ text: String, x: CGFloat, y: CGFloat) {
    let font = CTFontCreateWithName("Helvetica" as CFString, 24, nil)

    let attributes: NSDictionary = [
        kCTFontAttributeName: font,
        kCTForegroundColorAttributeName: CGColor(red: 1, green: 1, blue: 1, alpha: 1) as Any
    ]

    let attrString = NSAttributedString(string: text, attributes: attributes as? [NSAttributedString.Key: Any])
    let line = CTLineCreateWithAttributedString(attrString as CFAttributedString)

    context.textPosition = CGPoint(x: x, y: y)
    CTLineDraw(line, context)
}

func encodeVideo() throws {
    let outURL = URL(fileURLWithPath: outputPath)
    try? FileManager.default.removeItem(at: outURL)

    let writer = try AVAssetWriter(url: outURL, fileType: .mov)

    // Configure video settings
    let vidSettings: [String: Any] = [
        AVVideoCodecKey: AVVideoCodecType.h264,
        AVVideoWidthKey: width,
        AVVideoHeightKey: height,
        AVVideoCompressionPropertiesKey: [AVVideoAverageBitRateKey: bitrate]
    ]

    let vidInput = AVAssetWriterInput(mediaType: .video, outputSettings: vidSettings)
    writer.add(vidInput)

    // Pixel buffer pool attributes
    let pbAttrs: [String: Any] = [
        kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
        kCVPixelBufferWidthKey as String: width,
        kCVPixelBufferHeightKey as String: height,
    ]

    let adaptor = AVAssetWriterInputPixelBufferAdaptor(
        assetWriterInput: vidInput,
        sourcePixelBufferAttributes: pbAttrs
    )

    try writer.startWriting()
    writer.startSession(atSourceTime: .zero)

    // Write frames
    for frameNum in 0..<frameCount {
        // Create pixel buffer
        var pb: CVPixelBuffer?
        guard CVPixelBufferPoolCreatePixelBuffer(nil, adaptor.pixelBufferPool!, &pb) == kCVReturnSuccess,
              let buf = pb else {
            throw NSError(domain: "CVPixelBuffer", code: -1, userInfo: [NSLocalizedDescriptionKey: "Failed to create pixel buffer"])
        }

        CVPixelBufferLockBaseAddress(buf, [])
        defer { CVPixelBufferUnlockBaseAddress(buf, []) }

        guard let baddr = CVPixelBufferGetBaseAddress(buf) else {
            throw NSError(domain: "CVPixelBuffer", code: -2, userInfo: [NSLocalizedDescriptionKey: "Failed to get pixel buffer base address"])
        }

        let bpr = CVPixelBufferGetBytesPerRow(buf)
        let cs = CGColorSpaceCreateDeviceRGB()
        let bi = CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue)

        guard let ctx = CGContext(
            data: baddr,
            width: Int(width),
            height: Int(height),
            bitsPerComponent: 8,
            bytesPerRow: bpr,
            space: cs,
            bitmapInfo: bi.rawValue
        ) else {
            throw NSError(domain: "CGContext", code: -1, userInfo: [NSLocalizedDescriptionKey: "Failed to create graphics context"])
        }

        // Draw dark navy background
        ctx.setFillColor(red: 0.08, green: 0.08, blue: 0.16, alpha: 1.0)
        ctx.fill(CGRect(x: 0, y: 0, width: CGFloat(width), height: CGFloat(height)))

        // Animation parameter (0.0 to 1.0 over 8 seconds)
        let t = Double(frameNum) / Double(frameCount)

        // Define four drifting blobs with circular orbital paths
        let blobs = [
            (x: CGFloat(width) * 0.3, y: CGFloat(height) * 0.3, r: 150.0, col: (0.7, 0.3, 0.9), f: 1),
            (x: CGFloat(width) * 0.7, y: CGFloat(height) * 0.3, r: 180.0, col: (0.3, 0.5, 0.9), f: 2),
            (x: CGFloat(width) * 0.5, y: CGFloat(height) * 0.7, r: 160.0, col: (0.9, 0.3, 0.7), f: 3),
            (x: CGFloat(width) * 0.2, y: CGFloat(height) * 0.5, r: 140.0, col: (0.3, 0.9, 0.9), f: 2),
        ]

        for blob in blobs {
            // Circular orbital motion: position = center + radius * (cos(2πft), sin(2πft))
            // Using different frequencies for each blob ensures non-repeating pattern
            let ang = 2.0 * CGFloat.pi * CGFloat(t) * CGFloat(blob.f)
            let ox = blob.x + 200 * cos(ang)
            let oy = blob.y + 200 * sin(ang)

            // Draw soft blob using concentric circles with decreasing opacity
            for layer in 0..<5 {
                let alpha = CGFloat(1.0 - Double(layer) * 0.14)
                let rad = blob.r * (1.0 - CGFloat(layer) * 0.16)
                ctx.setFillColor(red: blob.col.0, green: blob.col.1, blue: blob.col.2, alpha: alpha)
                ctx.fillEllipse(in: CGRect(x: ox - rad, y: oy - rad, width: rad * 2, height: rad * 2))
            }
        }

        // Draw frame counter text in bottom-left
        drawTextInContext(ctx, "Reverie test · \(frameNum)", x: 15, y: CGFloat(height) - 50)

        // Append frame to video
        let tm = CMTime(value: CMTimeValue(frameNum), timescale: fps)
        guard adaptor.append(buf, withPresentationTime: tm) else {
            throw NSError(domain: "AVAssetWriter", code: -1, userInfo: [NSLocalizedDescriptionKey: "Failed to append frame \(frameNum)"])
        }

        // Wait for writer to be ready for more data
        while !vidInput.isReadyForMoreMediaData {
            usleep(1000)
        }

        // Print progress every 30 frames
        if frameNum % 30 == 0 {
            print("Frame \(frameNum)/\(frameCount)")
        }
    }

    vidInput.markAsFinished()

    // Wait for writing to complete
    let sem = DispatchSemaphore(value: 0)
    writer.finishWriting { sem.signal() }
    sem.wait()

    guard writer.status == .completed else {
        throw writer.error ?? NSError(domain: "AVAssetWriter", code: -1, userInfo: [NSLocalizedDescriptionKey: "Failed to complete video writing"])
    }

    print("Video written to: \(outputPath)")
}

do {
    try encodeVideo()
} catch {
    print("Error: \(error.localizedDescription)")
    exit(1)
}
