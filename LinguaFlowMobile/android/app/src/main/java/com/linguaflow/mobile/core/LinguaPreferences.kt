package com.linguaflow.mobile.core

import android.content.Context
import androidx.core.content.edit

class LinguaPreferences(context: Context) {
    private val preferences = context.getSharedPreferences(FILE_NAME, Context.MODE_PRIVATE)

    var targetLanguage: TargetLanguage
        get() = TargetLanguage.fromTag(preferences.getString(KEY_TARGET_LANGUAGE, null))
        set(value) {
            preferences.edit { putString(KEY_TARGET_LANGUAGE, value.tag) }
        }

    companion object {
        private const val FILE_NAME = "linguaflow_preferences"
        private const val KEY_TARGET_LANGUAGE = "target_language"
    }
}
