package com.linguaflow.mobile.core

import org.junit.Assert.assertEquals
import org.junit.Test

class SentenceExtractorTest {
    @Test
    fun extractsCurrentSentenceAfterPunctuation() {
        assertEquals("How are you", SentenceExtractor.trailingSentence("Hello. How are you"))
    }

    @Test
    fun includesFinalPunctuation() {
        assertEquals("How are you?", SentenceExtractor.trailingSentence("Hello. How are you?"))
    }

    @Test
    fun understandsKoreanAndJapaneseBoundaries() {
        assertEquals("내일 만나요！", SentenceExtractor.trailingSentence("안녕하세요。 내일 만나요！"))
    }

    @Test
    fun ignoresWhitespaceAroundSentence() {
        assertEquals("Nice to meet you.", SentenceExtractor.trailingSentence("Hello!   Nice to meet you.  "))
    }

    @Test
    fun returnsEmptyForWhitespace() {
        assertEquals("", SentenceExtractor.trailingSentence("   \n  "))
    }
}
