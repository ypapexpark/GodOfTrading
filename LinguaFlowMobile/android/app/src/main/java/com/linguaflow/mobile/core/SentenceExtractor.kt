package com.linguaflow.mobile.core

object SentenceExtractor {
    private val sentenceBoundary = Regex("[.!?。！？\\n]")

    /** Returns the current non-blank sentence immediately before the cursor. */
    fun trailingSentence(textBeforeCursor: CharSequence): String {
        val raw = textBeforeCursor.toString()
        if (raw.isBlank()) return ""

        val end = raw.trimEnd().length
        var start = end - 1
        if (start >= 0 && sentenceBoundary.matches(raw[start].toString())) start--
        while (start >= 0 && !sentenceBoundary.matches(raw[start].toString())) {
            start--
        }
        return raw.substring(start + 1, end).trimStart()
    }
}
