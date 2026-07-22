package com.linguaflow.mobile.ui

import android.app.Activity
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.graphics.Typeface
import android.os.Bundle
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.Window
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.LinearLayout
import android.widget.Spinner
import android.widget.TextView
import android.widget.Toast
import com.linguaflow.mobile.R
import com.linguaflow.mobile.core.LinguaPreferences
import com.linguaflow.mobile.core.TargetLanguage
import com.linguaflow.mobile.core.TransformAction
import com.linguaflow.mobile.core.TransformResult
import com.linguaflow.mobile.engine.TextTransformEngine

class ProcessTextActivity : Activity() {
    private lateinit var engine: TextTransformEngine
    private lateinit var preferences: LinguaPreferences
    private lateinit var originalText: String
    private lateinit var resultText: TextView
    private lateinit var statusText: TextView
    private lateinit var applyButton: Button
    private lateinit var actionButtons: List<Button>
    private var transformedText: String? = null
    private var isReadOnly: Boolean = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        originalText = intent.getCharSequenceExtra(Intent.EXTRA_PROCESS_TEXT)?.toString().orEmpty()
        isReadOnly = intent.getBooleanExtra(Intent.EXTRA_PROCESS_TEXT_READONLY, false)
        if (originalText.isBlank()) {
            finish()
            return
        }

        engine = TextTransformEngine(this)
        preferences = LinguaPreferences(this)
        setContentView(buildContent())
    }

    override fun onStart() {
        super.onStart()
        window.setLayout(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
        window.setGravity(Gravity.BOTTOM)
        window.setDimAmount(0.35f)
        window.addFlags(android.view.WindowManager.LayoutParams.FLAG_DIM_BEHIND)
    }

    private fun buildContent(): View {
        requestWindowFeature(Window.FEATURE_NO_TITLE)
        return LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(22), dp(20), dp(22), dp(24))
            background = roundedBackground(R.color.lf_surface, 24)

            addView(title("LinguaFlow", 22f))
            addView(body("선택한 문장을 이 자리에서 바로 바꿉니다.", 14f).withMargins(top = 4))
            addView(buildLanguageRow().withMargins(top = 14))
            addView(buildOriginalCard().withMargins(top = 12))
            addView(buildActionRow().withMargins(top = 10))
            addView(buildResultCard().withMargins(top = 12))
            addView(buildBottomRow().withMargins(top = 12))
        }
    }

    private fun buildLanguageRow() = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
        addView(body("번역 대상", 13f), LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))

        val languages = TargetLanguage.entries
        addView(Spinner(this@ProcessTextActivity).apply {
            adapter = ArrayAdapter(
                this@ProcessTextActivity,
                android.R.layout.simple_spinner_dropdown_item,
                languages.map { it.label },
            )
            setSelection(languages.indexOf(preferences.targetLanguage))
            onItemSelectedListener = object : android.widget.AdapterView.OnItemSelectedListener {
                override fun onItemSelected(parent: android.widget.AdapterView<*>?, view: View?, position: Int, id: Long) {
                    preferences.targetLanguage = languages[position]
                }

                override fun onNothingSelected(parent: android.widget.AdapterView<*>?) = Unit
            }
        })
    }

    private fun buildOriginalCard() = card().apply {
        setPadding(dp(14), dp(12), dp(14), dp(12))
        addView(body("원문", 12f).apply { setTypeface(typeface, Typeface.BOLD) })
        addView(TextView(this@ProcessTextActivity).apply {
            text = originalText
            textSize = 16f
            setTextColor(getColor(R.color.lf_ink))
            maxLines = 5
        }.withMargins(top = 5))
    }

    private fun buildActionRow(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        actionButtons = TransformAction.entries.mapIndexed { index, action ->
            actionButton(action.label, primary = action == TransformAction.TRANSLATE).also { button ->
                button.setOnClickListener { transform(action) }
                addView(button, LinearLayout.LayoutParams(0, dp(44), 1f).apply {
                    if (index > 0) marginStart = dp(7)
                })
            }
        }
    }

    private fun buildResultCard() = card().apply {
        setPadding(dp(14), dp(12), dp(14), dp(12))
        statusText = body("기능을 선택하세요.", 12f).apply { setTypeface(typeface, Typeface.BOLD) }
        addView(statusText)
        resultText = TextView(this@ProcessTextActivity).apply {
            text = "결과가 여기에 표시됩니다."
            textSize = 16f
            setTextColor(getColor(R.color.lf_ink))
            setTextIsSelectable(true)
            maxLines = 7
        }
        addView(resultText.withMargins(top = 5))
    }

    private fun buildBottomRow() = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        addView(actionButton("취소").apply { setOnClickListener { finish() } }, LinearLayout.LayoutParams(0, dp(46), 1f))
        addView(actionButton("복사").apply { setOnClickListener { copyResult() } }, LinearLayout.LayoutParams(0, dp(46), 1f).apply {
            marginStart = dp(8)
        })
        applyButton = actionButton("원문 교체", primary = true).apply {
            isEnabled = false
            visibility = if (isReadOnly) View.GONE else View.VISIBLE
            setOnClickListener { returnResult() }
        }
        addView(applyButton, LinearLayout.LayoutParams(0, dp(46), 1.2f).apply { marginStart = dp(8) })
    }

    private fun transform(action: TransformAction) {
        setBusy(true)
        transformedText = null
        engine.transform(action, originalText, preferences.targetLanguage, ::renderResult)
    }

    private fun renderResult(result: TransformResult) {
        when (result) {
            is TransformResult.Progress -> {
                statusText.text = result.message
                resultText.text = "잠시만 기다려 주세요…"
            }
            is TransformResult.Success -> {
                transformedText = result.text
                statusText.text = result.detail
                resultText.text = result.text
                setBusy(false)
                applyButton.isEnabled = !isReadOnly
            }
            is TransformResult.Unavailable -> {
                statusText.text = "이 기기에서는 사용할 수 없음"
                resultText.text = result.message
                setBusy(false)
            }
            is TransformResult.Failure -> {
                statusText.text = "처리 실패"
                resultText.text = result.message
                setBusy(false)
            }
        }
    }

    private fun setBusy(busy: Boolean) {
        if (::actionButtons.isInitialized) actionButtons.forEach { it.isEnabled = !busy }
        if (::applyButton.isInitialized && busy) applyButton.isEnabled = false
    }

    private fun copyResult() {
        val value = transformedText ?: return
        (getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager)
            .setPrimaryClip(ClipData.newPlainText("LinguaFlow", value))
        Toast.makeText(this, "결과를 복사했어요.", Toast.LENGTH_SHORT).show()
    }

    private fun returnResult() {
        val value = transformedText ?: return
        setResult(
            RESULT_OK,
            Intent().putExtra(Intent.EXTRA_PROCESS_TEXT, value),
        )
        finish()
    }

    override fun onDestroy() {
        if (::engine.isInitialized) engine.close()
        super.onDestroy()
    }
}
