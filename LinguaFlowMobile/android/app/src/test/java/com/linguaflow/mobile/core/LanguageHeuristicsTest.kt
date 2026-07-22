package com.linguaflow.mobile.core

import org.junit.Assert.assertEquals
import org.junit.Test

class LanguageHeuristicsTest {
    @Test
    fun detectsKorean() {
        assertEquals(WritingLanguage.KOREAN, LanguageHeuristics.writingLanguage("내일 회의가 있어요."))
    }

    @Test
    fun detectsJapaneseKana() {
        assertEquals(WritingLanguage.JAPANESE, LanguageHeuristics.writingLanguage("また明日ね。"))
    }

    @Test
    fun defaultsLatinScriptToEnglish() {
        assertEquals(WritingLanguage.ENGLISH, LanguageHeuristics.writingLanguage("I has a meeting tomorrow."))
    }
}
