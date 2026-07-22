import Foundation

enum SentenceExtractor {
    private static let boundaries: Set<Character> = [".", "!", "?", "\n", "。", "！", "？"]

    static func capture(selectedText: String?, contextBeforeInput: String?) -> CapturedText? {
        if let selectedText,
           !selectedText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return CapturedText(
                text: selectedText,
                deleteCount: 0,
                replacesSelection: true
            )
        }

        guard let contextBeforeInput, !contextBeforeInput.isEmpty else { return nil }

        let trailingWhitespaceCount = contextBeforeInput.reversed().prefix {
            $0.isWhitespace && $0 != "\n"
        }.count
        let withoutTrailingWhitespace = contextBeforeInput.dropLast(trailingWhitespaceCount)
        guard !withoutTrailingWhitespace.isEmpty else { return nil }

        var sentenceStart = withoutTrailingWhitespace.startIndex
        if let boundary = withoutTrailingWhitespace.lastIndex(where: { boundaries.contains($0) }) {
            sentenceStart = withoutTrailingWhitespace.index(after: boundary)
        }

        let sentenceSlice = withoutTrailingWhitespace[sentenceStart...]
        let leadingWhitespace = sentenceSlice.prefix(while: { $0.isWhitespace })
        let text = String(sentenceSlice.dropFirst(leadingWhitespace.count))
        guard !text.isEmpty else { return nil }

        return CapturedText(
            text: text,
            deleteCount: text.count + leadingWhitespace.count + trailingWhitespaceCount,
            replacesSelection: false
        )
    }
}
