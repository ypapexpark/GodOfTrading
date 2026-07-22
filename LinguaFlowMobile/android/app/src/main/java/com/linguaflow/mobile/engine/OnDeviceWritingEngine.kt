package com.linguaflow.mobile.engine

import android.content.Context
import androidx.core.content.ContextCompat
import com.google.common.util.concurrent.FutureCallback
import com.google.common.util.concurrent.Futures
import com.google.common.util.concurrent.ListenableFuture
import com.google.mlkit.genai.common.DownloadCallback
import com.google.mlkit.genai.common.FeatureStatus
import com.google.mlkit.genai.common.GenAiException
import com.google.mlkit.genai.proofreading.Proofreader
import com.google.mlkit.genai.proofreading.ProofreaderOptions
import com.google.mlkit.genai.proofreading.Proofreading
import com.google.mlkit.genai.proofreading.ProofreadingRequest
import com.google.mlkit.genai.rewriting.Rewriter
import com.google.mlkit.genai.rewriting.RewriterOptions
import com.google.mlkit.genai.rewriting.Rewriting
import com.google.mlkit.genai.rewriting.RewritingRequest
import com.linguaflow.mobile.core.LanguageHeuristics
import com.linguaflow.mobile.core.TransformResult
import com.linguaflow.mobile.core.WritingLanguage
import java.util.concurrent.Executor

/**
 * Grammar and rewriting through Android's AICore/Gemini Nano. No text is sent to a cloud API.
 * Availability is intentionally checked at runtime because supported Android devices are limited.
 */
class OnDeviceWritingEngine(private val context: Context) : AutoCloseable {
    private val mainExecutor: Executor = ContextCompat.getMainExecutor(context)
    private val openProofreaders = mutableSetOf<Proofreader>()
    private val openRewriters = mutableSetOf<Rewriter>()

    fun proofread(text: String, callback: (TransformResult) -> Unit) {
        val language = proofreaderLanguage(LanguageHeuristics.writingLanguage(text))
        val options = ProofreaderOptions.builder(context)
            .setInputType(ProofreaderOptions.InputType.KEYBOARD)
            .setLanguage(language)
            .build()
        val proofreader = Proofreading.getClient(options)
        openProofreaders += proofreader

        prepareFeature(
            statusFuture = proofreader.checkFeatureStatus(),
            download = { downloadCallback -> proofreader.downloadFeature(downloadCallback) },
            callback = callback,
            run = {
                val request = ProofreadingRequest.builder(text).build()
                Futures.addCallback(
                    proofreader.runInference(request),
                    object : FutureCallback<com.google.mlkit.genai.proofreading.ProofreadingResult> {
                        override fun onSuccess(result: com.google.mlkit.genai.proofreading.ProofreadingResult) {
                            val suggestion = result.results.firstOrNull()?.text
                            if (suggestion.isNullOrBlank()) {
                                callback(TransformResult.Success(text, "고칠 부분을 찾지 못했어요 · Gemini Nano"))
                            } else {
                                callback(TransformResult.Success(suggestion, "문법·맞춤법 교정 · Gemini Nano"))
                            }
                            closeProofreader(proofreader)
                        }

                        override fun onFailure(t: Throwable) {
                            callback(TransformResult.Failure("문장 교정에 실패했어요: ${t.readableMessage()}", t))
                            closeProofreader(proofreader)
                        }
                    },
                    mainExecutor,
                )
            },
            onUnavailable = { closeProofreader(proofreader) },
        )
    }

    fun rewrite(text: String, callback: (TransformResult) -> Unit) {
        val language = rewriterLanguage(LanguageHeuristics.writingLanguage(text))
        val options = RewriterOptions.builder(context)
            .setOutputType(RewriterOptions.OutputType.PROFESSIONAL)
            .setLanguage(language)
            .build()
        val rewriter = Rewriting.getClient(options)
        openRewriters += rewriter

        prepareFeature(
            statusFuture = rewriter.checkFeatureStatus(),
            download = { downloadCallback -> rewriter.downloadFeature(downloadCallback) },
            callback = callback,
            run = {
                val request = RewritingRequest.builder(text).build()
                Futures.addCallback(
                    rewriter.runInference(request),
                    object : FutureCallback<com.google.mlkit.genai.rewriting.RewritingResult> {
                        override fun onSuccess(result: com.google.mlkit.genai.rewriting.RewritingResult) {
                            val suggestion = result.results.firstOrNull()?.text
                            if (suggestion.isNullOrBlank()) {
                                callback(TransformResult.Success(text, "다른 표현을 찾지 못했어요 · Gemini Nano"))
                            } else {
                                callback(TransformResult.Success(suggestion, "자연스럽고 전문적인 표현 · Gemini Nano"))
                            }
                            closeRewriter(rewriter)
                        }

                        override fun onFailure(t: Throwable) {
                            callback(TransformResult.Failure("문장 다듬기에 실패했어요: ${t.readableMessage()}", t))
                            closeRewriter(rewriter)
                        }
                    },
                    mainExecutor,
                )
            },
            onUnavailable = { closeRewriter(rewriter) },
        )
    }

    private fun prepareFeature(
        statusFuture: ListenableFuture<Int>,
        download: (DownloadCallback) -> ListenableFuture<Void>,
        callback: (TransformResult) -> Unit,
        run: () -> Unit,
        onUnavailable: () -> Unit,
    ) {
        Futures.addCallback(
            statusFuture,
            object : FutureCallback<Int> {
                override fun onSuccess(status: Int) {
                    when (status) {
                        FeatureStatus.AVAILABLE -> run()
                        FeatureStatus.UNAVAILABLE -> {
                            callback(TransformResult.Unavailable(UNSUPPORTED_MESSAGE))
                            onUnavailable()
                        }
                        else -> downloadAndRun(download, callback, run, onUnavailable)
                    }
                }

                override fun onFailure(t: Throwable) {
                    callback(TransformResult.Unavailable("이 기기에서 Gemini Nano 상태를 확인할 수 없어요. ${t.readableMessage()}"))
                    onUnavailable()
                }
            },
            mainExecutor,
        )
    }

    private fun downloadAndRun(
        download: (DownloadCallback) -> ListenableFuture<Void>,
        callback: (TransformResult) -> Unit,
        run: () -> Unit,
        onUnavailable: () -> Unit,
    ) {
        var totalBytes = 0L
        callback(TransformResult.Progress("기기 내 글쓰기 모델을 준비하고 있어요…"))
        val future = download(
            object : DownloadCallback {
                override fun onDownloadStarted(bytesToDownload: Long) {
                    totalBytes = bytesToDownload
                }

                override fun onDownloadFailed(e: GenAiException) {
                    callback(TransformResult.Failure("글쓰기 모델 다운로드에 실패했어요: ${e.readableMessage()}", e))
                    onUnavailable()
                }

                override fun onDownloadProgress(totalBytesDownloaded: Long) {
                    if (totalBytes > 0L) {
                        val progress = (100L * totalBytesDownloaded / totalBytes).coerceIn(0L, 100L)
                        callback(TransformResult.Progress("기기 내 글쓰기 모델 다운로드 중 · $progress%"))
                    }
                }

                override fun onDownloadCompleted() = run()
            },
        )
        Futures.addCallback(
            future,
            object : FutureCallback<Void?> {
                override fun onSuccess(result: Void?) = Unit

                override fun onFailure(t: Throwable) {
                    // Some SDK versions report the same failure through DownloadCallback as well.
                    if (t !is GenAiException) {
                        callback(TransformResult.Failure("글쓰기 모델을 준비하지 못했어요: ${t.readableMessage()}", t))
                        onUnavailable()
                    }
                }
            },
            mainExecutor,
        )
    }

    private fun proofreaderLanguage(language: WritingLanguage): Int = when (language) {
        WritingLanguage.KOREAN -> ProofreaderOptions.Language.KOREAN
        WritingLanguage.JAPANESE -> ProofreaderOptions.Language.JAPANESE
        WritingLanguage.GERMAN -> ProofreaderOptions.Language.GERMAN
        WritingLanguage.FRENCH -> ProofreaderOptions.Language.FRENCH
        WritingLanguage.ITALIAN -> ProofreaderOptions.Language.ITALIAN
        WritingLanguage.SPANISH -> ProofreaderOptions.Language.SPANISH
        WritingLanguage.ENGLISH -> ProofreaderOptions.Language.ENGLISH
    }

    private fun rewriterLanguage(language: WritingLanguage): Int = when (language) {
        WritingLanguage.KOREAN -> RewriterOptions.Language.KOREAN
        WritingLanguage.JAPANESE -> RewriterOptions.Language.JAPANESE
        WritingLanguage.GERMAN -> RewriterOptions.Language.GERMAN
        WritingLanguage.FRENCH -> RewriterOptions.Language.FRENCH
        WritingLanguage.ITALIAN -> RewriterOptions.Language.ITALIAN
        WritingLanguage.SPANISH -> RewriterOptions.Language.SPANISH
        WritingLanguage.ENGLISH -> RewriterOptions.Language.ENGLISH
    }

    private fun closeProofreader(proofreader: Proofreader) {
        openProofreaders -= proofreader
        proofreader.close()
    }

    private fun closeRewriter(rewriter: Rewriter) {
        openRewriters -= rewriter
        rewriter.close()
    }

    override fun close() {
        openProofreaders.toList().forEach(::closeProofreader)
        openRewriters.toList().forEach(::closeRewriter)
    }

    companion object {
        private const val UNSUPPORTED_MESSAGE =
            "이 기기는 Gemini Nano 교정·다듬기를 지원하지 않아요. 번역 기능은 계속 무료로 사용할 수 있습니다."
    }
}
