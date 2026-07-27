package com.relayagent.app

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.ClipData
import android.content.ClipboardManager
import android.graphics.Bitmap
import android.graphics.Path
import android.os.Bundle
import android.util.Log
import android.view.Display
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityWindowInfo
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * The device-driving accessibility service: gesture injection, global keys,
 * focused-field text input, UI-tree access and (degraded-mode) screenshots.
 *
 * All methods are synchronous and called from the Python worker thread via
 * [DeviceBridge]; gesture dispatch blocks on the completion callback so the
 * caller's post-action settle timing matches the host adb semantics
 * (`adb shell input` is also synchronous).
 */
class RelayAccessibilityService : AccessibilityService() {

    companion object {
        private const val TAG = "RelayA11y"

        @Volatile
        var instance: RelayAccessibilityService? = null
            private set

        /** Generous upper bound for dispatchGesture callbacks. */
        private const val GESTURE_EXTRA_TIMEOUT_MS = 2000L
    }

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
        Log.i(TAG, "accessibility service connected")
    }

    override fun onDestroy() {
        if (instance === this) instance = null
        super.onDestroy()
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        // Pull model: the runtime polls dumps/screenshots; no event handling.
    }

    override fun onInterrupt() {}

    // -- gestures ------------------------------------------------------------

    private fun dispatchPath(path: Path, durationMs: Long): Boolean {
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, durationMs))
            .build()
        val latch = CountDownLatch(1)
        var ok = false
        val dispatched = dispatchGesture(gesture, object : GestureResultCallback() {
            override fun onCompleted(g: GestureDescription?) {
                ok = true
                latch.countDown()
            }

            override fun onCancelled(g: GestureDescription?) {
                latch.countDown()
            }
        }, null)
        if (!dispatched) {
            Log.w(TAG, "dispatchGesture refused")
            return false
        }
        latch.await(durationMs + GESTURE_EXTRA_TIMEOUT_MS, TimeUnit.MILLISECONDS)
        if (!ok) Log.w(TAG, "gesture cancelled (durationMs=$durationMs)")
        return ok
    }

    fun tap(x: Int, y: Int, durationMs: Long = 50): Boolean {
        val p = Path().apply { moveTo(x.toFloat(), y.toFloat()) }
        return dispatchPath(p, durationMs)
    }

    fun longPress(x: Int, y: Int, durationMs: Long = 1000): Boolean =
        tap(x, y, durationMs)

    fun swipe(x0: Int, y0: Int, x1: Int, y1: Int, durationMs: Long = 400): Boolean {
        val p = Path().apply {
            moveTo(x0.toFloat(), y0.toFloat())
            lineTo(x1.toFloat(), y1.toFloat())
        }
        return dispatchPath(p, durationMs)
    }

    // -- keys ------------------------------------------------------------------

    /** Maps the runtime's KEYCODE_* vocabulary to a11y equivalents. */
    fun keyevent(name: String): Boolean = when (name) {
        "KEYCODE_BACK" -> performGlobalAction(GLOBAL_ACTION_BACK)
        "KEYCODE_HOME" -> performGlobalAction(GLOBAL_ACTION_HOME)
        "KEYCODE_ENTER" -> pressImeEnter()
        else -> {
            Log.w(TAG, "unsupported keyevent: $name")
            false
        }
    }

    private fun pressImeEnter(): Boolean {
        val node = findFocusedEditable() ?: run {
            Log.w(TAG, "ime-enter: no focused editable node")
            return false
        }
        // ACTION_IME_ENTER (API 30): the editor action the soft keyboard's
        // enter key would fire — what KEYCODE_ENTER means on a chat box.
        return node.performAction(
            AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER.id
        )
    }

    // -- text input -------------------------------------------------------------

    private fun findFocusedEditable(): AccessibilityNodeInfo? {
        rootInActiveWindow?.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
            ?.let { return it }
        // Some windows report input focus only on the window object.
        for (w in windows) {
            w.root?.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)?.let { return it }
        }
        return null
    }

    /**
     * Type into the focused field, INSERTING at the cursor like the host
     * path (AdbKeyboard ADB_INPUT_B64 broadcast → InputConnection.commitText:
     * inserts at the cursor, replacing only the selection). ACTION_SET_TEXT
     * is a whole-field replace, so splice the new text into the existing
     * content at the selection and put the cursor back after the insert;
     * clipboard + ACTION_PASTE (natively insert-at-cursor, so both paths
     * agree) is the fallback for views that reject SET_TEXT. Misses are
     * logged loudly per the surface-fallback-failures rule.
     */
    fun inputText(text: String): Boolean {
        val node = findFocusedEditable() ?: run {
            Log.w(TAG, "input_text: no focused editable node")
            return false
        }
        // Hint text is not content; password text is masked and cannot be
        // spliced faithfully — treat both as empty (plain replace, the
        // common case for such fields anyway).
        val existing =
            if (node.isShowingHintText || node.isPassword) ""
            else node.text?.toString() ?: ""
        // No reported cursor (-1) → append at the end, the usual cursor
        // position after focusing a field.
        var start = node.textSelectionStart
        var end = node.textSelectionEnd
        if (start < 0 || start > existing.length) start = existing.length
        if (end < start || end > existing.length) end = start
        val spliced = existing.substring(0, start) + text + existing.substring(end)
        val args = Bundle().apply {
            putCharSequence(
                AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, spliced
            )
        }
        if (node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)) {
            // SET_TEXT leaves the cursor at the field end; move it back to
            // just after the inserted text (commitText semantics). Best
            // effort — a refusal only misplaces the cursor.
            val cursor = start + text.length
            if (cursor != spliced.length) {
                val sel = Bundle().apply {
                    putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT, cursor)
                    putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT, cursor)
                }
                node.performAction(AccessibilityNodeInfo.ACTION_SET_SELECTION, sel)
            }
            return true
        }
        Log.w(TAG, "input_text: ACTION_SET_TEXT rejected; falling back to paste")
        val cm = getSystemService(ClipboardManager::class.java)
        // Best-effort snapshot for restore: on Android 10+ a background app
        // usually CANNOT read the clipboard (null), so restore degrades to
        // clearing — either way the typed text (may carry profile values,
        // exactly what RELAY_TRAJ_REDACT protects) must not linger where the
        // next focused app can read it, nor silently replace the user's clip
        // for good.
        val previous = try {
            cm.primaryClip
        } catch (e: Exception) {
            null
        }
        cm.setPrimaryClip(ClipData.newPlainText("relay", text))
        val pasted = node.performAction(AccessibilityNodeInfo.ACTION_PASTE)
        try {
            if (previous != null) cm.setPrimaryClip(previous) else cm.clearPrimaryClip()
        } catch (e: Exception) {
            Log.w(TAG, "input_text: clipboard restore failed: $e")
        }
        return pasted
    }

    // -- observation --------------------------------------------------------------

    /** Package of the focused application window (permission dialogs included —
     * they hold focus while showing, same as the host `dumpsys window` probe). */
    fun foregroundPackage(): String? {
        for (w in windows) {
            if (w.isFocused && w.type == AccessibilityWindowInfo.TYPE_APPLICATION) {
                w.root?.packageName?.let { return it.toString() }
            }
        }
        return rootInActiveWindow?.packageName?.toString()
    }

    fun uiDumpXml(): String? = try {
        A11yXmlSerializer.serialize(windows, rootInActiveWindow)
    } catch (e: Exception) {
        Log.w(TAG, "uiDumpXml failed: $e")
        null
    }

    /**
     * Degraded-mode capture via AccessibilityService.takeScreenshot (API 30) —
     * used only when MediaProjection is unavailable/revoked. Rate-limited by
     * the platform; returns null on failure or throttle.
     */
    fun takeScreenshotBlocking(timeoutMs: Long = 4000): Bitmap? {
        val latch = CountDownLatch(1)
        var bitmap: Bitmap? = null
        takeScreenshot(
            Display.DEFAULT_DISPLAY, mainExecutor,
            object : TakeScreenshotCallback {
                override fun onSuccess(result: ScreenshotResult) {
                    bitmap = result.hardwareBuffer.let { buf ->
                        Bitmap.wrapHardwareBuffer(buf, result.colorSpace)
                            ?.copy(Bitmap.Config.ARGB_8888, false)
                            .also { buf.close() }
                    }
                    latch.countDown()
                }

                override fun onFailure(errorCode: Int) {
                    Log.w(TAG, "takeScreenshot failed: $errorCode")
                    latch.countDown()
                }
            })
        latch.await(timeoutMs, TimeUnit.MILLISECONDS)
        return bitmap
    }
}
