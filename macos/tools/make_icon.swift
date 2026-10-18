// make_icon.swift
// Generates the Reverie.app macOS icon: a dusk-landscape squircle icon,
// rendered at every size needed for a .iconset, then packed into Reverie.icns
// via /usr/bin/iconutil.
//
// Usage: swift tools/make_icon.swift <output-dir>
//
// No external dependencies — pure CoreGraphics/AppKit, no downloads.

import AppKit
import CoreGraphics
import Foundation

// MARK: - Args

let args = CommandLine.arguments
guard args.count >= 2 else {
    FileHandle.standardError.write("Usage: swift make_icon.swift <output-dir>\n".data(using: .utf8)!)
    exit(1)
}
let outputDir = (args[1] as NSString).expandingTildeInPath
let fm = FileManager.default
let iconsetDir = (outputDir as NSString).appendingPathComponent("Reverie.iconset")
try? fm.createDirectory(atPath: outputDir, withIntermediateDirectories: true)
try? fm.createDirectory(atPath: iconsetDir, withIntermediateDirectories: true)

// MARK: - Color helpers

func hex(_ hexString: String, alpha: CGFloat = 1.0) -> CGColor {
    var s = hexString
    if s.hasPrefix("#") { s.removeFirst() }
    var rgb: UInt64 = 0
    Scanner(string: s).scanHexInt64(&rgb)
    let r = CGFloat((rgb & 0xFF0000) >> 16) / 255.0
    let g = CGFloat((rgb & 0x00FF00) >> 8) / 255.0
    let b = CGFloat(rgb & 0x0000FF) / 255.0
    return CGColor(red: r, green: g, blue: b, alpha: alpha)
}

// MARK: - Squircle (continuous-corner rounded rect) path.
//
// A true circular-arc rounded rect (CGPath(roundedRect:)) already renders as
// smooth, uniformly-curved corners at every size — the earlier hand-rolled
// bezier version used control points too close to the corner vertex, which
// produced a chamfered/flattened "clipped octagon" look. Using the built-in
// rounded-rect constructor guarantees correct, smooth arcs.

func squirclePath(rect: CGRect, cornerRadius r: CGFloat) -> CGPath {
    return CGPath(roundedRect: rect, cornerWidth: r, cornerHeight: r, transform: nil)
}

// MARK: - Wave / hill path across the bottom of a rect

func hillPath(in rect: CGRect, baseY: CGFloat, peakOffsets: [CGFloat]) -> CGPath {
    // baseY is expressed as a fraction of rect.height from rect.minY (0 = bottom edge)
    let path = CGMutablePath()
    let w = rect.width
    let bottom = rect.minY - 4
    let startY = rect.minY + baseY * rect.height

    path.move(to: CGPoint(x: rect.minX - 4, y: bottom))
    path.addLine(to: CGPoint(x: rect.minX - 4, y: startY))

    let n = peakOffsets.count
    let segW = (w + 8) / CGFloat(n)
    var x = rect.minX - 4
    var prevY = startY
    for i in 0..<n {
        let nextX = x + segW
        let midX = x + segW / 2
        let targetY = startY + peakOffsets[i] * rect.height
        path.addCurve(to: CGPoint(x: nextX, y: targetY),
                       control1: CGPoint(x: midX, y: prevY),
                       control2: CGPoint(x: midX, y: targetY))
        prevY = targetY
        x = nextX
    }
    path.addLine(to: CGPoint(x: rect.maxX + 4, y: bottom))
    path.closeSubpath()
    return path
}

// MARK: - Main drawing routine (draws into whatever CGContext is current, at `canvas` size)

func drawIcon(context ctx: CGContext, canvas: CGFloat) {
    let scale = canvas / 1024.0

    ctx.saveGState()
    ctx.translateBy(x: 0, y: 0)

    // Full transparent background — nothing drawn outside the squircle.
    ctx.clear(CGRect(x: 0, y: 0, width: canvas, height: canvas))

    let margin: CGFloat = 100 * scale
    let bodyRect = CGRect(x: margin, y: margin,
                           width: canvas - margin * 2, height: canvas - margin * 2)
    let cornerRadius: CGFloat = 185 * scale
    let squircle = squirclePath(rect: bodyRect, cornerRadius: cornerRadius)

    // --- Drop shadow: drawn as its own shadowed fill of the squircle shape,
    // inside a transparency layer so the shadow is composited once as a unit
    // and never becomes part of what gets clipped for the interior artwork
    // below (that clip only ever applies to the sky/hills content, not to
    // this shadow pass).
    ctx.saveGState()
    ctx.beginTransparencyLayer(auxiliaryInfo: nil)
    ctx.setShadow(offset: CGSize(width: 0, height: -10 * scale),
                   blur: 28 * scale,
                   color: CGColor(red: 0, green: 0, blue: 0, alpha: 0.30))
    ctx.addPath(squircle)
    ctx.setFillColor(CGColor(red: 0, green: 0, blue: 0, alpha: 1))
    ctx.fillPath()
    ctx.endTransparencyLayer()
    ctx.restoreGState()

    // --- Clip to squircle for all interior artwork
    ctx.saveGState()
    ctx.addPath(squircle)
    ctx.clip()

    // --- Sky gradient: deep indigo -> violet -> warm peach near horizon (~62% down)
    let skyColors = [
        hex("#1B1F4B"),
        hex("#4C3A78"),
        hex("#6A4C93"),
        hex("#C97C93"),
        hex("#F4A383"),
    ] as CFArray
    let skyLocations: [CGFloat] = [0.0, 0.28, 0.42, 0.50, 0.58]
    if let skyGradient = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(),
                                     colors: skyColors, locations: skyLocations) {
        ctx.drawLinearGradient(skyGradient,
                                start: CGPoint(x: bodyRect.midX, y: bodyRect.maxY),
                                end: CGPoint(x: bodyRect.midX, y: bodyRect.minY),
                                options: [])
    }

    // --- Sun glow (radial), centered slightly right of center, sitting low so the
    // back hill overlaps its lower portion ("partly behind the hills").
    let horizonY = bodyRect.minY + bodyRect.height * 0.60
    let sunCenter = CGPoint(x: bodyRect.minX + bodyRect.width * 0.60, y: horizonY - bodyRect.height * 0.045)
    let sunRadius = bodyRect.width * 0.34

    ctx.saveGState()
    let glowColors = [
        hex("#FFE6C2", alpha: 0.95),
        hex("#FFD6A5", alpha: 0.55),
        hex("#FFD6A5", alpha: 0.0),
    ] as CFArray
    if let glowGradient = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(),
                                      colors: glowColors, locations: [0.0, 0.4, 1.0]) {
        ctx.drawRadialGradient(glowGradient,
                                startCenter: sunCenter, startRadius: 0,
                                endCenter: sunCenter, endRadius: sunRadius,
                                options: [])
    }
    ctx.restoreGState()

    // --- Solid sun disc core (slightly smaller, crisper) partly behind hills (hills drawn after)
    ctx.saveGState()
    let discRadius = bodyRect.width * 0.155
    let discRect = CGRect(x: sunCenter.x - discRadius, y: sunCenter.y - discRadius,
                           width: discRadius * 2, height: discRadius * 2)
    let discColors = [hex("#FFF3DE", alpha: 0.98), hex("#FFD6A5", alpha: 0.9)] as CFArray
    if let discGradient = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(),
                                      colors: discColors, locations: [0.0, 1.0]) {
        ctx.drawRadialGradient(discGradient,
                                startCenter: sunCenter, startRadius: 0,
                                endCenter: sunCenter, endRadius: discRadius,
                                options: [])
    }
    _ = discRect
    ctx.restoreGState()

    // --- Three layered rolling hills, back to front, cooler & darker as they approach.
    // The back hill's wave line rides up into the sun disc so the sun reads as
    // "partly behind" it. All three are fully opaque so nothing beneath (sun,
    // glow, sky) shows through a hill that's supposed to be in front of it.
    // Back hill
    ctx.saveGState()
    let backHill = hillPath(in: bodyRect, baseY: 0.46,
                             peakOffsets: [0.035, -0.025, 0.05, -0.015])
    ctx.addPath(backHill)
    ctx.setFillColor(hex("#9D6B9E", alpha: 1.0))
    ctx.fillPath()
    ctx.restoreGState()

    // Middle hill
    ctx.saveGState()
    let midHill = hillPath(in: bodyRect, baseY: 0.28,
                            peakOffsets: [-0.02, 0.055, 0.0, 0.065])
    ctx.addPath(midHill)
    ctx.setFillColor(hex("#5B3F7A", alpha: 1.0))
    ctx.fillPath()
    ctx.restoreGState()

    // Front hill
    ctx.saveGState()
    let frontHill = hillPath(in: bodyRect, baseY: 0.10,
                              peakOffsets: [0.045, -0.02, 0.04, 0.0])
    ctx.addPath(frontHill)
    ctx.setFillColor(hex("#2A2350", alpha: 1.0))
    ctx.fillPath()
    ctx.restoreGState()

    // --- Subtle vignette at very bottom for depth (very light, stays subtle)
    ctx.saveGState()
    let vignetteColors = [hex("#000000", alpha: 0.0), hex("#000000", alpha: 0.10)] as CFArray
    if let vGradient = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(),
                                   colors: vignetteColors, locations: [0.0, 1.0]) {
        ctx.drawLinearGradient(vGradient,
                                start: CGPoint(x: bodyRect.midX, y: bodyRect.minY + bodyRect.height * 0.18),
                                end: CGPoint(x: bodyRect.midX, y: bodyRect.minY),
                                options: [])
    }
    ctx.restoreGState()

    ctx.restoreGState() // end clip to squircle

    // --- Glassy top-edge inner highlight: stroke the *entire* body outline
    // (so it naturally follows the curved corners, not just a straight line
    // across the top), then mask that stroke with a vertical gradient that is
    // bright at the top and fades to nothing about a quarter of the way down.
    // The net effect is a highlight that traces the top edge and rounded
    // corners and dies out along the sides.
    ctx.saveGState()
    ctx.addPath(squircle)
    ctx.clip()

    ctx.saveGState()
    ctx.addPath(squircle)
    ctx.setLineWidth(4 * scale)
    ctx.replacePathWithStrokedPath()
    ctx.clip()
    let highlightColors = [
        hex("#FFFFFF", alpha: 0.35),
        hex("#FFFFFF", alpha: 0.0),
    ] as CFArray
    if let hlGradient = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(),
                                    colors: highlightColors,
                                    locations: [0.0, 1.0]) {
        ctx.drawLinearGradient(hlGradient,
                                start: CGPoint(x: bodyRect.midX, y: bodyRect.maxY),
                                end: CGPoint(x: bodyRect.midX, y: bodyRect.maxY - bodyRect.height * 0.25),
                                options: [])
    }
    ctx.restoreGState()
    ctx.restoreGState()

    ctx.restoreGState()
}

// MARK: - Render a single PNG at the given pixel size

func renderPNG(size: Int, to path: String) {
    let w = size, h = size
    guard let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil,
        pixelsWide: w,
        pixelsHigh: h,
        bitsPerSample: 8,
        samplesPerPixel: 4,
        hasAlpha: true,
        isPlanar: false,
        colorSpaceName: .deviceRGB,
        bytesPerRow: 0,
        bitsPerPixel: 0
    ) else {
        fatalError("Could not create bitmap rep for size \(size)")
    }
    rep.size = NSSize(width: w, height: h)

    guard let ctx = NSGraphicsContext(bitmapImageRep: rep) else {
        fatalError("Could not create graphics context for size \(size)")
    }
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = ctx
    let cgctx = ctx.cgContext
    drawIcon(context: cgctx, canvas: CGFloat(size))
    cgctx.flush()
    NSGraphicsContext.restoreGraphicsState()

    guard let data = rep.representation(using: .png, properties: [:]) else {
        fatalError("Could not encode PNG for size \(size)")
    }
    do {
        try data.write(to: URL(fileURLWithPath: path))
    } catch {
        fatalError("Failed writing \(path): \(error)")
    }
}

// MARK: - Render every size needed for the .iconset

struct IconSpec { let name: String; let size: Int }

let specs: [IconSpec] = [
    IconSpec(name: "icon_16x16.png", size: 16),
    IconSpec(name: "icon_16x16@2x.png", size: 32),
    IconSpec(name: "icon_32x32.png", size: 32),
    IconSpec(name: "icon_32x32@2x.png", size: 64),
    IconSpec(name: "icon_128x128.png", size: 128),
    IconSpec(name: "icon_128x128@2x.png", size: 256),
    IconSpec(name: "icon_256x256.png", size: 256),
    IconSpec(name: "icon_256x256@2x.png", size: 512),
    IconSpec(name: "icon_512x512.png", size: 512),
    IconSpec(name: "icon_512x512@2x.png", size: 1024),
]

for spec in specs {
    let outPath = (iconsetDir as NSString).appendingPathComponent(spec.name)
    renderPNG(size: spec.size, to: outPath)
    print("Rendered \(spec.name) (\(spec.size)x\(spec.size))")
}

// Also save the 1024 preview
let previewPath = (outputDir as NSString).appendingPathComponent("icon_preview.png")
renderPNG(size: 1024, to: previewPath)
print("Rendered icon_preview.png (1024x1024)")

// MARK: - Run iconutil to produce the .icns

let icnsPath = (outputDir as NSString).appendingPathComponent("Reverie.icns")
let process = Process()
process.executableURL = URL(fileURLWithPath: "/usr/bin/iconutil")
process.arguments = ["-c", "icns", iconsetDir, "-o", icnsPath]

do {
    try process.run()
    process.waitUntilExit()
    if process.terminationStatus == 0 {
        print("Wrote \(icnsPath)")
    } else {
        FileHandle.standardError.write("iconutil exited with status \(process.terminationStatus)\n".data(using: .utf8)!)
        exit(Int32(process.terminationStatus))
    }
} catch {
    FileHandle.standardError.write("Failed to run iconutil: \(error)\n".data(using: .utf8)!)
    exit(1)
}
