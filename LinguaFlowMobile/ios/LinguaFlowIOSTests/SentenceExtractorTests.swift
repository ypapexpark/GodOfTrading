import XCTest
@testable import LinguaFlowIOS

final class SentenceExtractorTests: XCTestCase {
    func testSelectionWinsOverContext() {
        let result = SentenceExtractor.capture(
            selectedText: "Selected text",
            contextBeforeInput: "Ignored sentence"
        )

        XCTAssertEqual(result, CapturedText(text: "Selected text", deleteCount: 0, replacesSelection: true))
    }

    func testCapturesCurrentSentenceAfterBoundary() {
        let result = SentenceExtractor.capture(
            selectedText: nil,
            contextBeforeInput: "Already sent. I has a meeting tomorrow"
        )

        XCTAssertEqual(
            result,
            CapturedText(text: "I has a meeting tomorrow", deleteCount: 25, replacesSelection: false)
        )
    }

    func testIncludesWhitespaceInReplacementCount() {
        let result = SentenceExtractor.capture(
            selectedText: nil,
            contextBeforeInput: "Hello!   Rewrite me  "
        )

        XCTAssertEqual(
            result,
            CapturedText(text: "Rewrite me", deleteCount: 15, replacesSelection: false)
        )
    }

    func testEmptyContextReturnsNil() {
        XCTAssertNil(SentenceExtractor.capture(selectedText: "  ", contextBeforeInput: nil))
    }
}
