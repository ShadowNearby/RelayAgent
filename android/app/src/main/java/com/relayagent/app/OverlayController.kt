package com.relayagent.app

import android.annotation.SuppressLint
import android.graphics.Color
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import android.view.Gravity
import android.view.WindowManager
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import org.json.JSONObject
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference

/**
 * Floating status chip + ask_user panel, attached to the accessibility
 * service's window (TYPE_ACCESSIBILITY_OVERLAY — no extra permission, and it
 * stays visible while the agent drives other apps in the foreground).
 *
 * - postStatus(json): updates the chip with the current step / leg. The chip
 *   window is FLAG_NOT_TOUCHABLE so the agent's injected gestures pass through
 *   it; Stop lives on the capture notification, not the chip.
 * - askUserBlocking(text): expands an answer panel; blocks the calling
 *   (Python worker) thread until 回答 (answer) or 接管 (take over -> null). This
 *   panel IS touchable, but only shows while the agent waits for input (not
 *   while it is dispatching gestures), so it never steals a tap.
 */
object OverlayController {

    private const val TAG = "RelayOverlay"
    private val main = Handler(Looper.getMainLooper())

    /** Upper bound for one askUserBlocking wait. The host terminal input()
     * has no timeout, but an unattended phone must not park the single
     * Python worker forever — on expiry the panel resolves to null (EOF /
     * take-over = handoff-success terminal). Mutable so a settings hook or
     * test can tune it without a rebuild. */
    @Volatile
    var askTimeoutSeconds: Long = 600

    /** Latch poll period: how quickly a stop request / service death is
     * noticed while the worker waits on the ask_user panel. */
    private const val ASK_POLL_MS = 250L

    private var chip: TextView? = null

    @SuppressLint("SetTextI18n")
    fun show() = main.post {
        val service = RelayAccessibilityService.instance ?: run {
            Log.w(TAG, "overlay: a11y service not connected")
            return@post
        }
        if (chip != null) return@post
        val wm = service.getSystemService(WindowManager::class.java)
        val view = TextView(service).apply {
            text = service.getString(R.string.overlay_idle)
            setTextColor(Color.WHITE)
            setBackgroundResource(R.drawable.bg_overlay_chip)
            textSize = 12f
            // NOT clickable on purpose: the chip window is FLAG_NOT_TOUCHABLE so
            // the agent's injected gestures (dispatchGesture) pass THROUGH it to
            // the app underneath. A touchable chip sitting on top would steal
            // those taps — a tap landing on it used to fire requestStop and end
            // the run silently. Stop now lives on the capture notification.
        }
        try {
            wm.addView(view, chipLayoutParams())
            chip = view
        } catch (e: Exception) {
            // e.g. the service is mid-teardown; the run continues without a chip.
            Log.w(TAG, "overlay: chip addView failed: $e")
        }
    }

    fun hide() = main.post {
        val view = chip ?: return@post
        // Clear the field unconditionally BEFORE any early exit: a stale
        // non-null chip would make every future show() a no-op (the guard
        // above) while holding a view of a possibly-destroyed service.
        chip = null
        val service = RelayAccessibilityService.instance ?: run {
            // Service gone — the system already removed its windows with it.
            Log.w(TAG, "overlay: a11y service gone; chip window already removed")
            return@post
        }
        try {
            service.getSystemService(WindowManager::class.java).removeView(view)
        } catch (e: Exception) {
            // The view can already be detached (service toggled off/on mid-run
            // removes its windows); removeView then throws — never crash here.
            Log.w(TAG, "overlay: chip removeView failed: $e")
        }
    }

    fun postStatus(json: String) {
        val line = humanize(json)
        // Mirror every status event into the live log tail, the overlay chip,
        // AND the typed channel the conversation UI renders its live activity
        // card from (RunEvents parses the same JSON).
        RunLog.append(line)
        RunEvents.dispatch(json)
        main.post { chip?.text = "RelayAgent ▶ $line" }
    }

    /** Turn a status event JSON into a short human line for the chip + log. */
    private fun humanize(json: String): String = try {
        val ctx = RelayAccessibilityService.instance
        val o = JSONObject(json)
        when (o.optString("event")) {
            "leg_start" -> {
                val app = o.optString("app").takeIf { it.isNotEmpty() }
                val id = o.optString("id")
                when {
                    ctx == null -> "▷ $id" + (if (app != null) ": $app" else "")
                    app != null -> ctx.getString(R.string.overlay_leg_start_app, id, app)
                    else -> ctx.getString(R.string.overlay_leg_start, id)
                }
            }
            "leg_end" -> ctx?.getString(R.string.overlay_leg_end, o.optString("id"))
                ?: "✓ ${o.optString("id")}"
            "step" -> {
                val thought = o.optString("thought").replace("\n", " ").take(80)
                val head = ctx?.getString(
                    R.string.overlay_step_status, o.optInt("step"), o.optString("action_type")
                ) ?: "${o.optInt("step")} · ${o.optString("action_type")}"
                head + (if (thought.isNotEmpty()) " · $thought" else "")
            }
            else -> json.take(120)
        }
    } catch (e: Exception) {
        json.take(120)
    }

    /**
     * Blocking ask_user panel. Must NOT be called on the main thread (it is
     * called from the Python worker; enforced by the latch pattern).
     *
     * The wait is bounded: the worker polls the latch instead of parking
     * forever, so it also unblocks on (a) a stop request — the App/notification
     * 停止 only flips DeviceBridge's flag, and the loop-boundary polls can
     * never run while the single Python worker is parked here — (b) the
     * accessibility service dying (the system removes the panel window with
     * it, so no tap can ever land), or (c) [askTimeoutSeconds] elapsing.
     * All three resolve to null = the host InteractionProvider's EOF
     * semantics, which the runtime treats as the handoff-success terminal.
     */
    fun askUserBlocking(text: String): String? {
        val service = RelayAccessibilityService.instance ?: run {
            Log.w(TAG, "ask_user: a11y service not connected -> take-over")
            return null
        }
        val latch = CountDownLatch(1)
        val answer = AtomicReference<String?>(null)
        val done = AtomicBoolean(false)
        val panelRef = AtomicReference<LinearLayout?>(null)

        // Single terminal for every exit path (button tap, stop request,
        // service death, timeout): first caller wins, the panel is removed on
        // the main thread, and the latch counts down exactly once.
        fun conclude(value: String?, why: String) {
            if (!done.compareAndSet(false, true)) return
            answer.set(value)
            main.post {
                panelRef.getAndSet(null)?.let { panel ->
                    try {
                        service.getSystemService(WindowManager::class.java).removeView(panel)
                    } catch (e: Exception) {
                        Log.w(TAG, "ask_user: removeView failed ($why): $e")
                    }
                }
            }
            latch.countDown()
        }

        main.post {
            if (done.get()) return@post // stop/timeout raced ahead of the panel
            val wm = service.getSystemService(WindowManager::class.java)
            val panel = LinearLayout(service).apply {
                orientation = LinearLayout.VERTICAL
                setBackgroundResource(R.drawable.bg_overlay_panel)
                setPadding(44, 36, 44, 36)
            }
            val label = TextView(service).apply {
                setTextColor(Color.WHITE)
                textSize = 15f
                this.text = text
            }
            val input = EditText(service).apply {
                setTextColor(Color.WHITE)
                setHintTextColor(0x99FFFFFF.toInt())
                setBackgroundResource(R.drawable.bg_overlay_input)
                hint = service.getString(R.string.overlay_input_hint)
                val lp = LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT,
                ).apply { topMargin = 20; bottomMargin = 20 }
                layoutParams = lp
            }
            val buttons = LinearLayout(service).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.END
            }
            buttons.addView(Button(service).apply {
                this.text = service.getString(R.string.overlay_answer)
                setOnClickListener { conclude(input.text.toString(), "answer") }
            })
            buttons.addView(Button(service).apply {
                // Take-over: maps to the EOF/None handoff terminal — the user
                // finishes the task by hand and the run ends as a success.
                this.text = service.getString(R.string.overlay_takeover)
                setOnClickListener { conclude(null, "take-over") }
            })
            panel.addView(label)
            panel.addView(input)
            panel.addView(buttons)
            try {
                wm.addView(panel, panelLayoutParams())
                panelRef.set(panel)
            } catch (e: Exception) {
                // If the panel can't be shown the latch would never release
                // and the Python worker would hang forever — treat as take-over.
                Log.w(TAG, "ask_user: addView failed -> take-over: $e")
                conclude(null, "addView failed")
            }
        }

        val deadline = SystemClock.elapsedRealtime() + askTimeoutSeconds * 1000
        while (!latch.await(ASK_POLL_MS, TimeUnit.MILLISECONDS)) {
            when {
                DeviceBridge.shouldStop() -> {
                    Log.i(TAG, "ask_user: stop requested -> unblock as take-over")
                    conclude(null, "stop requested")
                }
                RelayAccessibilityService.instance !== service -> {
                    Log.w(TAG, "ask_user: a11y service died -> unblock as take-over")
                    conclude(null, "service died")
                }
                SystemClock.elapsedRealtime() >= deadline -> {
                    Log.w(TAG, "ask_user: no answer within ${askTimeoutSeconds}s -> take-over")
                    conclude(null, "timeout")
                }
            }
        }
        return answer.get()
    }

    private fun chipLayoutParams() = WindowManager.LayoutParams(
        WindowManager.LayoutParams.WRAP_CONTENT,
        WindowManager.LayoutParams.WRAP_CONTENT,
        WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
        // NOT_TOUCHABLE is the critical flag: the agent injects taps via
        // dispatchGesture, and an overlay that consumes touches at those screen
        // coords would steal them (and could trip its own click handler). Keep
        // the chip purely informational so every gesture reaches the app below.
        WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
            WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE,
        android.graphics.PixelFormat.TRANSLUCENT,
    ).apply {
        gravity = Gravity.TOP or Gravity.END
        y = 120
    }

    private fun panelLayoutParams() = WindowManager.LayoutParams(
        WindowManager.LayoutParams.MATCH_PARENT,
        WindowManager.LayoutParams.WRAP_CONTENT,
        WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
        0, // focusable: the EditText needs the IME
        android.graphics.PixelFormat.TRANSLUCENT,
    ).apply {
        gravity = Gravity.BOTTOM
    }
}
