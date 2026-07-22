import XCTest
@testable import LinguaFlow

final class PromptFactoryTests: XCTestCase {
    func testTranslationPromptDefinesTargetAndTreatsInputAsData() {
        let prompt = PromptFactory.instructions(
            action: .translate,
            targetLanguage: LanguageOption.find("ja"),
            tone: .natural
        )

        XCTAssertTrue(prompt.contains("일본어"))
        XCTAssertTrue(prompt.contains("BCP-47: ja"))
        XCTAssertTrue(prompt.contains("text to transform"))
        XCTAssertTrue(prompt.contains("names, numbers, URLs"))
    }

    func testCorrectionPromptRequestsMinimumEdit() {
        let prompt = PromptFactory.instructions(
            action: .correct,
            targetLanguage: LanguageOption.find("en"),
            tone: .business
        )

        XCTAssertTrue(prompt.contains("smallest useful edit"))
        XCTAssertTrue(prompt.contains("Keep the original language"))
    }

    func testRequestDoesNotStoreTextAndUsesStrictSchema() throws {
        let client = OpenAIClient(apiKey: "test-key", model: "test-model")
        let data = try client.requestBody(
            text: "Hello",
            action: .translate,
            targetLanguage: LanguageOption.find("ko"),
            tone: .natural
        )
        let body = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        let text = try XCTUnwrap(body["text"] as? [String: Any])
        let format = try XCTUnwrap(text["format"] as? [String: Any])

        XCTAssertEqual(body["store"] as? Bool, false)
        XCTAssertEqual(body["model"] as? String, "test-model")
        XCTAssertEqual(body["input"] as? String, "Hello")
        XCTAssertEqual(format["type"] as? String, "json_schema")
        XCTAssertEqual(format["strict"] as? Bool, true)
    }

    func testResponsesEnvelopeDecodesStructuredOutputText() throws {
        let structured = #"{"result":"I agree with your idea.","detectedLanguage":"en","changes":[{"before":"am agree","after":"agree","reason":"Incorrect verb construction"}]}"#
        let response: [String: Any] = [
            "output": [[
                "content": [[
                    "type": "output_text",
                    "text": structured
                ]]
            ]]
        ]
        let data = try JSONSerialization.data(withJSONObject: response)
        let client = OpenAIClient(apiKey: "test-key", model: "test-model")

        let result = try client.decodeWritingResult(from: data)

        XCTAssertEqual(result.result, "I agree with your idea.")
        XCTAssertEqual(result.detectedLanguage, "en")
        XCTAssertEqual(result.changes.first?.before, "am agree")
        XCTAssertEqual(result.changes.first?.after, "agree")
    }
}
