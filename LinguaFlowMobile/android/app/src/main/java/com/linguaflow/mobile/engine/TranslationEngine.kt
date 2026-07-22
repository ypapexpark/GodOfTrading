package com.linguaflow.mobile.engine

import com.google.mlkit.common.model.DownloadConditions
import com.google.mlkit.nl.languageid.LanguageIdentification
import com.google.mlkit.nl.translate.TranslateLanguage
import com.google.mlkit.nl.translate.Translation
import com.google.mlkit.nl.translate.TranslatorOptions
import com.linguaflow.mobile.core.TargetLanguage
import com.linguaflow.mobile.core.TransformResult

class TranslationEngine {
    private val languageIdentifier = LanguageIdentification.getClient()

    fun translate(
        text: String,
        targetLanguage: TargetLanguage,
        callback: (TransformResult) -> Unit,
    ) {
        languageIdentifier.identifyLanguage(text)
            .addOnSuccessListener { sourceTag ->
                if (sourceTag == "und") {
                    callback(TransformResult.Unavailable("입력 언어를 판별하지 못했어요. 문장을 조금 더 길게 써보세요."))
                    return@addOnSuccessListener
                }

                val source = TranslateLanguage.fromLanguageTag(sourceTag)
                val target = TranslateLanguage.fromLanguageTag(targetLanguage.tag)
                if (source == null || target == null) {
                    callback(TransformResult.Unavailable("이 언어 조합은 기기 내 번역에서 지원하지 않아요."))
                    return@addOnSuccessListener
                }
                if (source == target) {
                    callback(TransformResult.Success(text, "이미 ${targetLanguage.label} 문장입니다 · Google 번역"))
                    return@addOnSuccessListener
                }

                val translator = Translation.getClient(
                    TranslatorOptions.Builder()
                        .setSourceLanguage(source)
                        .setTargetLanguage(target)
                        .build(),
                )
                callback(TransformResult.Progress("번역 모델을 확인하고 있어요…"))
                translator.downloadModelIfNeeded(DownloadConditions.Builder().build())
                    .addOnSuccessListener {
                        translator.translate(text)
                            .addOnSuccessListener { translated ->
                                callback(
                                    TransformResult.Success(
                                        translated,
                                        "$sourceTag → ${targetLanguage.label} · Google 번역 제공",
                                    ),
                                )
                                translator.close()
                            }
                            .addOnFailureListener { error ->
                                callback(TransformResult.Failure("번역에 실패했어요: ${error.readableMessage()}", error))
                                translator.close()
                            }
                    }
                    .addOnFailureListener { error ->
                        callback(
                            TransformResult.Failure(
                                "번역 모델을 받을 수 없어요. 인터넷 연결을 확인한 뒤 다시 시도하세요.",
                                error,
                            ),
                        )
                        translator.close()
                    }
            }
            .addOnFailureListener { error ->
                callback(TransformResult.Failure("언어 판별에 실패했어요: ${error.readableMessage()}", error))
            }
    }
}

internal fun Throwable.readableMessage(): String = message?.takeIf { it.isNotBlank() } ?: "알 수 없는 오류"
