package com.relayagent.app

import android.app.Activity
import android.content.Intent
import android.media.projection.MediaProjectionManager
import android.os.Bundle
import android.provider.Settings
import android.view.View
import android.widget.LinearLayout
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.recyclerview.widget.LinearLayoutManager
import com.google.android.material.button.MaterialButton
import com.relayagent.app.databinding.ActivityMainBinding
import org.json.JSONObject

/**
 * Conversation-style home (Codex/Claude-app-like).
 *
 * A task is one exchange in a thread: the goal renders as an outgoing bubble,
 * the run as a live activity card (subtask rows + current step, fed by
 * [RunEvents]), and the outcome as a result card with a "view details" jump
 * into the trajectory viewer. The composer is pinned to the bottom; while a
 * run is active its send button becomes Stop.
 *
 * Run flow is unchanged underneath: ensure a11y service → per-session
 * MediaProjection consent (Android 14) → capture service + overlay chip →
 * PythonRuntime.runFlow.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var ui: ActivityMainBinding
    private lateinit var adapter: ChatAdapter
    private var pendingGoal: String? = null
    private var running = false
    private var working: ChatItem.Working? = null

    private val projectionConsent =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
            val goal = pendingGoal ?: return@registerForActivityResult
            pendingGoal = null
            if (result.resultCode != Activity.RESULT_OK || result.data == null) {
                RunLog.append(getString(R.string.notice_consent_denied))
                finishWorking()
                appendItem(ChatItem.Notice(getString(R.string.notice_consent_denied)))
                setRunning(false)
                return@registerForActivityResult
            }
            ScreenCaptureService.start(this, result.resultCode, result.data!!)
            launchFlow(goal)
        }

    private val pickExample =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
            val instruction = result.data?.getStringExtra(ExamplesActivity.EXTRA_INSTRUCTION)
            if (result.resultCode == Activity.RESULT_OK && !instruction.isNullOrBlank()) {
                ui.composerInput.setText(instruction)
                ui.composerInput.setSelection(instruction.length)
            }
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        ui = ActivityMainBinding.inflate(layoutInflater)
        setContentView(ui.root)

        ui.toolbar.inflateMenu(R.menu.main)
        ui.toolbar.setOnMenuItemClickListener {
            when (it.itemId) {
                R.id.action_history -> {
                    startActivity(Intent(this, LogActivity::class.java)); true
                }
                R.id.action_examples -> {
                    pickExample.launch(Intent(this, ExamplesActivity::class.java)); true
                }
                R.id.action_settings -> {
                    startActivity(Intent(this, SettingsActivity::class.java)); true
                }
                R.id.action_open_a11y -> {
                    startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)); true
                }
                else -> false
            }
        }

        adapter = ChatAdapter(ChatStore.items)
        ui.chatList.layoutManager = LinearLayoutManager(this).apply { stackFromEnd = true }
        ui.chatList.adapter = adapter

        ui.sendBtn.setOnClickListener { if (running) onStopClicked() else onSendClicked() }
        buildExampleChips()
        refreshEmptyState()
    }

    override fun onResume() {
        super.onResume()
        refreshSetupBanner()
        RunEvents.listener = { onRunEvent(it) }
        adapter.notifyDataSetChanged()
        scrollToBottom()
    }

    override fun onPause() {
        super.onPause()
        RunEvents.listener = null
    }

    // ------------------------------------------------------------- composing

    private fun onSendClicked() {
        val goal = ui.composerInput.text?.toString()?.trim().orEmpty()
        if (goal.isEmpty()) return
        if (RelayAccessibilityService.instance == null) {
            appendItem(ChatItem.Notice(getString(R.string.status_a11y_off)))
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
            return
        }
        ui.composerInput.setText("")
        appendItem(ChatItem.User(goal))
        working = ChatItem.Working().also { appendItem(it) }
        setRunning(true)
        RunLog.append("▶ $goal")

        // Per-session projection consent (Android 14 requirement).
        pendingGoal = goal
        val mgr = getSystemService(MediaProjectionManager::class.java)
        projectionConsent.launch(mgr.createScreenCaptureIntent())
    }

    private fun onStopClicked() {
        DeviceBridge.requestStop()
        RunLog.append(getString(R.string.notice_stop_requested))
        working?.let {
            it.stopping = true
            notifyWorkingChanged()
        }
        appendItem(ChatItem.Notice(getString(R.string.notice_stop_requested)))
    }

    private fun launchFlow(goal: String) {
        OverlayController.show()
        PythonRuntime.runFlow(this, goal, SettingsActivity.loadConfig(this)) { result ->
            runOnUiThread {
                if (!isDestroyed) onRunFinished(result)
                OverlayController.hide()
                ScreenCaptureService.stop(this)
            }
        }
    }

    // --------------------------------------------------------------- results

    private fun onRunFinished(resultJson: String) {
        finishWorking()
        appendItem(parseResult(resultJson))
        setRunning(false)
        RunLog.append("结果: ${resultJson.take(400)}")
    }

    /** Map run_flow's structured JSON result onto a result card. */
    private fun parseResult(raw: String): ChatItem {
        val o = try {
            JSONObject(raw)
        } catch (e: Exception) {
            return ChatItem.Answer(false, getString(R.string.result_failed), raw.take(600))
        }
        val trajRoot = o.optString("traj_root").takeIf { it.isNotEmpty() }
        if (o.optBoolean("ok")) {
            return ChatItem.Answer(
                true, getString(R.string.result_done), summarizeBlackboard(o), trajRoot
            )
        }
        if (o.optBoolean("unsatisfiable")) {
            return ChatItem.Answer(
                false,
                getString(R.string.result_unsatisfiable),
                o.optString("reason").ifEmpty { "没有能覆盖这个任务的 App。" },
            )
        }
        val validation = o.optJSONArray("validation_errors")
        if (validation != null && validation.length() > 0) {
            val lines = (0 until validation.length()).joinToString("\n") {
                "· ${validation.optString(it)}"
            }
            return ChatItem.Answer(false, getString(R.string.result_failed), lines)
        }
        return ChatItem.Answer(
            false,
            getString(R.string.result_failed),
            o.optString("error").ifEmpty { raw.take(600) },
            trajRoot,
        )
    }

    /** Human summary of the final blackboard: captured replies / user picks. */
    private fun summarizeBlackboard(result: JSONObject): String {
        val bb = result.optJSONObject("blackboard") ?: return "已执行完毕。"
        val parts = mutableListOf<String>()
        for (key in bb.keys()) {
            val value = bb.opt(key) ?: continue
            val text = value.toString().trim()
            if (text.isEmpty() || text == "null") continue
            parts.add(if (bb.length() == 1) truncate(text) else "$key：${truncate(text)}")
        }
        return if (parts.isEmpty()) "已执行完毕。" else parts.joinToString("\n\n")
    }

    private fun truncate(s: String, n: Int = 800): String =
        if (s.length <= n) s else s.take(n) + "…"

    // ---------------------------------------------------------- live events

    private fun onRunEvent(event: RunEvents.Event) {
        val w = working ?: return
        when (event) {
            is RunEvents.Event.LegStart -> {
                val label = event.app?.let { AppLabels.label(it) } ?: event.id
                w.legs.add(ChatItem.Working.LegRow(event.id, label))
                w.stepLine = null
            }
            is RunEvents.Event.Step -> {
                w.stepLine = "步骤 ${event.step} · ${event.actionType}" +
                    (if (event.thought.isNotEmpty()) " · ${event.thought.take(60)}" else "")
            }
            is RunEvents.Event.LegEnd -> {
                w.legs.lastOrNull { it.id == event.id }?.done = true
                w.stepLine = null
            }
            RunEvents.Event.AskUser ->
                appendItem(ChatItem.Notice(getString(R.string.notice_waiting_answer)))
            RunEvents.Event.AskAnswered ->
                appendItem(ChatItem.Notice(getString(R.string.notice_answer_received)))
        }
        notifyWorkingChanged()
    }

    // ------------------------------------------------------------- UI state

    private fun appendItem(item: ChatItem) {
        ChatStore.items.add(item)
        adapter.notifyItemInserted(ChatStore.items.size - 1)
        refreshEmptyState()
        scrollToBottom()
    }

    private fun notifyWorkingChanged() {
        val idx = working?.let { ChatStore.items.indexOf(it) } ?: -1
        if (idx >= 0) adapter.notifyItemChanged(idx)
        scrollToBottom()
    }

    private fun finishWorking() {
        working?.let {
            it.running = false
            val idx = ChatStore.items.indexOf(it)
            if (idx >= 0) adapter.notifyItemChanged(idx)
        }
        working = null
    }

    private fun setRunning(value: Boolean) {
        running = value
        ui.sendBtn.setIconResource(
            if (value) android.R.drawable.ic_media_pause else android.R.drawable.ic_menu_send
        )
        ui.composerInput.isEnabled = !value
        if (!value) ui.composerInput.requestFocus()
    }

    private fun scrollToBottom() {
        if (ChatStore.items.isNotEmpty()) {
            ui.chatList.post { ui.chatList.scrollToPosition(ChatStore.items.size - 1) }
        }
    }

    private fun refreshEmptyState() {
        ui.emptyState.visibility =
            if (ChatStore.items.isEmpty()) View.VISIBLE else View.GONE
    }

    private fun refreshSetupBanner() {
        val a11yOn = RelayAccessibilityService.instance != null
        val gatewaySet =
            SettingsActivity.loadConfig(this).optString("LLM_BASE_URL").isNotEmpty()
        when {
            !a11yOn -> showBanner(getString(R.string.status_a11y_off)) {
                startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
            }
            !gatewaySet -> showBanner(getString(R.string.status_gateway_off)) {
                startActivity(Intent(this, SettingsActivity::class.java))
            }
            else -> ui.setupBanner.visibility = View.GONE
        }
    }

    private fun showBanner(text: String, onFix: () -> Unit) {
        ui.setupBanner.visibility = View.VISIBLE
        ui.setupText.text = text
        ui.setupFixBtn.setOnClickListener { onFix() }
    }

    private fun buildExampleChips() {
        ui.exampleChips.removeAllViews()
        for (suggestion in exampleSuggestions()) {
            val btn = MaterialButton(
                this, null, com.google.android.material.R.attr.materialButtonOutlinedStyle
            ).apply {
                text = suggestion
                isAllCaps = false
                maxLines = 1
                ellipsize = android.text.TextUtils.TruncateAt.END
                setOnClickListener {
                    ui.composerInput.setText(suggestion)
                    ui.composerInput.setSelection(suggestion.length)
                }
            }
            ui.exampleChips.addView(
                btn,
                LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT,
                ),
            )
        }
    }

    /** A few easy bundled examples for the empty-state suggestions. */
    private fun exampleSuggestions(n: Int = 3): List<String> = try {
        val raw = resources.openRawResource(R.raw.examples)
            .bufferedReader().use { it.readText() }
        val arr = JSONObject(raw).getJSONArray("examples")
        (0 until arr.length()).asSequence()
            .mapNotNull { arr.optJSONObject(it) }
            .filter { it.optString("difficulty") == "easy" }
            .map { it.optString("instruction") }
            .filter { it.isNotBlank() }
            .distinct()
            .take(n)
            .toList()
    } catch (e: Exception) {
        emptyList()
    }
}
