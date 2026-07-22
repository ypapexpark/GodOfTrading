package com.linguaflow.mobile.core

enum class WritingLanguage {
    ENGLISH,
    KOREAN,
    JAPANESE,
    GERMAN,
    FRENCH,
    ITALIAN,
    SPANISH,
}

object LanguageHeuristics {
    fun writingLanguage(text: String): WritingLanguage {
        if (text.any { it.code in 0xAC00..0xD7A3 || it.code in 0x1100..0x11FF }) {
            return WritingLanguage.KOREAN
        }
        if (text.any { it.code in 0x3040..0x30FF }) return WritingLanguage.JAPANESE

        // The on-device writing APIs need a language up front. Latin text defaults to English;
        // the settings screen explains this MVP limitation.
        return WritingLanguage.ENGLISH
    }
}
