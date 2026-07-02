package com.relayagent.app

import android.annotation.SuppressLint
import android.graphics.Color
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.Gravity
import android.view.WindowManager
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import org.json.JSONObject
import java.util.concurrent.CountDownLatch
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
            text = "RelayAgent 待命"
            setTextColor(Color.WHITE)
            setBackgroundResource(R.drawable.bg_overlay_chip)
            textSize = 12f
            // NOT clickable on purpose: the chip window is FLAG_NOT_TOUCHABLE so
            // the agent's injected gestures (dispatchGesture) pass THROUGH it to
            // the app underneath. A touchable chip sitting on top would steal
            // those taps — a tap landing on it used to fire requestStop and end
            // the run silently. Stop now lives on the capture notification.
        }
        wm.addView(view, chipLayoutParams())
        chip = view
    }

    fun hide() = main.post {
        val service = RelayAccessibilityService.instance ?: return@post
        chip?.let { service.getSystemService(WindowManager::class.java).removeView(it) }
        chip = null
    }

    fun postStatus(json: String) {
        val line = humanize(json)
        // Mirror every status event into the live log tail (MainActivity pane).
        RunLog.append(line)
        main.post { chip?.text = "RelayAgent ▶ $line" }
    }

    /** Turn a status event JSON into a short human line for the chip + log. */
    private fun humanize(json: String): String = try {
        val o = JSONObject(json)
        when (o.optString("event")) {
            "leg_start" -> {
                val app = o.optString("app").takeIf { it.isNotEmpty() }
                "▷ 子任务 ${o.optString("id")}" + (if (app != null) "：$app" else "")
            }
            "leg_end" -> "✓ 子任务 ${o.optString("id")} 完成"
            "step" -> {
                val thought = o.optString("thought").replace("\n", " ").take(80)
                "步骤 ${o.optInt("step")} · ${o.optString("action_type")}" +
                    (if (thought.isNotEmpty()) " · $thought" else "")
            }
            else -> json.take(120)
        }
    } catch (e: Exception) {
        json.take(120)
    }

    /**
     * Blocking ask_user panel. Must NOT be called on the main thread (it is
     * called from the Python worker; enforced by the latch pattern).
     */
    fun askUserBlocking(text: String): String? {
        val service = RelayAccessibilityService.instance ?: run {
            Log.w(TAG, "ask_user: a11y service not connected -> take-over")
            return null
        }
        val latch = CountDownLatch(1)
        val answer = AtomicReference<String?>(null)
        val done = java.util.concurrent.atomic.AtomicBoolean(false)
        main.post {
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
                hint = "输入回答…"
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
            fun finish(value: String?) {
                // Guard against double-taps: a second removeView on the same
                // panel throws and the latch must count down exactly once.
                if (!done.compareAndSet(false, true)) return
                answer.set(value)
                try {
                    wm.removeView(panel)
                } catch (e: Exception) {
                    Log.w(TAG, "ask_user: removeView failed: $e")
                }
                latch.countDown()
            }
            buttons.addView(Button(service).apply {
                this.text = "回答"
                setOnClickListener { finish(input.text.toString()) }
            })
            buttons.addView(Button(service).apply {
                // Take-over: maps to the EOF/None handoff terminal — the user
                // finishes the task by hand and the run ends as a success.
                this.text = "我来接管"
                setOnClickListener { finish(null) }
            })
            panel.addView(label)
            panel.addView(input)
            panel.addView(buttons)
            try {
                wm.addView(panel, panelLayoutParams())
            } catch (e: Exception) {
                // If the panel can't be shown the latch would never release
                // and the Python worker would hang forever — treat as take-over.
                Log.w(TAG, "ask_user: addView failed -> take-over: $e")
                if (done.compareAndSet(false, true)) latch.countDown()
            }
        }
        latch.await()
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
