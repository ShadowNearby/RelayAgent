package com.relayagent.app

import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.util.Log
import android.view.WindowManager
import java.io.ByteArrayOutputStream
import java.util.concurrent.atomic.AtomicBoolean

/**
 * The Kotlin facade the embedded Python runtime calls (via Chaquopy
 * `jclass("com.relayagent.app.DeviceBridge")`). One static method per device
 * primitive; the Python-side AndroidBackend maps the runtime's DeviceBackend
 * interface onto these.
 *
 * Threading: everything here is called from the Python worker thread.
 * Gesture/key/text calls block until dispatched (RelayAccessibilityService);
 * UI work (overlay, ask_user) hops to the main thread internally.
 */
object DeviceBridge {

    private const val TAG = "RelayBridge"

    private lateinit var appContext: Context
    private val stopRequested = AtomicBoolean(false)

    fun init(context: Context) {
        appContext = context.applicationContext
    }

    private val a11y: RelayAccessibilityService?
        get() = RelayAccessibilityService.instance.also {
            if (it == null) Log.w(TAG, "accessibility service not connected")
        }

    // -- observation ----------------------------------------------------------

    /** Current frame as JPEG bytes (quality 85). MediaProjection first (fast,
     * continuous); a11y takeScreenshot as the degraded path; null when both
     * fail. JPEG on purpose: PNG is lossless (the quality arg is ignored) and
     * costs hundreds of ms per 1080p+ frame on a mid-range SoC — the single
     * biggest per-step cost — while a JPEG encode is tens of ms and PIL's
     * Image.open on the Python side decodes either format transparently.
     * Consumers only need same-source frame consistency (hash prechecks
     * compare frames that went through this same encoder) + VLM legibility,
     * not pixel-exact lossless frames. */
    @JvmStatic
    fun screencapJpeg(): ByteArray? {
        val bmp: Bitmap? = ScreenCaptureService.instance
            ?.takeIf { it.isActive }?.captureBitmap()
            ?: a11y?.takeScreenshotBlocking()
        return bmp?.let {
            val out = ByteArrayOutputStream(1 shl 19)
            it.compress(Bitmap.CompressFormat.JPEG, 85, out)
            out.toByteArray()
        }
    }

    /** Same geometry the capture pipeline mirrors into (see
     * ScreenCaptureService.captureGeometry): real display bounds in the current
     * orientation, system bars included. App-context displayMetrics can exclude
     * the bars and goes stale on rotation, so gesture geometry computed from it
     * would not match the frames the agent looks at. */
    @JvmStatic
    fun screenSize(): IntArray {
        val bounds = appContext.getSystemService(WindowManager::class.java)
            .maximumWindowMetrics.bounds
        return intArrayOf(bounds.width(), bounds.height())
    }

    /** Monotonic frame-arrival counter off the MediaProjection pipeline, for
     * settle detection (the on-device analogue of the host's scrcpy
     * frame_seq — see OnDeviceAndroidBackend.wait_settled). -1 when the
     * projection is down (caller falls back to fixed sleeps). */
    @JvmStatic
    fun captureFrameSeq(): Long =
        ScreenCaptureService.instance?.takeIf { it.isActive }?.frameSeq ?: -1L

    @JvmStatic
    fun uiDumpXml(): String? = a11y?.uiDumpXml()

    @JvmStatic
    fun foregroundPackage(): String? = a11y?.foregroundPackage()

    // -- gestures / keys ---------------------------------------------------------

    @JvmStatic
    fun tap(x: Int, y: Int): Boolean = a11y?.tap(x, y) ?: false

    @JvmStatic
    fun longPress(x: Int, y: Int, durationMs: Int): Boolean =
        a11y?.longPress(x, y, durationMs.toLong()) ?: false

    @JvmStatic
    fun swipe(x0: Int, y0: Int, x1: Int, y1: Int, durationMs: Int): Boolean =
        a11y?.swipe(x0, y0, x1, y1, durationMs.toLong()) ?: false

    @JvmStatic
    fun keyevent(name: String): Boolean = a11y?.keyevent(name) ?: false

    @JvmStatic
    fun inputText(text: String): Boolean = a11y?.inputText(text) ?: false

    // -- app lifecycle --------------------------------------------------------------

    /**
     * Launch the target app with a cleared task — the closest no-shell
     * approximation of the host's force-stop + monkey cold launch. The
     * process may survive (no real force-stop without shell; Android 14
     * killBackgroundProcesses only kills the caller), so in-memory state can
     * persist: a KNOWN semantic drift from the host cold-launch policy,
     * accepted for on-device runs (see plan §risks).
     */
    @JvmStatic
    fun launchApp(pkg: String): Boolean {
        val intent = appContext.packageManager.getLaunchIntentForPackage(pkg) ?: run {
            Log.w(TAG, "no launch intent for $pkg")
            return false
        }
        intent.addFlags(
            Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK
        )
        return try {
            appContext.startActivity(intent)
            true
        } catch (e: Exception) {
            Log.w(TAG, "launchApp $pkg failed: $e")
            false
        }
    }

    /** Best-effort "force stop": no shell privilege exists for a real
     * `am force-stop`, so this is a no-op that logs the drift. Kept as a
     * separate entry point so a future root/Shizuku build can fill it in. */
    @JvmStatic
    fun forceStopApprox(pkg: String): Boolean {
        Log.i(TAG, "force-stop($pkg): unavailable without shell; relying on CLEAR_TASK relaunch")
        return false
    }

    // -- interaction (overlay) ---------------------------------------------------------

    /** Blocking ask_user: shows the overlay panel, returns the answer or null
     * on take-over/dismiss. Called from the Python worker thread. The typed
     * events let the conversation UI show a "waiting for your answer" line
     * while the panel is up (take-over ends the run, so no resumed event). */
    @JvmStatic
    fun askUser(text: String): String? {
        RunEvents.post(RunEvents.Event.AskUser)
        val answer = OverlayController.askUserBlocking(text)
        if (answer != null) RunEvents.post(RunEvents.Event.AskAnswered)
        return answer
    }

    @JvmStatic
    fun emitStatus(json: String) = OverlayController.postStatus(json)

    @JvmStatic
    fun shouldStop(): Boolean = stopRequested.get()

    fun requestStop() = stopRequested.set(true)

    fun resetStop() = stopRequested.set(false)

    // -- storage ------------------------------------------------------------------------

    @JvmStatic
    fun appFilesDir(): String = appContext.filesDir.absolutePath
}
