package com.relayagent.app

import android.os.Handler
import android.os.Looper
import org.json.JSONObject

/**
 * Typed event channel from the running flow to the conversation UI.
 *
 * The Python runtime emits status events (`emit_status`) as JSON through
 * [OverlayController.postStatus]; that path keeps feeding the overlay chip and
 * the RunLog text tail, and ALSO dispatches here so MainActivity's task thread
 * can render a live activity card (per-subtask rows + current step) the way
 * the Codex/Claude apps show agent progress.
 *
 * Delivery is always on the main thread. A single listener (the visible
 * MainActivity) is enough — missed events while the activity is backgrounded
 * only degrade the live card; the final result still arrives via the
 * runFlow completion callback.
 */
object RunEvents {

    sealed class Event {
        data class LegStart(val id: String, val app: String?) : Event()
        data class Step(val step: Int, val actionType: String, val thought: String) : Event()
        data class LegEnd(val id: String) : Event()
        /** The agent is blocked on the overlay ask_user panel. */
        object AskUser : Event()
        /** The user answered the overlay panel (not take-over) — run resumes. */
        object AskAnswered : Event()
    }

    private val main = Handler(Looper.getMainLooper())

    @Volatile
    var listener: ((Event) -> Unit)? = null

    fun post(event: Event) {
        main.post { listener?.invoke(event) }
    }

    /** Parse one emit_status JSON payload; unknown events are ignored. */
    fun dispatch(json: String) {
        val event = try {
            val o = JSONObject(json)
            when (o.optString("event")) {
                "leg_start" -> Event.LegStart(
                    id = o.optString("id"),
                    app = o.optString("app").takeIf { it.isNotEmpty() },
                )
                "leg_end" -> Event.LegEnd(o.optString("id"))
                "step" -> Event.Step(
                    step = o.optInt("step"),
                    actionType = o.optString("action_type"),
                    thought = o.optString("thought").replace("\n", " "),
                )
                else -> null
            }
        } catch (e: Exception) {
            null
        }
        if (event != null) post(event)
    }
}
