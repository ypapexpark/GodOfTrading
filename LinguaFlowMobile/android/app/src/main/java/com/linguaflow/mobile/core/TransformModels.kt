package com.linguaflow.mobile.core

enum class TransformAction(val label: String) {
    TRANSLATE("Google 번역"),
    PROOFREAD("교정"),
    REWRITE("다듬기"),
}

enum class TargetLanguage(val tag: String, val label: String) {
    ENGLISH("en", "영어"),
    KOREAN("ko", "한국어"),
    JAPANESE("ja", "일본어"),
    CHINESE("zh", "중국어"),
    SPANISH("es", "스페인어"),
    FRENCH("fr", "프랑스어"),
    GERMAN("de", "독일어"),
    ITALIAN("it", "이탈리아어"),
    PORTUGUESE("pt", "포르투갈어");

    companion object {
        fun fromTag(tag: String?): TargetLanguage = entries.firstOrNull { it.tag == tag } ?: ENGLISH
    }
}

sealed interface TransformResult {
    data class Success(
        val text: String,
        val detail: String,
    ) : TransformResult

    data class Progress(val message: String) : TransformResult
    data class Unavailable(val message: String) : TransformResult
    data class Failure(val message: String, val cause: Throwable? = null) : TransformResult
}
