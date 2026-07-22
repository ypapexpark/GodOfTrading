// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "LinguaFlow",
    platforms: [
        .macOS(.v14)
    ],
    products: [
        .executable(name: "LinguaFlow", targets: ["LinguaFlow"])
    ],
    targets: [
        .executableTarget(
            name: "LinguaFlow",
            linkerSettings: [
                .linkedFramework("ApplicationServices"),
                .linkedFramework("Carbon"),
                .linkedFramework("Security")
            ]
        ),
        .testTarget(
            name: "LinguaFlowTests",
            dependencies: ["LinguaFlow"]
        )
    ],
    swiftLanguageModes: [.v5]
)
