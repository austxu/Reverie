// swift-tools-version: 6.4
import PackageDescription

let package = Package(
    name: "Reverie",
    platforms: [.macOS(.v27)],
    targets: [
        .executableTarget(
            name: "Reverie",
            path: "Sources/Reverie",
            swiftSettings: [.swiftLanguageMode(.v5)]
        )
    ]
)
