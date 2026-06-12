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
import java.util.concurrent.CountDownLatch
import java.util.concurrent.atomic.AtomicReference

/**
 * Floating status chip + ask_user panel, attached to the accessibility
 * service's window (TYPE_ACCESSIBILITY_OVERLAY — no extra permission, and it
 * stays visible while the agent drives other apps in the foreground).
 *
 * - postStatus(json): updates the chip with the current step / leg.
 * - askUserBlocking(text): expands an answer panel; blocks the calling
 *   (Python worker) thread until 回答 (answer) or 接管 (take over -> null).
 * - The 停止 button flips DeviceBridge.shouldStop, polled at loop boundaries.
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
            setPadding(28, 14, 28, 14)
            setTextColor(Color.WHITE)
            setBackgroundColor(0xCC222222.toInt())
            textSize = 12f
            setOnClickListener { DeviceBridge.requestStop() }
        }
        wm.addView(view, chipLayoutParams())
        chip = view
    }

    fun hide() = main.post {
        val service = RelayAccessibilityService.instance ?: return@post
        chip?.let { service.getSystemService(WindowManager::class.java).removeView(it) }
        chip = null
    }

    fun postStatus(json: String) = main.post {
        // Minimal rendering for now: show the raw event's interesting bits.
        // A structured chip (step n / leg id / thought) lands with Phase 2 UX.
        chip?.text = "RelayAgent ▶ ${json.take(120)}（点按停止）"
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
        main.post {
            val wm = service.getSystemService(WindowManager::class.java)
            val panel = LinearLayout(service).apply {
                orientation = LinearLayout.VERTICAL
                setBackgroundColor(0xEE333333.toInt())
                setPadding(32, 24, 32, 24)
            }
            val label = TextView(service).apply {
                setTextColor(Color.WHITE)
                this.text = text
            }
            val input = EditText(service).apply {
                setTextColor(Color.WHITE)
                hint = "输入回答…"
            }
            val buttons = LinearLayout(service).apply {
                orientation = LinearLayout.HORIZONTAL
            }
            fun finish(value: String?) {
                answer.set(value)
                wm.removeView(panel)
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
            wm.addView(panel, panelLayoutParams())
        }
        latch.await()
        return answer.get()
    }

    private fun chipLayoutParams() = WindowManager.LayoutParams(
        WindowManager.LayoutParams.WRAP_CONTENT,
        WindowManager.LayoutParams.WRAP_CONTENT,
        WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
        WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE,
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
