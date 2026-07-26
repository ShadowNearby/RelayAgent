package com.relayagent.app

import android.content.Context
import android.os.Handler
import android.os.Looper
import org.json.JSONObject

/**
 * Process-level holder of the active run's state (same pattern as [ChatStore]
 * / [RunLog]: a plain in-memory singleton that survives activity recreation).
 *
 * MainActivity used to keep pendingGoal / running / the live Working card in
 * instance fields and gate the runFlow completion callback on `!isDestroyed`,
 * so a recreation while a flow was running (rotation, dark-mode switch from
 * Settings, low-memory kill) silently dropped the result, left the Working
 * card spinning forever, and re-enabled the composer for a concurrent second
 * submission. All of that state now lives here: the completion callback and
 * [RunEvents] land on this singleton and mutate [ChatStore] directly, and the
 * currently visible MainActivity is only a renderer that attaches as [Host]
 * on resume and re-reads this object.
 *
 * Threading: every mutation happens on the main thread ([RunEvents] already
 * delivers there; the runFlow completion hops via [main]), so [Host] is
 * always called on the main thread too.
 */
object RunSession {

    /** Render surface implemented by the visible MainActivity. */
    interface Host {
        fun onItemInserted(index: Int)
        fun onItemChanged(index: Int)
        fun onRunStateChanged(running: Boolean)
    }

    private val main = Handler(Looper.getMainLooper())
    private lateinit var appContext: Context
    private var host: Host? = null

    /**
     * True from send until the flow finishes (or consent is denied),
     * including while the projection consent dialog is up. The composer's
     * send/stop toggle keys off this, so a recreated activity cannot start a
     * second flow while one is still in flight (a second submission would let
     * PythonRuntime.runFlow's resetStop() clear a stop request the first flow
     * has not polled yet).
     */
    var running = false
        private set

    /** Goal parked while the per-run projection consent dialog is showing;
     * survives the activity recreation the dialog itself can trigger. */
    private var pendingGoal: String? = null

    /** The live activity card inside [ChatStore.items] for the current run. */
    private var working: ChatItem.Working? = null

    // ------------------------------------------------------------- lifecycle

    fun attach(host: Host, context: Context) {
        appContext = context.applicationContext
        this.host = host
        // Own the event channel unconditionally (and keep it across detach):
        // events must keep mutating the Working card model even while no
        // activity is attached, so a recreated activity re-renders an
        // up-to-date card straight from ChatStore.
        RunEvents.listener = { onRunEvent(it) }
    }

    fun detach(host: Host) {
        if (this.host === host) this.host = null
    }

    // ------------------------------------------------------------- run flow

    /** Start a new exchange: goal bubble + live Working card, composer to
     * Stop. Called right before the projection consent dialog is launched. */
    fun begin(context: Context, goal: String) {
        appContext = context.applicationContext
        pendingGoal = goal
        running = true
        append(ChatItem.User(goal))
        working = ChatItem.Working().also { append(it) }
        RunLog.append("▶ $goal")
        host?.onRunStateChanged(true)
    }

    /** Consume the goal parked across the consent dialog (null when the
     * result is stale — e.g. redelivered after the run already resolved). */
    fun takePendingGoal(): String? = pendingGoal.also { pendingGoal = null }

    /** The consent dialog came back denied/cancelled — unwind the exchange. */
    fun onConsentDenied() {
        val msg = appContext.getString(R.string.notice_consent_denied)
        RunLog.append(msg)
        finishWorking()
        append(ChatItem.Notice(msg))
        running = false
        host?.onRunStateChanged(false)
    }

    /**
     * Kick the flow off. Deliberately captures NO activity reference: the
     * completion callback lands back on this singleton, so a destroyed or
     * recreated MainActivity can never swallow the result.
     */
    fun launchFlow(context: Context, goal: String) {
        val app = context.applicationContext
        appContext = app
        OverlayController.show()
        PythonRuntime.runFlow(app, goal, SettingsActivity.loadConfig(app)) { result ->
            main.post { onFlowFinished(app, result) }
        }
    }

    /** Stop button: signal the run loop and mark the card as stopping. */
    fun requestStop() {
        DeviceBridge.requestStop()
        val msg = appContext.getString(R.string.notice_stop_requested)
        RunLog.append(msg)
        working?.let {
            it.stopping = true
            notifyWorkingChanged()
        }
        append(ChatItem.Notice(msg))
    }

    // --------------------------------------------------------------- results

    private fun onFlowFinished(context: Context, resultJson: String) {
        finishWorking()
        append(parseResult(context, resultJson))
        running = false
        host?.onRunStateChanged(false)
        RunLog.append("结果: ${resultJson.take(400)}")
        OverlayController.hide()
        ScreenCaptureService.stop(context)
    }

    /** Map run_flow's structured JSON result onto a result card. */
    private fun parseResult(context: Context, raw: String): ChatItem {
        val o = try {
            JSONObject(raw)
        } catch (e: Exception) {
            return ChatItem.Answer(false, context.getString(R.string.result_failed), raw.take(600))
        }
        val trajRoot = o.optString("traj_root").takeIf { it.isNotEmpty() }
        if (o.optBoolean("ok")) {
            return ChatItem.Answer(
                true, context.getString(R.string.result_done), summarizeBlackboard(o), trajRoot
            )
        }
        if (o.optBoolean("unsatisfiable")) {
            return ChatItem.Answer(
                false,
                context.getString(R.string.result_unsatisfiable),
                o.optString("reason").ifEmpty { "没有能覆盖这个任务的 App。" },
            )
        }
        val validation = o.optJSONArray("validation_errors")
        if (validation != null && validation.length() > 0) {
            val lines = (0 until validation.length()).joinToString("\n") {
                "· ${validation.optString(it)}"
            }
            return ChatItem.Answer(false, context.getString(R.string.result_failed), lines)
        }
        return ChatItem.Answer(
            false,
            context.getString(R.string.result_failed),
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
                append(ChatItem.Notice(appContext.getString(R.string.notice_waiting_answer)))
            RunEvents.Event.AskAnswered ->
                append(ChatItem.Notice(appContext.getString(R.string.notice_answer_received)))
        }
        notifyWorkingChanged()
    }

    // ---------------------------------------------------------- thread model

    private fun append(item: ChatItem) {
        ChatStore.items.add(item)
        host?.onItemInserted(ChatStore.items.size - 1)
    }

    private fun notifyWorkingChanged() {
        val idx = working?.let { ChatStore.items.indexOf(it) } ?: -1
        if (idx >= 0) host?.onItemChanged(idx)
    }

    private fun finishWorking() {
        working?.let {
            it.running = false
            val idx = ChatStore.items.indexOf(it)
            if (idx >= 0) host?.onItemChanged(idx)
        }
        working = null
    }
}
