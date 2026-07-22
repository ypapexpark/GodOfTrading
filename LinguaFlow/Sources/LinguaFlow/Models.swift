import Foundation

enum WritingAction: String, CaseIterable, Codable, Identifiable {
    case translate
    case correct
    case rewrite

    var id: String { rawValue }

    var title: String {
        switch self {
        case .translate: "번역"
        case .correct: "문법 교정"
        case .rewrite: "자연스럽게"
        }
    }

    var shortTitle: String {
        switch self {
        case .translate: "번역"
        case .correct: "교정"
        case .rewrite: "다듬기"
        }
    }

    var symbol: String {
        switch self {
        case .translate: "character.book.closed"
        case .correct: "checkmark.seal"
        case .rewrite: "wand.and.sparkles"
        }
    }

    var hotKeyID: UInt32 {
        switch self {
        case .translate: 1
        case .correct: 2
        case .rewrite: 3
        }
    }

    var shortcutDescription: String {
        switch self {
        case .translate: "⌃⌥T"
        case .correct: "⌃⌥G"
        case .rewrite: "⌃⌥R"
        }
    }
}

enum WritingTone: String, CaseIterable, Codable, Identifiable {
    case natural
    case business
    case polite
    case casual

    var id: String { rawValue }

    var title: String {
        switch self {
        case .natural: "자연스럽게"
        case .business: "비즈니스"
        case .polite: "정중하게"
        case .casual: "친근하게"
        }
    }
}

struct LanguageOption: Identifiable, Hashable {
    let code: String
    let name: String
    let flag: String

    var id: String { code }

    static let supported: [LanguageOption] = [
        .init(code: "en", name: "영어", flag: "🇺🇸"),
        .init(code: "ko", name: "한국어", flag: "🇰🇷"),
        .init(code: "ja", name: "일본어", flag: "🇯🇵"),
        .init(code: "zh-CN", name: "중국어(간체)", flag: "🇨🇳"),
        .init(code: "zh-TW", name: "중국어(번체)", flag: "🇹🇼"),
        .init(code: "es", name: "스페인어", flag: "🇪🇸"),
        .init(code: "fr", name: "프랑스어", flag: "🇫🇷"),
        .init(code: "de", name: "독일어", flag: "🇩🇪"),
        .init(code: "pt", name: "포르투갈어", flag: "🇵🇹"),
        .init(code: "vi", name: "베트남어", flag: "🇻🇳"),
        .init(code: "th", name: "태국어", flag: "🇹🇭"),
        .init(code: "id", name: "인도네시아어", flag: "🇮🇩"),
        .init(code: "ar", name: "아랍어", flag: "🇸🇦"),
        .init(code: "ru", name: "러시아어", flag: "🇷🇺")
    ]

    static func find(_ code: String) -> LanguageOption {
        supported.first(where: { $0.code == code }) ?? supported[0]
    }
}

struct WritingChange: Codable, Identifiable, Equatable {
    let before: String
    let after: String
    let reason: String

    var id: String { "\(before)|\(after)|\(reason)" }
}

struct WritingResult: Codable, Equatable {
    let result: String
    let detectedLanguage: String
    let changes: [WritingChange]
}

enum LinguaFlowError: LocalizedError, Equatable {
    case missingAPIKey
    case emptyInput
    case accessibilityPermissionRequired
    case noSelectedText
    case selectionUnavailable(String)
    case invalidResponse
    case api(status: Int, message: String)
    case keychain(OSStatus)

    var errorDescription: String? {
        switch self {
        case .missingAPIKey:
            "먼저 설정에서 OpenAI API 키를 저장해 주세요."
        case .emptyInput:
            "처리할 문장을 입력하거나 선택해 주세요."
        case .accessibilityPermissionRequired:
            "다른 앱의 선택 문장을 읽으려면 손쉬운 사용 권한이 필요합니다."
        case .noSelectedText:
            "선택된 문장을 찾지 못했습니다. 문장을 드래그한 뒤 단축키를 다시 눌러 주세요."
        case .selectionUnavailable(let detail):
            "선택 문장을 가져오지 못했습니다. \(detail)"
        case .invalidResponse:
            "AI 응답 형식을 확인하지 못했습니다. 다시 시도해 주세요."
        case .api(_, let message):
            message
        case .keychain(let status):
            "API 키를 Keychain에 저장하지 못했습니다. (\(status))"
        }
    }
}
