package com.linguaflow.mobile.ui

import android.content.Context
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import com.linguaflow.mobile.R

internal fun Context.dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

internal fun Context.roundedBackground(color: Int, radiusDp: Int, strokeColor: Int? = null): GradientDrawable =
    GradientDrawable().apply {
        shape = GradientDrawable.RECTANGLE
        setColor(getColor(color))
        cornerRadius = dp(radiusDp).toFloat()
        strokeColor?.let { setStroke(dp(1), getColor(it)) }
    }

internal fun Context.title(text: String, size: Float = 24f): TextView = TextView(this).apply {
    this.text = text
    textSize = size
    setTextColor(getColor(R.color.lf_ink))
    setTypeface(typeface, Typeface.BOLD)
}

internal fun Context.body(text: String, size: Float = 15f): TextView = TextView(this).apply {
    this.text = text
    textSize = size
    setTextColor(getColor(R.color.lf_muted))
    setLineSpacing(0f, 1.25f)
}

internal fun Context.actionButton(text: String, primary: Boolean = false): Button = Button(this).apply {
    this.text = text
    textSize = 14f
    isAllCaps = false
    minHeight = dp(44)
    setPadding(dp(14), 0, dp(14), 0)
    setTextColor(getColor(if (primary) android.R.color.white else R.color.lf_primary))
    background = roundedBackground(
        if (primary) R.color.lf_primary else R.color.lf_card,
        12,
        if (primary) null else R.color.lf_line,
    )
}

internal fun Context.card(): LinearLayout = LinearLayout(this).apply {
    orientation = LinearLayout.VERTICAL
    setPadding(dp(18), dp(18), dp(18), dp(18))
    background = roundedBackground(R.color.lf_card, 18, R.color.lf_line)
}

internal fun View.withMargins(
    width: Int = ViewGroup.LayoutParams.MATCH_PARENT,
    height: Int = ViewGroup.LayoutParams.WRAP_CONTENT,
    left: Int = 0,
    top: Int = 0,
    right: Int = 0,
    bottom: Int = 0,
): View = apply {
    layoutParams = LinearLayout.LayoutParams(width, height).apply {
        setMargins(context.dp(left), context.dp(top), context.dp(right), context.dp(bottom))
    }
}
