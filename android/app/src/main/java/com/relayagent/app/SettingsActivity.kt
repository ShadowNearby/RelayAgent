package com.relayagent.app

import android.content.Context
import android.os.Bundle
import androidx.appcompat.app.AppCompatActivity
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import com.relayagent.app.databinding.ActivitySettingsBinding
import org.json.JSONObject

/**
 * LLM endpoint + runtime knobs, stored in EncryptedSharedPreferences — the
 * on-device replacement for the host's .env (LLM_BASE_URL / LLM_API_KEY /
 * LLM_MODEL). loadConfig() hands them to the Python entrypoint, which
 * installs them as env vars for the runtime.
 */
class SettingsActivity : AppCompatActivity() {

    companion object {
        private const val PREFS = "relay_secure_settings"
        const val K_BASE_URL = "LLM_BASE_URL"
        const val K_API_KEY = "LLM_API_KEY"
        const val K_MODEL = "LLM_MODEL"
        private val KEYS = listOf(K_BASE_URL, K_API_KEY, K_MODEL)

        private fun prefs(context: Context) = EncryptedSharedPreferences.create(
            context, PREFS,
            MasterKey.Builder(context).setKeyScheme(MasterKey.KeyScheme.AES256_GCM).build(),
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
        )

        /** Config JSON for the Python entrypoint (entry._install_env). */
        fun loadConfig(context: Context): JSONObject {
            val p = prefs(context)
            val o = JSONObject()
            for (k in KEYS) o.put(k, p.getString(k, "") ?: "")
            return o
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

        ui.saveBtn.setOnClickListener {
            p.edit()
                .putString(K_BASE_URL, ui.fieldBaseUrl.text.toString().trim())
                .putString(K_API_KEY, ui.fieldApiKey.text.toString().trim())
                .putString(K_MODEL, ui.fieldModel.text.toString().trim())
                .apply()
            finish()
        }
    }
}
