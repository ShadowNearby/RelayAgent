package com.relayagent.app

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.media.projection.MediaProjectionManager
import android.os.Bundle
import android.provider.Settings
import android.text.method.ScrollingMovementMethod
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import org.json.JSONObject

/**
 * Minimal frontend: goal box + Run + onboarding state + a log pane.
 * Programmatic UI on purpose — the product surface for now is the runtime,
 * not the chrome; a designed UI lands in Phase 4.
 *
 * Run flow: ensure a11y service enabled -> request MediaProjection consent
 * (per session, Android 14) -> start capture service -> overlay chip ->
 * PythonRuntime.runFlow.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var goalInput: EditText
    private lateinit var statusView: TextView
    private lateinit var logView: TextView
    private var pendingGoal: String? = null

    private val projectionConsent =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
            val goal = pendingGoal ?: return@registerForActivityResult
            pendingGoal = null
            if (result.resultCode != Activity.RESULT_OK || result.data == null) {
                appendLog("屏幕采集授权被拒绝，无法运行。")
                return@registerForActivityResult
            }
            ScreenCaptureService.start(this, result.resultCode, result.data!!)
            launchFlow(goal)
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(32, 32, 32, 32)
        }
        statusView = TextView(this)
        goalInput = EditText(this).apply {
            hint = "用一句话描述任务，例如：帮我找一台适合学生的平板电脑，预算2000以内"
            minLines = 2
        }
        val runBtn = Button(this).apply {
            text = "运行"
            setOnClickListener { onRunClicked() }
        }
        val stopBtn = Button(this).apply {
            text = "停止"
            setOnClickListener { DeviceBridge.requestStop(); appendLog("已请求停止。") }
        }
        val settingsBtn = Button(this).apply {
            text = "设置（LLM 网关）"
            setOnClickListener {
                startActivity(Intent(this@MainActivity, SettingsActivity::class.java))
            }
        }
        val a11yBtn = Button(this).apply {
            text = "打开无障碍设置"
            setOnClickListener {
                startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
            }
        }
        logView = TextView(this).apply {
            movementMethod = ScrollingMovementMethod()
            textSize = 12f
        }

        root.addView(statusView)
        root.addView(goalInput)
        root.addView(runBtn)
        root.addView(stopBtn)
        root.addView(settingsBtn)
        root.addView(a11yBtn)
        root.addView(ScrollView(this).apply { addView(logView) })
        setContentView(root)
    }

    override fun onResume() {
        super.onResume()
        refreshStatus()
    }

    private fun refreshStatus() {
        val a11yOn = RelayAccessibilityService.instance != null
        val cfg = SettingsActivity.loadConfig(this)
        val gatewaySet = cfg.optString("LLM_BASE_URL").isNotEmpty()
        statusView.text = buildString {
            append(if (a11yOn) "✅ 无障碍服务已连接" else "❌ 无障碍服务未开启（必须）")
            append('\n')
            append(if (gatewaySet) "✅ LLM 网关已配置" else "❌ LLM 网关未配置（设置里填）")
        }
    }

    private fun onRunClicked() {
        val goal = goalInput.text.toString().trim()
        if (goal.isEmpty()) {
            appendLog("请先输入任务。")
            return
        }
        if (RelayAccessibilityService.instance == null) {
            appendLog("请先在系统设置里开启 RelayAgent 的无障碍服务。")
            return
        }
        // Per-session projection consent (Android 14 requirement).
        pendingGoal = goal
        val mgr = getSystemService(MediaProjectionManager::class.java)
        projectionConsent.launch(mgr.createScreenCaptureIntent())
    }

    private fun launchFlow(goal: String) {
        appendLog("▶ $goal")
        OverlayController.show()
        PythonRuntime.runFlow(this, goal, SettingsActivity.loadConfig(this)) { result ->
            runOnUiThread {
                appendLog("结果: $result")
                OverlayController.hide()
                ScreenCaptureService.stop(this)
            }
        }
    }

    private fun appendLog(line: String) {
        logView.append(line + "\n")
    }
}
