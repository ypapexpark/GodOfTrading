package com.linguaflow.mobile.ui

import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.graphics.Typeface
import android.os.Bundle
import android.provider.Settings
import android.view.inputmethod.InputMethodManager
import android.widget.ArrayAdapter
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Spinner
import android.widget.TextView
import androidx.core.net.toUri
import com.linguaflow.mobile.R
import com.linguaflow.mobile.core.LinguaPreferences
import com.linguaflow.mobile.core.TargetLanguage
import com.linguaflow.mobile.core.TransformAction
import com.linguaflow.mobile.core.TransformResult
import com.linguaflow.mobile.engine.TextTransformEngine

class MainActivity : Activity() {
    private lateinit var preferences: LinguaPreferences
    private lateinit var engine: TextTransformEngine
    private lateinit var demoInput: EditText
    private lateinit var demoStatus: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        preferences = LinguaPreferences(this)
        engine = TextTransformEngine(this)
        setContentView(buildContent())
    }

    private fun buildContent(): ScrollView {
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(22), dp(32), dp(22), dp(32))
            setBackgroundColor(getColor(R.color.lf_surface))
        }

        root.addView(title("LinguaFlow", 30f))
        root.addView(body("앱을 옮겨 다니지 않고, 쓰던 입력창에서 바로 번역하고 문장을 고칩니다.", 16f).withMargins(top = 8))

        root.addView(buildSetupCard().withMargins(top = 24))
        root.addView(buildTargetCard().withMargins(top = 14))
        root.addView(buildPrivacyCard().withMargins(top = 14))
        root.addView(buildDemoCard().withMargins(top = 14))

        return ScrollView(this).apply { addView(root) }
    }

    private fun buildSetupCard() = card().apply {
        addView(title("1. 키보드 연결", 19f))
        addView(body("설정에서 LinguaFlow 키보드를 활성화한 뒤, 입력창의 지구본 버튼으로 전환하세요.").withMargins(top = 6))

        val buttons = LinearLayout(this@MainActivity).apply {
            orientation = LinearLayout.HORIZONTAL
            addView(actionButton("키보드 활성화").apply {
                setOnClickListener { startActivity(Intent(Settings.ACTION_INPUT_METHOD_SETTINGS)) }
            }.withMargins(width = 0), LinearLayout.LayoutParams(0, dp(44), 1f))
            addView(actionButton("키보드 선택", primary = true).apply {
                setOnClickListener {
                    (getSystemService(INPUT_METHOD_SERVICE) as InputMethodManager).showInputMethodPicker()
                }
            }.withMargins(width = 0, left = 8), LinearLayout.LayoutParams(0, dp(44), 1f))
        }
        addView(buttons.withMargins(top = 14))

        addView(body("팁: 키보드를 바꾸지 않아도 문장을 길게 눌러 선택 → 더보기 → ‘LinguaFlow로 다듬기’를 사용할 수 있어요.", 13f).withMargins(top = 12))
    }

    private fun buildTargetCard() = card().apply {
        addView(title("2. 기본 번역 언어", 19f))
        val spinner = languageSpinner()
        addView(spinner.withMargins(top = 10))
        addView(body("입력 언어는 자동으로 감지합니다. 번역 모델은 최초 사용 시 한 번 내려받습니다.", 13f).withMargins(top = 8))
    }

    private fun buildPrivacyCard() = card().apply {
        addView(title("무료 · 기기 내 처리", 19f))
        addView(statusLine("✓", "번역", "ML Kit 온디바이스 · 50개 이상 언어"))
        addView(statusLine("◇", "교정·다듬기", "Gemini Nano 지원 기기에서 온디바이스"))
        addView(body("유료 API 키는 사용하지 않습니다. Gemini Nano 미지원 기기에서도 번역은 정상적으로 쓸 수 있어요.", 13f).withMargins(top = 10))
        addView(actionButton("Google 번역 제공 · 번역 고지 보기").apply {
            setOnClickListener { showTranslationNotice() }
        }.withMargins(top = 12))
    }

    private fun statusLine(symbol: String, name: String, detail: String) = TextView(this).apply {
        text = getString(R.string.status_line, symbol, name, detail)
        textSize = 14f
        setTextColor(getColor(R.color.lf_ink))
        setLineSpacing(0f, 1.15f)
        setPadding(0, dp(12), 0, 0)
    }

    private fun buildDemoCard() = card().apply {
        addView(title("바로 시험해 보기", 19f))
        demoInput = EditText(this@MainActivity).apply {
            hint = "예: I has a meeting tomorrow."
            minLines = 3
            gravity = android.view.Gravity.TOP
            setPadding(dp(14), dp(12), dp(14), dp(12))
            setTextColor(getColor(R.color.lf_ink))
            setHintTextColor(getColor(R.color.lf_muted))
            background = roundedBackground(R.color.lf_surface, 12, R.color.lf_line)
        }
        addView(demoInput.withMargins(top = 12))

        val actions = LinearLayout(this@MainActivity).apply {
            orientation = LinearLayout.HORIZONTAL
            TransformAction.entries.forEachIndexed { index, action ->
                addView(actionButton(action.label, primary = action == TransformAction.TRANSLATE).apply {
                    setOnClickListener { runDemo(action) }
                }, LinearLayout.LayoutParams(0, dp(44), 1f).apply {
                    if (index > 0) marginStart = dp(7)
                })
            }
        }
        addView(actions.withMargins(top = 10))

        demoStatus = body("결과는 이 입력칸에 바로 반영됩니다.", 13f).apply {
            setTypeface(typeface, Typeface.NORMAL)
        }
        addView(demoStatus.withMargins(top = 10))
    }

    private fun languageSpinner(): Spinner = Spinner(this).apply {
        val languages = TargetLanguage.entries
        adapter = ArrayAdapter(
            this@MainActivity,
            android.R.layout.simple_spinner_dropdown_item,
            languages.map { it.label },
        )
        setSelection(languages.indexOf(preferences.targetLanguage))
        onItemSelectedListener = object : android.widget.AdapterView.OnItemSelectedListener {
            override fun onItemSelected(parent: android.widget.AdapterView<*>?, view: android.view.View?, position: Int, id: Long) {
                preferences.targetLanguage = languages[position]
            }

            override fun onNothingSelected(parent: android.widget.AdapterView<*>?) = Unit
        }
    }

    private fun runDemo(action: TransformAction) {
        demoStatus.text = "처리 중…"
        engine.transform(action, demoInput.text.toString(), preferences.targetLanguage, ::showDemoResult)
    }

    private fun showTranslationNotice() {
        AlertDialog.Builder(this)
            .setTitle("자동 번역 고지")
            .setMessage(
                "LinguaFlow의 번역은 Google 번역 기술로 제공됩니다. 이 서비스에는 Google에서 제공하는 번역이 포함될 수 있습니다. " +
                    "Google은 번역의 정확성·신뢰성 및 상품성, 특정 목적 적합성, 비침해에 대한 명시적 또는 묵시적 보증을 부인합니다.",
            )
            .setPositiveButton("확인", null)
            .setNeutralButton("Google 번역 열기") { _, _ ->
                startActivity(Intent(Intent.ACTION_VIEW, "https://translate.google.com".toUri()))
            }
            .show()
    }

    private fun showDemoResult(result: TransformResult) {
        when (result) {
            is TransformResult.Success -> {
                demoInput.setText(result.text)
                demoInput.setSelection(result.text.length)
                demoStatus.text = result.detail
            }
            is TransformResult.Progress -> demoStatus.text = result.message
            is TransformResult.Unavailable -> demoStatus.text = result.message
            is TransformResult.Failure -> demoStatus.text = result.message
        }
    }

    override fun onDestroy() {
        engine.close()
        super.onDestroy()
    }
}
