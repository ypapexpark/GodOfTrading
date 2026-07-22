import Foundation
import FoundationModels
import NaturalLanguage
import Translation

struct OnDeviceTextEngine: Sendable {
    func transform(
        _ text: String,
        action: TransformAction,
        targetLanguage: TargetLanguage
    ) async throws -> String {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { throw LinguaFlowError.emptyText }

        switch action {
        case .translate:
            return try await translate(trimmed, to: targetLanguage)
        case .proofread:
            return try await improve(trimmed, action: .proofread)
        case .rewrite:
            return try await improve(trimmed, action: .rewrite)
        }
    }

    private func translate(_ text: String, to targetLanguage: TargetLanguage) async throws -> String {
        guard let sourceCode = NLLanguageRecognizer.dominantLanguage(for: text)?.rawValue else {
            throw LinguaFlowError.sourceLanguageUnknown
        }
        guard sourceCode != targetLanguage.rawValue else {
            throw LinguaFlowError.sameLanguage
        }

        let source = Locale.Language(identifier: sourceCode)
        let target = targetLanguage.localeLanguage
        let availability = await LanguageAvailability().status(from: source, to: target)

        switch availability {
        case .installed:
            break
        case .supported:
            throw LinguaFlowError.languageModelNeedsPreparation
        case .unsupported:
            throw LinguaFlowError.languageUnsupported
        @unknown default:
            throw LinguaFlowError.languageUnsupported
        }

        let session = TranslationSession(installedSource: source, target: target)
        let response = try await session.translate(text)
        let result = response.targetText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !result.isEmpty else { throw LinguaFlowError.emptyResult }
        return result
    }

    private func improve(_ text: String, action: TransformAction) async throws -> String {
        let model = SystemLanguageModel(
            useCase: .general,
            guardrails: .permissiveContentTransformations
        )

        switch model.availability {
        case .available:
            break
        case .unavailable(.deviceNotEligible):
            throw LinguaFlowError.appleIntelligenceUnsupported
        case .unavailable(.appleIntelligenceNotEnabled):
            throw LinguaFlowError.appleIntelligenceDisabled
        case .unavailable(.modelNotReady):
            throw LinguaFlowError.appleIntelligenceNotReady
        @unknown default:
            throw LinguaFlowError.appleIntelligenceNotReady
        }

        let instruction: String
        let request: String
        switch action {
        case .proofread:
            instruction = "Correct grammar, spelling, punctuation, and unnatural phrasing while preserving meaning, language, names, numbers, links, and tone. Return only the corrected text."
            request = "Correct this text without explaining your changes:\n\n\(text)"
        case .rewrite:
            instruction = "Rewrite text so it sounds clear, natural, and concise while preserving meaning, language, names, numbers, links, and intent. Return only the rewritten text."
            request = "Polish this text without explaining your changes:\n\n\(text)"
        case .translate:
            preconditionFailure("Translation must use the Translation framework.")
        }

        let session = LanguageModelSession(model: model, instructions: instruction)
        let response = try await session.respond(to: request)
        let result = response.content.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !result.isEmpty else { throw LinguaFlowError.emptyResult }
        return result
    }
}
