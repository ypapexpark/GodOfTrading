import AppKit
import SwiftUI

@main
struct LinguaFlowApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        MenuBarExtra {
            MenuBarView()
                .environmentObject(AppModel.shared)
                .environmentObject(AppSettings.shared)
        } label: {
            Label("LinguaFlow", systemImage: "character.bubble.fill")
        }

        Settings {
            SettingsView()
                .environmentObject(AppSettings.shared)
        }
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        AppModel.shared.start()
        AppModel.shared.showWindow()
    }
}

private struct MenuBarView: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var settings: AppSettings

    var body: some View {
        Button("LinguaFlow 열기", systemImage: "macwindow") {
            model.showWindow()
        }
        Divider()
        ForEach(WritingAction.allCases) { action in
            Button("\(action.title)  \(action.shortcutDescription)", systemImage: action.symbol) {
                model.captureFromHotKey(action: action)
            }
        }
        Divider()
        if !settings.hasAPIKey {
            Text("API 키 설정이 필요합니다")
                .foregroundStyle(.secondary)
        }
        SettingsLink {
            Label("설정…", systemImage: "gearshape")
        }
        Divider()
        Button("종료", systemImage: "power") {
            NSApp.terminate(nil)
        }
    }
}
