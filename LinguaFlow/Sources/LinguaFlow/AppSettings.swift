import Combine
import Foundation

@MainActor
final class AppSettings: ObservableObject {
    static let shared = AppSettings()

    private enum Key {
        static let model = "openaiModel"
        static let targetLanguage = "targetLanguage"
        static let tone = "writingTone"
        static let autoRun = "autoRunFromHotKey"
    }

    private let defaults: UserDefaults
    private let keyStore: APIKeyStore

    @Published var model: String {
        didSet { defaults.set(model, forKey: Key.model) }
    }

    @Published var targetLanguageCode: String {
        didSet { defaults.set(targetLanguageCode, forKey: Key.targetLanguage) }
    }

    @Published var tone: WritingTone {
        didSet { defaults.set(tone.rawValue, forKey: Key.tone) }
    }

    @Published var autoRunFromHotKey: Bool {
        didSet { defaults.set(autoRunFromHotKey, forKey: Key.autoRun) }
    }

    @Published private(set) var hasAPIKey: Bool

    init(defaults: UserDefaults = .standard, keyStore: APIKeyStore = APIKeyStore()) {
        self.defaults = defaults
        self.keyStore = keyStore
        self.model = defaults.string(forKey: Key.model) ?? "gpt-5.6-luna"
        self.targetLanguageCode = defaults.string(forKey: Key.targetLanguage) ?? "en"
        self.tone = WritingTone(rawValue: defaults.string(forKey: Key.tone) ?? "") ?? .natural
        self.autoRunFromHotKey = defaults.object(forKey: Key.autoRun) as? Bool ?? true
        self.hasAPIKey = keyStore.read() != nil
    }

    var targetLanguage: LanguageOption {
        LanguageOption.find(targetLanguageCode)
    }

    func apiKey() -> String? {
        let environmentKey = ProcessInfo.processInfo.environment["OPENAI_API_KEY"]?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        if let environmentKey, !environmentKey.isEmpty {
            return environmentKey
        }
        return keyStore.read()
    }

    func storedAPIKey() -> String {
        keyStore.read() ?? ""
    }

    func saveAPIKey(_ value: String) throws {
        try keyStore.save(value)
        hasAPIKey = keyStore.read() != nil
    }
}
