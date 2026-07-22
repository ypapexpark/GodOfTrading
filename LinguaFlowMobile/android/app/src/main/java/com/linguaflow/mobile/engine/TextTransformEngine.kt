package com.linguaflow.mobile.engine

import android.content.Context
import com.linguaflow.mobile.core.TargetLanguage
import com.linguaflow.mobile.core.TransformAction
import com.linguaflow.mobile.core.TransformResult

class TextTransformEngine(context: Context) : AutoCloseable {
    private val translationEngine = TranslationEngine()
    private val writingEngine = OnDeviceWritingEngine(context.applicationContext)

    fun transform(
        action: TransformAction,
        text: String,
        targetLanguage: TargetLanguage,
        callback: (TransformResult) -> Unit,
    ) {
        val normalized = text.trim()
        if (normalized.isEmpty()) {
            callback(TransformResult.Unavailable("먼저 문장을 선택하거나 입력해 주세요."))
            return
        }

        when (action) {
            TransformAction.TRANSLATE -> translationEngine.translate(normalized, targetLanguage, callback)
            TransformAction.PROOFREAD -> writingEngine.proofread(normalized, callback)
            TransformAction.REWRITE -> writingEngine.rewrite(normalized, callback)
        }
    }

    override fun close() = writingEngine.close()
}
