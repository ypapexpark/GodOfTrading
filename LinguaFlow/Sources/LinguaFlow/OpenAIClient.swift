import Foundation

struct OpenAIClient {
    let apiKey: String
    let model: String
    var endpoint = URL(string: "https://api.openai.com/v1/responses")!

    func transform(
        text: String,
        action: WritingAction,
        targetLanguage: LanguageOption,
        tone: WritingTone
    ) async throws -> WritingResult {
        var request = URLRequest(url: endpoint)
        request.httpMethod = "POST"
        request.timeoutInterval = 30
        request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try requestBody(
            text: text,
            action: action,
            targetLanguage: targetLanguage,
            tone: tone
        )

        let (data, response) = try await URLSession.shared.data(for: request)
        guard let httpResponse = response as? HTTPURLResponse else {
            throw LinguaFlowError.invalidResponse
        }

        guard (200..<300).contains(httpResponse.statusCode) else {
            let apiMessage = (try? JSONDecoder().decode(APIErrorEnvelope.self, from: data))?.error.message
            throw LinguaFlowError.api(
                status: httpResponse.statusCode,
                message: apiMessage ?? "OpenAI API 요청에 실패했습니다. (HTTP \(httpResponse.statusCode))"
            )
        }

        return try decodeWritingResult(from: data)
    }

    func decodeWritingResult(from data: Data) throws -> WritingResult {
        let envelope = try JSONDecoder().decode(ResponsesEnvelope.self, from: data)
        guard let outputText = envelope.outputText ?? envelope.firstOutputText,
              let jsonData = outputText.data(using: .utf8),
              let result = try? JSONDecoder().decode(WritingResult.self, from: jsonData),
              !result.result.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw LinguaFlowError.invalidResponse
        }
        return result
    }

    func requestBody(
        text: String,
        action: WritingAction,
        targetLanguage: LanguageOption,
        tone: WritingTone
    ) throws -> Data {
        let body: [String: Any] = [
            "model": model,
            "instructions": PromptFactory.instructions(
                action: action,
                targetLanguage: targetLanguage,
                tone: tone
            ),
            "input": text,
            "store": false,
            "reasoning": ["effort": "low"],
            "max_output_tokens": 1_200,
            "text": [
                "verbosity": "low",
                "format": [
                    "type": "json_schema",
                    "name": "writing_result",
                    "strict": true,
                    "schema": PromptFactory.responseSchema
                ]
            ]
        ]
        return try JSONSerialization.data(withJSONObject: body, options: [.sortedKeys])
    }
}

private struct APIErrorEnvelope: Decodable {
    struct APIError: Decodable {
        let message: String
    }

    let error: APIError
}

private struct ResponsesEnvelope: Decodable {
    struct OutputItem: Decodable {
        struct ContentItem: Decodable {
            let type: String?
            let text: String?
        }

        let content: [ContentItem]?
    }

    let outputText: String?
    let output: [OutputItem]

    enum CodingKeys: String, CodingKey {
        case outputText = "output_text"
        case output
    }

    var firstOutputText: String? {
        output
            .lazy
            .compactMap(\.content)
            .joined()
            .first(where: { $0.type == "output_text" && $0.text != nil })?
            .text
    }
}
