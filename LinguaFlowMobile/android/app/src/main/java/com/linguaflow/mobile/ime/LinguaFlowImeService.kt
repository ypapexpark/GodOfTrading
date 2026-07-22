package com.linguaflow.mobile.ime

import android.graphics.Typeface
import android.inputmethodservice.InputMethodService
import android.os.Build
import android.text.InputType
import android.view.Gravity
import android.view.KeyEvent
import android.view.View
import android.view.ViewGroup
import android.view.inputmethod.EditorInfo
import android.view.inputmethod.InputMethodManager
import android.widget.Button
import android.widget.HorizontalScrollView
import android.widget.LinearLayout
import android.widget.TextView
import com.linguaflow.mobile.R
import com.linguaflow.mobile.core.LinguaPreferences
import com.linguaflow.mobile.core.SentenceExtractor
import com.linguaflow.mobile.core.TransformAction
import com.linguaflow.mobile.core.TransformResult
import com.linguaflow.mobile.engine.TextTransformEngine
import com.linguaflow.mobile.ui.dp
import com.linguaflow.mobile.ui.roundedBackground

class LinguaFlowImeService : InputMethodService() {
    private lateinit var engine: TextTransformEngine
    private lateinit var preferences: LinguaPreferences
    private lateinit var statusText: TextView
    private lateinit var actionButtons: List<Button>
    private var privateField = false
    private var shifted = false
    private var shiftButton: Button? = null

    override fun onCreate() {
        super.onCreate()
        engine = TextTransformEngine(this)
        preferences = LinguaPreferences(this)
    }

    override fun onCreateInputView(): View = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(dp(5), dp(6), dp(5), dp(8))
        setBackgroundColor(getColor(R.color.lf_surface))
        addView(buildToolbar())
        addView(buildKeyboard(), LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply {
            topMargin = dp(5)
        })
    }

    override fun onStartInput(attribute: EditorInfo?, restarting: Boolean) {
        super.onStartInput(attribute, restarting)
        privateField = attribute?.inputType?.let(::isPrivateInputType) == true
        if (::statusText.isInitialized) {
            statusText.text = if (privateField) "보안 입력란에서는 LinguaFlow가 꺼집니다" else "문장 선택 또는 커서 앞 문장에 적용"
        }
        if (::actionButtons.isInitialized) actionButtons.forEach { it.isEnabled = !privateField }
    }

    private fun buildToolbar(): View {
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }

        statusText = TextView(this).apply {
            text = "문장 선택 또는 커서 앞 문장에 적용"
            textSize = 11f
            maxLines = 1
            setTextColor(getColor(R.color.lf_muted))
            setPadding(dp(7), 0, dp(5), 0)
        }
        row.addView(statusText, LinearLayout.LayoutParams(0, dp(40), 1f))

        actionButtons = TransformAction.entries.map { action ->
            toolbarButton(action.label, action == TransformAction.TRANSLATE).also { button ->
                button.setOnClickListener { transformCurrentText(action) }
                row.addView(button, LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, dp(40)).apply {
                    marginStart = dp(4)
                })
            }
        }

        row.addView(toolbarButton("⌄").apply { setOnClickListener { requestHideSelf(0) } }, LinearLayout.LayoutParams(dp(40), dp(40)).apply {
            marginStart = dp(4)
        })

        return HorizontalScrollView(this).apply {
            isHorizontalScrollBarEnabled = false
            addView(row, ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(40)))
        }
    }

    private fun buildKeyboard(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        addView(letterRow("qwertyuiop"))
        addView(letterRow("asdfghjkl"))
        addView(letterRow("zxcvbnm", withShift = true, withBackspace = true))
        addView(bottomRow())
    }

    private fun letterRow(
        keys: String,
        withShift: Boolean = false,
        withBackspace: Boolean = false,
    ): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER
        setPadding(if (keys.length == 9) dp(13) else 0, 0, if (keys.length == 9) dp(13) else 0, 0)

        if (withShift) {
            shiftButton = keyboardButton("⇧").apply {
                contentDescription = "Shift"
                setOnClickListener { toggleShift() }
            }
            addView(shiftButton, keyParams(1.35f))
        }
        keys.forEach { character ->
            addView(keyboardButton(character.toString()).apply {
                tag = character
                setOnClickListener { typeLetter(character) }
            }, keyParams())
        }
        if (withBackspace) {
            addView(keyboardButton("⌫").apply {
                contentDescription = "Backspace"
                setOnClickListener { sendDownUpKeyEvents(KeyEvent.KEYCODE_DEL) }
                setOnLongClickListener {
                    currentInputConnection?.deleteSurroundingText(8, 0)
                    true
                }
            }, keyParams(1.35f))
        }
    }

    private fun bottomRow(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER
        addView(keyboardButton("123").apply {
            isEnabled = false
            alpha = 0.45f
            contentDescription = "숫자 키보드는 다음 버전에서 지원"
        }, keyParams(1.3f))
        addView(keyboardButton("🌐").apply {
            contentDescription = "다음 키보드"
            setOnClickListener {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                    switchToNextInputMethod(false)
                } else {
                    (getSystemService(INPUT_METHOD_SERVICE) as InputMethodManager).showInputMethodPicker()
                }
            }
        }, keyParams(1.15f))
        addView(keyboardButton("space").apply {
            contentDescription = "Space"
            setOnClickListener { currentInputConnection?.commitText(" ", 1) }
        }, keyParams(4.2f))
        addView(keyboardButton(".").apply { setOnClickListener { currentInputConnection?.commitText(".", 1) } }, keyParams())
        addView(keyboardButton("↵").apply {
            contentDescription = "Enter"
            setOnClickListener { sendDefaultEditorAction(true) }
            setOnLongClickListener {
                currentInputConnection?.commitText("\n", 1)
                true
            }
        }, keyParams(1.35f))
    }

    private fun keyParams(weight: Float = 1f) = LinearLayout.LayoutParams(0, dp(48), weight).apply {
        setMargins(dp(2), dp(2), dp(2), dp(2))
    }

    private fun toolbarButton(label: String, primary: Boolean = false): Button = Button(this).apply {
        text = label
        textSize = 12f
        isAllCaps = false
        minWidth = 0
        minimumWidth = 0
        setPadding(dp(11), 0, dp(11), 0)
        setTextColor(getColor(if (primary) android.R.color.white else R.color.lf_primary))
        background = roundedBackground(
            if (primary) R.color.lf_primary else R.color.lf_card,
            11,
            if (primary) null else R.color.lf_line,
        )
    }

    private fun keyboardButton(label: String): Button = Button(this).apply {
        text = label
        textSize = 16f
        isAllCaps = false
        minWidth = 0
        minimumWidth = 0
        minHeight = 0
        minimumHeight = 0
        setPadding(0, 0, 0, 0)
        setTextColor(getColor(R.color.lf_ink))
        setTypeface(typeface, Typeface.NORMAL)
        background = roundedBackground(R.color.lf_card, 7, R.color.lf_line)
    }

    private fun typeLetter(character: Char) {
        val value = if (shifted) character.uppercaseChar() else character
        currentInputConnection?.commitText(value.toString(), 1)
        if (shifted) toggleShift()
    }

    private fun toggleShift() {
        shifted = !shifted
        shiftButton?.apply {
            text = if (shifted) "⇧" else "⇧"
            setTextColor(getColor(if (shifted) R.color.lf_primary else R.color.lf_ink))
        }
    }

    private fun transformCurrentText(action: TransformAction) {
        if (privateField) return
        val target = captureTarget()
        if (target == null) {
            statusText.text = "먼저 문장을 입력하거나 선택해 주세요"
            return
        }

        setActionsEnabled(false)
        engine.transform(action, target.text, preferences.targetLanguage) { result ->
            when (result) {
                is TransformResult.Progress -> statusText.text = result.message
                is TransformResult.Success -> {
                    replaceTarget(target, result.text)
                    statusText.text = result.detail
                    setActionsEnabled(true)
                }
                is TransformResult.Unavailable -> {
                    statusText.text = if (result.message.contains("Gemini Nano")) {
                        "이 기기는 Gemini Nano 교정·다듬기 미지원"
                    } else {
                        result.message
                    }
                    setActionsEnabled(true)
                }
                is TransformResult.Failure -> {
                    statusText.text = result.message
                    setActionsEnabled(true)
                }
            }
        }
    }

    private fun captureTarget(): EditingTarget? {
        val connection = currentInputConnection ?: return null
        val selected = connection.getSelectedText(0)?.toString().orEmpty()
        if (selected.isNotBlank()) return EditingTarget(selected, deleteBeforeCursor = 0, trailingWhitespace = "")

        val beforeCursor = connection.getTextBeforeCursor(1_000, 0)?.toString().orEmpty()
        val sentence = SentenceExtractor.trailingSentence(beforeCursor)
        if (sentence.isBlank()) return null
        val start = beforeCursor.lastIndexOf(sentence)
        if (start < 0) return null
        return EditingTarget(
            text = sentence,
            deleteBeforeCursor = beforeCursor.length - start,
            trailingWhitespace = beforeCursor.substring(start + sentence.length),
        )
    }

    private fun replaceTarget(target: EditingTarget, replacement: String) {
        currentInputConnection?.apply {
            beginBatchEdit()
            if (target.deleteBeforeCursor > 0) deleteSurroundingText(target.deleteBeforeCursor, 0)
            commitText(replacement + target.trailingWhitespace, 1)
            endBatchEdit()
        }
    }

    private fun setActionsEnabled(enabled: Boolean) {
        actionButtons.forEach { it.isEnabled = enabled && !privateField }
    }

    private fun isPrivateInputType(inputType: Int): Boolean {
        val textVariation = inputType and InputType.TYPE_MASK_VARIATION
        val inputClass = inputType and InputType.TYPE_MASK_CLASS
        return (inputClass == InputType.TYPE_CLASS_TEXT && textVariation in setOf(
            InputType.TYPE_TEXT_VARIATION_PASSWORD,
            InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD,
            InputType.TYPE_TEXT_VARIATION_WEB_PASSWORD,
        )) || (inputClass == InputType.TYPE_CLASS_NUMBER && textVariation == InputType.TYPE_NUMBER_VARIATION_PASSWORD)
    }

    override fun onDestroy() {
        engine.close()
        super.onDestroy()
    }

    private data class EditingTarget(
        val text: String,
        val deleteBeforeCursor: Int,
        val trailingWhitespace: String,
    )
}
