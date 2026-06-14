package com.relayagent.app

import android.app.Activity
import android.content.Intent
import android.media.projection.MediaProjectionManager
import android.os.Bundle
import android.provider.Settings
import android.text.method.ScrollingMovementMethod
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.widget.addTextChangedListener
import com.relayagent.app.databinding.ActivityMainBinding

/**
 * Designed frontend: goal box + Run + onboarding status + a live log pane,
 * plus entries into the bundled task examples and on-device run logs.
 *
 * Run flow: ensure a11y service enabled -> request MediaProjection consent
 * (per session, Android 14) -> start capture service -> overlay chip ->
 * PythonRuntime.runFlow.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var ui: ActivityMainBinding
    private var pendingGoal: String? = null

    private val projectionConsent =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
            val goal = pendingGoal ?: return@registerForActivityResult
            pendingGoal = null
            if (result.resultCode != Activity.RESULT_OK || result.data == null) {
                RunLog.append("屏幕采集授权被拒绝，无法运行。")
                return@registerForActivityResult
            }
            ScreenCaptureService.start(this, result.resultCode, result.data!!)
            launchFlow(goal)
        }

    private val pickExample =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
            val instruction = result.data?.getStringExtra(ExamplesActivity.EXTRA_INSTRUCTION)
            if (result.resultCode == Activity.RESULT_OK && !instruction.isNullOrBlank()) {
                ui.goalInput.setText(instruction)
                ui.goalInput.setSelection(instruction.length)
            }
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        ui = ActivityMainBinding.inflate(layoutInflater)
        setContentView(ui.root)
        setSupportActionBar(ui.toolbar)

        ui.logView.movementMethod = ScrollingMovementMethod()
        ui.logView.text = RunLog.snapshot()

        ui.goalInput.addTextChangedListener { ui.goalLayout.error = null }
        ui.runBtn.setOnClickListener { onRunClicked() }
        ui.stopBtn.setOnClickListener {
            DeviceBridge.requestStop()
            RunLog.append("已请求停止。")
        }
        ui.examplesBtn.setOnClickListener {
            pickExample.launch(Intent(this, ExamplesActivity::class.java))
        }
        ui.logsBtn.setOnClickListener {
            startActivity(Intent(this, LogActivity::class.java))
        }
        ui.toolbar.inflateMenu(R.menu.main)
        ui.toolbar.setOnMenuItemClickListener {
            when (it.itemId) {
                R.id.action_settings -> {
                    startActivity(Intent(this, SettingsActivity::class.java)); true
                }
                R.id.action_open_a11y -> {
                    startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)); true
                }
                else -> false
            }
        }
    }

    override fun onResume() {
        super.onResume()
        refreshStatus()
        // Live log: append new lines and keep scrolled to the bottom.
        ui.logView.text = RunLog.snapshot()
        scrollLogToBottom()
        RunLog.listener = { line ->
            if (line == null) ui.logView.text = RunLog.snapshot()
            else ui.logView.append("\n$line")
            scrollLogToBottom()
        }
    }

    override fun onPause() {
        super.onPause()
        RunLog.listener = null
    }

    private fun scrollLogToBottom() {
        ui.logView.post {
            val lines = ui.logView.layout?.lineCount ?: return@post
            val y = ui.logView.layout.getLineTop(lines) - ui.logView.height
            if (y > 0) ui.logView.scrollTo(0, y) else ui.logView.scrollTo(0, 0)
        }
    }

    private fun refreshStatus() {
        val a11yOn = RelayAccessibilityService.instance != null
        val cfg = SettingsActivity.loadConfig(this)
        val gatewaySet = cfg.optString("LLM_BASE_URL").isNotEmpty()
        ui.statusA11y.text =
            (if (a11yOn) "✅ " else "❌ ") +
                getString(if (a11yOn) R.string.status_a11y_on else R.string.status_a11y_off)
        ui.statusGateway.text =
            (if (gatewaySet) "✅ " else "❌ ") +
                getString(if (gatewaySet) R.string.status_gateway_on else R.string.status_gateway_off)
    }

    private fun onRunClicked() {
        val goal = ui.goalInput.text?.toString()?.trim().orEmpty()
        if (goal.isEmpty()) {
            ui.goalLayout.error = "请先输入任务"
            return
        }
        ui.goalLayout.error = null
        if (RelayAccessibilityService.instance == null) {
            RunLog.append("请先在系统设置里开启 RelayAgent 的无障碍服务。")
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
            return
        }
        // Per-session projection consent (Android 14 requirement).
        pendingGoal = goal
        val mgr = getSystemService(MediaProjectionManager::class.java)
        projectionConsent.launch(mgr.createScreenCaptureIntent())
    }

    private fun launchFlow(goal: String) {
        RunLog.append("▶ $goal")
        OverlayController.show()
        PythonRuntime.runFlow(this, goal, SettingsActivity.loadConfig(this)) { result ->
            runOnUiThread {
                RunLog.append("结果: $result")
                OverlayController.hide()
                ScreenCaptureService.stop(this)
            }
        }
    }

}
