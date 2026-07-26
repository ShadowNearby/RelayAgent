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
 *
 * All run state (pending goal, running flag, live Working card) lives in
 * [RunSession], which survives activity recreation the way [ChatStore] does;
 * this activity only renders. On resume it re-reads both singletons, so a
 * rotation / theme-switch / low-memory recreation mid-run comes back with the
 * live card still spinning and the composer still in its Stop state, and a
 * result that arrived while no instance was attached is already in the thread.
 */
class MainActivity : AppCompatActivity(), RunSession.Host {

    private lateinit var ui: ActivityMainBinding
    private lateinit var adapter: ChatAdapter

    private val projectionConsent =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
            // The consent dialog can outlive this activity instance (the
            // recreation is delivered to the NEW instance's callback); the
            // parked goal lives in RunSession, not on the old instance.
            val goal = RunSession.takePendingGoal() ?: return@registerForActivityResult
            if (result.resultCode != Activity.RESULT_OK || result.data == null) {
                RunSession.onConsentDenied()
                return@registerForActivityResult
            }
            ScreenCaptureService.start(this, result.resultCode, result.data!!)
            RunSession.launchFlow(applicationContext, goal)
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

        ui.sendBtn.setOnClickListener {
            if (RunSession.running) onStopClicked() else onSendClicked()
        }
        buildExampleChips()
        refreshEmptyState()
    }

    override fun onResume() {
        super.onResume()
        refreshSetupBanner()
        RunSession.attach(this, applicationContext)
        // Re-render everything from the surviving singletons: the thread
        // (including a Working card mid-run or a result card that landed while
        // no instance was attached) and the composer's send/stop state.
        adapter.notifyDataSetChanged()
        refreshEmptyState()
        renderRunning(RunSession.running)
        scrollToBottom()
    }

    override fun onDestroy() {
        super.onDestroy()
        // Detach at destroy, not pause: a paused-but-alive instance (consent
        // dialog or another activity on top) must keep receiving adapter
        // notifications — its RecyclerView still reads ChatStore.items, and
        // mutating that list without notifying it is an inconsistency crash.
        // The identity guard keeps a stale destroy from kicking out an
        // instance that attached after us; on plain recreation the successor
        // re-attaches (and re-renders) in its own onResume.
        RunSession.detach(this)
    }

    // ------------------------------------------------------------- composing

    private fun onSendClicked() {
        val goal = ui.composerInput.text?.toString()?.trim().orEmpty()
        if (goal.isEmpty() || RunSession.running) return
        if (RelayAccessibilityService.instance == null) {
            appendItem(ChatItem.Notice(getString(R.string.status_a11y_off)))
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
            return
        }
        ui.composerInput.setText("")
        RunSession.begin(applicationContext, goal)

        // Per-session projection consent (Android 14 requirement).
        val mgr = getSystemService(MediaProjectionManager::class.java)
        projectionConsent.launch(mgr.createScreenCaptureIntent())
    }

    private fun onStopClicked() {
        RunSession.requestStop()
    }

    // ------------------------------------------------------ RunSession.Host

    override fun onItemInserted(index: Int) {
        adapter.notifyItemInserted(index)
        refreshEmptyState()
        scrollToBottom()
    }

    override fun onItemChanged(index: Int) {
        adapter.notifyItemChanged(index)
        scrollToBottom()
    }

    override fun onRunStateChanged(running: Boolean) {
        renderRunning(running)
        if (!running) ui.composerInput.requestFocus()
    }

    // ------------------------------------------------------------- UI state

    /** Activity-local notices (e.g. a11y off) that are not part of a run. */
    private fun appendItem(item: ChatItem) {
        ChatStore.items.add(item)
        adapter.notifyItemInserted(ChatStore.items.size - 1)
        refreshEmptyState()
        scrollToBottom()
    }

    /** Composer send/stop rendering. Pure view state — [RunSession] owns the
     * running flag itself. */
    private fun renderRunning(value: Boolean) {
        ui.sendBtn.setIconResource(
            if (value) android.R.drawable.ic_media_pause else android.R.drawable.ic_menu_send
        )
        ui.composerInput.isEnabled = !value
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
