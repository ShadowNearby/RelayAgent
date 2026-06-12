package com.relayagent.app

import android.content.Context
import android.os.Bundle
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
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
        private val KEYS = listOf("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")

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

    private val fields = mutableMapOf<String, EditText>()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(32, 32, 32, 32)
        }
        val p = prefs(this)
        for (key in KEYS) {
            root.addView(TextView(this).apply { text = key })
            val field = EditText(this).apply { setText(p.getString(key, "") ?: "") }
            fields[key] = field
            root.addView(field)
        }
        root.addView(Button(this).apply {
            text = "保存"
            setOnClickListener {
                val e = p.edit()
                for ((k, f) in fields) e.putString(k, f.text.toString().trim())
                e.apply()
                finish()
            }
        })
        setContentView(root)
    }
}
