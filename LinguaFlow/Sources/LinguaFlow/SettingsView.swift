import SwiftUI

struct SettingsView: View {
    @EnvironmentObject private var settings: AppSettings
    @State private var apiKey = ""
    @State private var statusMessage: String?
    @State private var isError = false

    var body: some View {
        Form {
            Section("OpenAI") {
                SecureField("sk-...", text: $apiKey)
                    .textFieldStyle(.roundedBorder)

                HStack {
                    Label(
                        settings.hasAPIKey ? "Keychain에 API 키가 저장되어 있습니다." : "API 키가 아직 없습니다.",
                        systemImage: settings.hasAPIKey ? "checkmark.shield.fill" : "key"
                    )
                    .foregroundStyle(settings.hasAPIKey ? .green : .secondary)
                    .font(.caption)
                    Spacer()
                    Button("저장") { saveKey() }
                        .buttonStyle(.borderedProminent)
                }

                TextField("모델", text: $settings.model)
                    .textFieldStyle(.roundedBorder)
                Text("짧은 번역·교정의 속도와 비용을 고려한 기본값은 gpt-5.6-luna입니다.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Section("동작") {
                Picker("기본 번역 언어", selection: $settings.targetLanguageCode) {
                    ForEach(LanguageOption.supported) { language in
                        Text("\(language.flag) \(language.name)").tag(language.code)
                    }
                }
                Toggle("단축키로 호출하면 즉시 실행", isOn: $settings.autoRunFromHotKey)

                VStack(alignment: .leading, spacing: 6) {
                    Text("전역 단축키")
                        .font(.headline)
                    Text("번역 ⌃⌥T   ·   문법 교정 ⌃⌥G   ·   자연스럽게 ⌃⌥R")
                        .font(.system(.caption, design: .monospaced))
                        .foregroundStyle(.secondary)
                }
            }

            Section("macOS 권한") {
                HStack {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("손쉬운 사용 권한")
                        Text("다른 앱에서 선택한 문장을 읽고 결과로 교체할 때만 사용합니다.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("권한 요청") {
                        AppModel.shared.requestAccessibilityPermission()
                    }
                }
            }

            Section("개인정보") {
                Label("LinguaFlow는 선택하여 실행한 문장만 전송하며 원문을 앱 로그에 저장하지 않습니다.", systemImage: "lock.shield")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            if let statusMessage {
                Text(statusMessage)
                    .font(.caption)
                    .foregroundStyle(isError ? .red : .green)
            }
        }
        .formStyle(.grouped)
        .frame(width: 520, height: 500)
        .onAppear {
            apiKey = settings.storedAPIKey()
        }
    }

    private func saveKey() {
        do {
            try settings.saveAPIKey(apiKey)
            statusMessage = apiKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                ? "저장된 API 키를 삭제했습니다."
                : "API 키를 Keychain에 저장했습니다."
            isError = false
        } catch {
            statusMessage = error.localizedDescription
            isError = true
        }
    }
}
