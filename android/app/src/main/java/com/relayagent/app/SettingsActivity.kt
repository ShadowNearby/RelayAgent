package com.relayagent.app

import android.content.Context
import android.content.SharedPreferences
import android.os.Bundle
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.app.AppCompatDelegate
import androidx.core.os.LocaleListCompat
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import com.relayagent.app.databinding.ActivitySettingsBinding
import org.json.JSONObject

/**
 * LLM endpoint + runtime knobs + appearance, stored in
 * EncryptedSharedPreferences — the on-device replacement for the host's .env.
 * loadConfig() hands the LLM config + runtime knobs to the Python entrypoint,
 * which installs them as env vars (entry._install_env). Language/theme are
 * applied locally via AppCompat.
 */
class SettingsActivity : AppCompatActivity() {

    companion object {
        private const val PREFS = "relay_secure_settings"

        // LLM endpoint (host .env parity).
        const val K_BASE_URL = "LLM_BASE_URL"
        const val K_API_KEY = "LLM_API_KEY"
        const val K_MODEL = "LLM_MODEL"

        // Runtime knobs (forwarded as env vars to the host runtime).
        const val K_MAX_STEP = "max_step"
        const val K_STEP_WAIT = "RELAY_STEP_WAIT"
        const val K_WAIT_SECONDS = "RELAY_WAIT_SECONDS"
        const val K_CAPTURE_FULL = "RELAY_CAPTURE_FULL_REPLY"
        const val K_SCROLL_RATIO = "RELAY_CAPTURE_SCROLL_RATIO"
        const val K_CROP_TOP = "RELAY_CROP_TOP"
        const val K_CROP_BOTTOM = "RELAY_CROP_BOTTOM"
        const val K_STEP_LOG = "RELAY_STEP_LOG"
        const val K_FRESH_CONV = "RELAY_FRESH_CONV"
        const val K_DISMISS_PERMS = "RELAY_DISMISS_PERMISSIONS"

        // Appearance (local; never forwarded to Python).
        const val K_LANGUAGE = "ui_language"  // "system" | "zh" | "en"
        const val K_THEME = "ui_theme"         // "system" | "light" | "dark"

        private val TEXT_KEYS = listOf(K_BASE_URL, K_API_KEY, K_MODEL)
        private val NUM_KEYS = listOf(
            K_MAX_STEP, K_STEP_WAIT, K_WAIT_SECONDS,
            K_SCROLL_RATIO, K_CROP_TOP, K_CROP_BOTTOM,
        )
        private val BOOL_KEYS = listOf(K_CAPTURE_FULL, K_STEP_LOG, K_FRESH_CONV, K_DISMISS_PERMS)

        // Creating EncryptedSharedPreferences walks the Keystore + disk every
        // time (30-150ms, a known ANR source) and this is hit on the main
        // thread from every MainActivity resume and every send — cache one
        // instance for the process lifetime (also the single point to touch
        // when migrating off the deprecated androidx.security-crypto).
        @Volatile
        private var cachedPrefs: SharedPreferences? = null

        fun prefs(context: Context): SharedPreferences =
            cachedPrefs ?: synchronized(this) {
                cachedPrefs ?: run {
                    val app = context.applicationContext
                    EncryptedSharedPreferences.create(
                        app, PREFS,
                        MasterKey.Builder(app)
                            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM).build(),
                        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
                        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
                    ).also { cachedPrefs = it }
                }
            }

        /** Config JSON for the Python entrypoint (entry._install_env). */
        fun loadConfig(context: Context): JSONObject {
            val p = prefs(context)
            val o = JSONObject()
            for (k in TEXT_KEYS) o.put(k, p.getString(k, "") ?: "")
            for (k in NUM_KEYS) o.put(k, p.getString(k, "") ?: "")
            // Toggles default ON; forwarded as "1"/"0".
            for (k in BOOL_KEYS) o.put(k, if (p.getBoolean(k, true)) "1" else "0")
            return o
        }

        /** Apply the persisted theme/language at process start (RelayApp). */
        fun applyAppearance(context: Context) {
            val p = prefs(context)
            AppCompatDelegate.setDefaultNightMode(nightMode(p.getString(K_THEME, "system")))
            // Language is auto-restored by AppCompat (autoStoreLocales), but apply
            // explicitly too so a fresh install with a stored pref is consistent.
            val lang = p.getString(K_LANGUAGE, "system")
            if (lang != "system" && AppCompatDelegate.getApplicationLocales().isEmpty) {
                AppCompatDelegate.setApplicationLocales(LocaleListCompat.forLanguageTags(lang))
            }
        }

        private fun nightMode(theme: String?): Int = when (theme) {
            "light" -> AppCompatDelegate.MODE_NIGHT_NO
            "dark" -> AppCompatDelegate.MODE_NIGHT_YES
            else -> AppCompatDelegate.MODE_NIGHT_FOLLOW_SYSTEM
        }
    }

    private lateinit var ui: ActivitySettingsBinding

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        ui = ActivitySettingsBinding.inflate(layoutInflater)
        setContentView(ui.root)
        ui.toolbar.setNavigationOnClickListener { finish() }

        val p = prefs(this)
        ui.fieldBaseUrl.setText(p.getString(K_BASE_URL, "") ?: "")
        ui.fieldApiKey.setText(p.getString(K_API_KEY, "") ?: "")
        ui.fieldModel.setText(p.getString(K_MODEL, "") ?: "")
        ui.fieldMaxStep.setText(p.getString(K_MAX_STEP, "") ?: "")
        ui.fieldStepWait.setText(p.getString(K_STEP_WAIT, "") ?: "")
        ui.fieldWaitSeconds.setText(p.getString(K_WAIT_SECONDS, "") ?: "")
        ui.fieldScrollRatio.setText(p.getString(K_SCROLL_RATIO, "") ?: "")
        ui.fieldCropTop.setText(p.getString(K_CROP_TOP, "") ?: "")
        ui.fieldCropBottom.setText(p.getString(K_CROP_BOTTOM, "") ?: "")

        ui.switchCaptureFull.isChecked = p.getBoolean(K_CAPTURE_FULL, true)
        ui.switchStepLog.isChecked = p.getBoolean(K_STEP_LOG, true)
        ui.switchFreshConv.isChecked = p.getBoolean(K_FRESH_CONV, true)
        ui.switchDismissPerms.isChecked = p.getBoolean(K_DISMISS_PERMS, true)

        // Appearance segmented controls.
        ui.toggleLanguage.check(
            when (p.getString(K_LANGUAGE, "system")) {
                "zh" -> R.id.btnLangZh
                "en" -> R.id.btnLangEn
                else -> R.id.btnLangSystem
            }
        )
        ui.toggleTheme.check(
            when (p.getString(K_THEME, "system")) {
                "light" -> R.id.btnThemeLight
                "dark" -> R.id.btnThemeDark
                else -> R.id.btnThemeSystem
            }
        )

        ui.saveBtn.setOnClickListener { save(p) }
    }

    private fun save(p: android.content.SharedPreferences) {
        fun field(t: CharSequence?) = t?.toString()?.trim().orEmpty()
        val newLang = when (ui.toggleLanguage.checkedButtonId) {
            R.id.btnLangZh -> "zh"
            R.id.btnLangEn -> "en"
            else -> "system"
        }
        val newTheme = when (ui.toggleTheme.checkedButtonId) {
            R.id.btnThemeLight -> "light"
            R.id.btnThemeDark -> "dark"
            else -> "system"
        }

        p.edit()
            .putString(K_BASE_URL, field(ui.fieldBaseUrl.text))
            .putString(K_API_KEY, field(ui.fieldApiKey.text))
            .putString(K_MODEL, field(ui.fieldModel.text))
            .putString(K_MAX_STEP, field(ui.fieldMaxStep.text))
            .putString(K_STEP_WAIT, field(ui.fieldStepWait.text))
            .putString(K_WAIT_SECONDS, field(ui.fieldWaitSeconds.text))
            .putString(K_SCROLL_RATIO, field(ui.fieldScrollRatio.text))
            .putString(K_CROP_TOP, field(ui.fieldCropTop.text))
            .putString(K_CROP_BOTTOM, field(ui.fieldCropBottom.text))
            .putBoolean(K_CAPTURE_FULL, ui.switchCaptureFull.isChecked)
            .putBoolean(K_STEP_LOG, ui.switchStepLog.isChecked)
            .putBoolean(K_FRESH_CONV, ui.switchFreshConv.isChecked)
            .putBoolean(K_DISMISS_PERMS, ui.switchDismissPerms.isChecked)
            .putString(K_LANGUAGE, newLang)
            .putString(K_THEME, newTheme)
            .apply()

        Toast.makeText(this, R.string.settings_saved, Toast.LENGTH_SHORT).show()

        // Apply appearance — these recreate activities, so do them last.
        AppCompatDelegate.setDefaultNightMode(nightMode(newTheme))
        AppCompatDelegate.setApplicationLocales(
            if (newLang == "system") LocaleListCompat.getEmptyLocaleList()
            else LocaleListCompat.forLanguageTags(newLang)
        )
        finish()
    }
}
