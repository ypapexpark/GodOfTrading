import Foundation

enum TransformAction: String, CaseIterable, Sendable {
    case translate
    case proofread
    case rewrite

    var title: String {
        switch self {
        case .translate: "번역"
        case .proofread: "교정"
        case .rewrite: "다듬기"
        }
    }
}

enum TargetLanguage: String, CaseIterable, Identifiable, Sendable {
    case korean = "ko"
    case english = "en"
    case japanese = "ja"

    var id: String { rawValue }

    var title: String {
        switch self {
        case .korean: "한국어"
        case .english: "English"
        case .japanese: "日本語"
        }
    }

    var shortTitle: String {
        switch self {
        case .korean: "한"
        case .english: "EN"
        case .japanese: "日"
        }
    }

    var localeLanguage: Locale.Language {
        Locale.Language(identifier: rawValue)
    }

    func next() -> TargetLanguage {
        let all = Self.allCases
        guard let index = all.firstIndex(of: self) else { return .korean }
        return all[(index + 1) % all.count]
    }
}

struct CapturedText: Equatable, Sendable {
    let text: String
    let deleteCount: Int
    let replacesSelection: Bool
}

enum LinguaFlowError: LocalizedError, Equatable {
    case emptyText
    case sourceLanguageUnknown
    case sameLanguage
    case languageUnsupported
    case languageModelNeedsPreparation
    case appleIntelligenceUnsupported
    case appleIntelligenceDisabled
    case appleIntelligenceNotReady
    case emptyResult

    var errorDescription: String? {
        switch self {
        case .emptyText:
            "먼저 문장을 입력하거나 선택해 주세요."
        case .sourceLanguageUnknown:
            "원문 언어를 인식하지 못했습니다. 조금 더 긴 문장을 선택해 주세요."
        case .sameLanguage:
            "원문과 번역 대상 언어가 같습니다."
        case .languageUnsupported:
            "이 언어 조합은 Apple 온디바이스 번역에서 지원하지 않습니다."
        case .languageModelNeedsPreparation:
            "LinguaFlow 앱에서 이 언어의 번역 모델을 먼저 준비해 주세요."
        case .appleIntelligenceUnsupported:
            "이 기기는 Apple Intelligence를 지원하지 않습니다. 번역은 계속 사용할 수 있습니다."
        case .appleIntelligenceDisabled:
            "설정에서 Apple Intelligence를 켜 주세요. 번역은 계속 사용할 수 있습니다."
        case .appleIntelligenceNotReady:
            "Apple Intelligence 모델이 아직 준비 중입니다. 잠시 후 다시 시도해 주세요."
        case .emptyResult:
            "변환 결과가 비어 있습니다. 다시 시도해 주세요."
        }
    }
}
