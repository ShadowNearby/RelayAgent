package com.relayagent.app

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/**
 * loadConfig round-trips through EncryptedSharedPreferences — on a real
 * device this exercises the hardware Keystore-backed master key, which
 * Robolectric can't. Touched keys are backed up and restored so the user's
 * actual on-device settings survive the test run.
 */
@RunWith(AndroidJUnit4::class)
class SettingsConfigTest {

    private val context: Context = ApplicationProvider.getApplicationContext()

    private val textKeys = listOf(
        SettingsActivity.K_BASE_URL, SettingsActivity.K_API_KEY,
        SettingsActivity.K_MODEL, SettingsActivity.K_MAX_STEP,
    )
    private val boolKeys = listOf(SettingsActivity.K_STEP_LOG, SettingsActivity.K_CAPTURE_FULL)

    private lateinit var textBackup: Map<String, String?>
    private lateinit var boolBackup: Map<String, Boolean?>

    @Before
    fun backupPrefs() {
        val p = SettingsActivity.prefs(context)
        textBackup = textKeys.associateWith { p.getString(it, null) }
        boolBackup = boolKeys.associateWith { if (p.contains(it)) p.getBoolean(it, true) else null }
    }

    @After
    fun restorePrefs() {
        val e = SettingsActivity.prefs(context).edit()
        textBackup.forEach { (k, v) -> if (v == null) e.remove(k) else e.putString(k, v) }
        boolBackup.forEach { (k, v) -> if (v == null) e.remove(k) else e.putBoolean(k, v) }
        e.commit()
    }

    @Test
    fun loadConfigRoundTripsThroughEncryptedPrefs() {
        SettingsActivity.prefs(context).edit()
            .putString(SettingsActivity.K_BASE_URL, "http://gw.test:8000/v1")
            .putString(SettingsActivity.K_API_KEY, "sk-test-123")
            .putString(SettingsActivity.K_MODEL, "qwen")
            .putString(SettingsActivity.K_MAX_STEP, "25")
            .putBoolean(SettingsActivity.K_STEP_LOG, false)
            .commit()

        val cfg = SettingsActivity.loadConfig(context)
        assertEquals("http://gw.test:8000/v1", cfg.getString(SettingsActivity.K_BASE_URL))
        assertEquals("sk-test-123", cfg.getString(SettingsActivity.K_API_KEY))
        assertEquals("qwen", cfg.getString(SettingsActivity.K_MODEL))
        assertEquals("25", cfg.getString(SettingsActivity.K_MAX_STEP))
        assertEquals("0", cfg.getString(SettingsActivity.K_STEP_LOG))
    }

    @Test
    fun togglesDefaultOnAndBlanksStayBlank() {
        SettingsActivity.prefs(context).edit()
            .remove(SettingsActivity.K_CAPTURE_FULL)
            .remove(SettingsActivity.K_MAX_STEP)
            .commit()

        val cfg = SettingsActivity.loadConfig(context)
        // Unset toggles are forwarded as "1" (runtime defaults ON) …
        assertEquals("1", cfg.getString(SettingsActivity.K_CAPTURE_FULL))
        // … and unset text/number fields stay blank so entry._install_env
        // leaves the runtime default untouched.
        assertEquals("", cfg.getString(SettingsActivity.K_MAX_STEP))
    }
}
