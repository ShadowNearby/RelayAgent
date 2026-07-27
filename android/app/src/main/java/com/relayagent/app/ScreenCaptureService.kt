package com.relayagent.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.content.res.Configuration
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.Image
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.util.Log
import android.view.WindowManager

/**
 * Foreground service owning the MediaProjection capture pipeline.
 *
 * A VirtualDisplay feeds an ImageReader; the latest frame is converted to a
 * Bitmap on demand by [captureBitmap]. Continuous projection makes a frame
 * grab ~tens of ms — replacing the ~1.5s `adb exec-out screencap` that was
 * the single biggest per-step cost on host (CLAUDE.md, 性能旋钮).
 *
 * Consent flow: MainActivity fires createScreenCaptureIntent() per run
 * (Android 14 requires per-session consent) and hands resultCode/data here
 * via [start]. If the user revokes mid-run, [MediaProjection.Callback.onStop]
 * clears state and [DeviceBridge] falls back to the a11y takeScreenshot path
 * (rate-limited but keeps the loop alive).
 */
class ScreenCaptureService : Service() {

    companion object {
        private const val TAG = "RelayCapture"
        private const val CHANNEL_ID = "relay_capture"
        private const val NOTIF_ID = 1001
        private const val EXTRA_RESULT_CODE = "resultCode"
        private const val EXTRA_RESULT_DATA = "resultData"
        const val ACTION_STOP = "com.relayagent.app.action.STOP_RUN"

        @Volatile
        var instance: ScreenCaptureService? = null
            private set

        fun start(context: Context, resultCode: Int, data: Intent) {
            val i = Intent(context, ScreenCaptureService::class.java)
                .putExtra(EXTRA_RESULT_CODE, resultCode)
                .putExtra(EXTRA_RESULT_DATA, data)
            context.startForegroundService(i)
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, ScreenCaptureService::class.java))
        }
    }

    private var projection: MediaProjection? = null
    private var projectionCallback: MediaProjection.Callback? = null
    private var virtualDisplay: VirtualDisplay? = null
    private var imageReader: ImageReader? = null
    private var latestImage: Image? = null
    private val frameLock = Any()

    /**
     * Monotonic frame-arrival counter for settle detection: the VirtualDisplay
     * surface only receives buffers when the screen CHANGES (same property the
     * host's scrcpy stream exploits, P2-S2), so "frameSeq unchanged for a quiet
     * window" means the screen is static. Read from the Python worker thread
     * via [DeviceBridge.captureFrameSeq].
     */
    @Volatile
    var frameSeq: Long = 0
        private set

    val isActive: Boolean get() = projection != null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent == null) return START_NOT_STICKY
        if (intent.action == ACTION_STOP) {
            // User tapped 停止 on the capture notification — signal the run loop
            // (polled at each step / leg boundary). The flow tears the service
            // and overlay down itself when it unwinds.
            Log.i(TAG, "stop requested via notification")
            DeviceBridge.requestStop()
            return START_NOT_STICKY
        }
        startForeground(
            NOTIF_ID, buildNotification(),
            ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION,
        )
        val resultCode = intent.getIntExtra(EXTRA_RESULT_CODE, 0)
        @Suppress("DEPRECATION")
        val data = intent.getParcelableExtra<Intent>(EXTRA_RESULT_DATA)
        if (data == null) {
            Log.w(TAG, "missing projection consent data; stopping")
            stopSelf()
            return START_NOT_STICKY
        }
        setUpProjection(resultCode, data)
        instance = this
        return START_NOT_STICKY
    }

    private fun setUpProjection(resultCode: Int, data: Intent) {
        // A repeated start (new consent while a session is still up) must not
        // overwrite live fields and orphan the old session; no-op when idle.
        tearDownProjection()
        val mgr = getSystemService(MediaProjectionManager::class.java)
        val proj = mgr.getMediaProjection(resultCode, data) ?: run {
            Log.w(TAG, "getMediaProjection returned null")
            stopSelf()
            return
        }
        // Must be registered before createVirtualDisplay (enforced on Android 14).
        // Kept in a field so tearDownProjection can unregister it before calling
        // stop(), which would otherwise re-enter teardown via this onStop.
        val callback = object : MediaProjection.Callback() {
            override fun onStop() {
                Log.w(TAG, "projection revoked/stopped")
                tearDownProjection()
            }
        }
        proj.registerCallback(callback, null)
        projectionCallback = callback

        val (w, h, dpi) = captureGeometry()
        val reader = newFrameReader(w, h)
        virtualDisplay = proj.createVirtualDisplay(
            "relay-capture", w, h, dpi,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
            reader.surface, null, null,
        )
        imageReader = reader
        projection = proj
        Log.i(TAG, "projection up: ${w}x$h")
    }

    /**
     * Real display size (system bars included) in the CURRENT orientation,
     * plus density — read at VirtualDisplay (re)build time, never cached.
     * NOT resources.displayMetrics: app-context metrics can exclude system
     * bars on some devices and go stale on rotation, which letterboxes the
     * AUTO_MIRROR content into a wrong-sized surface — frame pixels must map
     * 1:1 onto the dispatchGesture screen coordinate space, the same space
     * the degraded-mode a11y takeScreenshot frames use, so both capture
     * paths also stay size-consistent within a leg.
     */
    private fun captureGeometry(): Triple<Int, Int, Int> {
        val bounds = getSystemService(WindowManager::class.java)
            .maximumWindowMetrics.bounds
        return Triple(bounds.width(), bounds.height(), resources.displayMetrics.densityDpi)
    }

    private fun newFrameReader(w: Int, h: Int): ImageReader {
        // 3 buffers: one held as latestImage + one being acquired + producer
        // headroom, so holding the latest frame never stalls the pipeline.
        val reader = ImageReader.newInstance(w, h, PixelFormat.RGBA_8888, 3)
        // Frames are drained as they arrive (keep-latest): each callback swaps
        // the held Image and bumps frameSeq. Without a consumer the queue
        // would fill after maxImages frames and arrival events would stop —
        // which would make settle detection see a static screen mid-animation.
        reader.setOnImageAvailableListener({ r ->
            synchronized(frameLock) {
                val img = try {
                    r.acquireLatestImage()
                } catch (e: Exception) {
                    null  // reader closing under us during teardown/resize
                } ?: return@setOnImageAvailableListener
                latestImage?.close()
                latestImage = img
                frameSeq += 1
            }
        }, Handler(Looper.getMainLooper()))
        return reader
    }

    override fun onConfigurationChanged(newConfig: Configuration) {
        super.onConfigurationChanged(newConfig)
        resizeForCurrentDisplay()
    }

    /**
     * Rotation / fold / resolution change while a session is live: the
     * projection keeps mirroring into the old-sized surface (letterboxed →
     * every frame-grounded tap goes wrong), so rebuild the consumer side at
     * the new geometry. Config-change driven — a Service receives
     * onConfigurationChanged for orientation/screen-size changes, so no
     * DisplayListener bookkeeping is needed (a 180° flip keeps the same size
     * and is correctly a no-op). Resize-in-place per the platform guidance:
     * Android 14 allows only ONE createVirtualDisplay per MediaProjection,
     * so the VirtualDisplay is resized and handed a fresh reader surface,
     * never recreated.
     */
    private fun resizeForCurrentDisplay() {
        val vd = virtualDisplay ?: return
        val oldReader = imageReader ?: return
        val (w, h, dpi) = captureGeometry()
        if (oldReader.width == w && oldReader.height == h) return
        val reader = newFrameReader(w, h)
        vd.resize(w, h, dpi)
        vd.surface = reader.surface
        // Detach the old listener before closing (as in teardown) and drop
        // the stale-geometry frame under the lock so captureBitmap never sees
        // a mid-close Image; until the first new-size frame arrives,
        // DeviceBridge serves the a11y-screenshot fallback (already the
        // correct new size).
        oldReader.setOnImageAvailableListener(null, null)
        synchronized(frameLock) {
            latestImage?.close()
            latestImage = null
            imageReader = reader
        }
        oldReader.close()
        Log.i(TAG, "projection resized: ${w}x$h")
    }

    /**
     * Latest frame as ARGB_8888 Bitmap, or null when the pipeline is down or
     * no frame has arrived yet. Called from the Python worker thread. Reads
     * the listener-held latest Image; the Image stays owned by the listener
     * (closed on the next swap), so it is NOT closed here.
     */
    fun captureBitmap(): Bitmap? {
        synchronized(frameLock) {
            val img = latestImage ?: return null
            val plane = img.planes[0]
            val rowStride = plane.rowStride
            val pixelStride = plane.pixelStride
            plane.buffer.rewind()
            val padded = Bitmap.createBitmap(
                rowStride / pixelStride, img.height, Bitmap.Config.ARGB_8888
            )
            padded.copyPixelsFromBuffer(plane.buffer)
            // Trim row-stride padding to the true width.
            return if (padded.width != img.width) {
                Bitmap.createBitmap(padded, 0, 0, img.width, img.height)
            } else padded
        }
    }

    /**
     * Releases the capture pipeline consumer-side first (VirtualDisplay →
     * held Image → ImageReader), then ends the projection session itself.
     * Runs on both paths: explicit teardown (onDestroy) and system revocation
     * (the callback's onStop). Fields are nulled as they are released, so a
     * second entry is a no-op.
     */
    private fun tearDownProjection() {
        virtualDisplay?.release()
        virtualDisplay = null
        // Drop the frame listener before closing so a queued callback can't
        // touch a closed reader and the lambda's reference to this is released.
        imageReader?.setOnImageAvailableListener(null, null)
        synchronized(frameLock) {
            latestImage?.close()
            latestImage = null
        }
        imageReader?.close()
        imageReader = null
        val proj = projection
        projection = null
        val callback = projectionCallback
        projectionCallback = null
        if (proj != null) {
            // Unregister first: stop() dispatches onStop to registered
            // callbacks, which would re-enter this method. On the revocation
            // path the session is already stopped and the extra stop() is a
            // no-op system-side, so this ends the session exactly once —
            // without it the token stays held and the status-bar capture
            // indicator persists after every run.
            callback?.let { proj.unregisterCallback(it) }
            proj.stop()
        }
    }

    override fun onDestroy() {
        tearDownProjection()
        if (instance === this) instance = null
        super.onDestroy()
    }

    private fun buildNotification(): Notification {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(
                CHANNEL_ID, getString(R.string.notif_channel_capture),
                NotificationManager.IMPORTANCE_LOW,
            )
        )
        val stopIntent = PendingIntent.getService(
            this, 0,
            Intent(this, ScreenCaptureService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE,
        )
        return Notification.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_menu_camera)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(getString(R.string.notif_capture_text))
            .setOngoing(true)
            .addAction(
                Notification.Action.Builder(
                    null, getString(R.string.action_stop), stopIntent
                ).build()
            )
            .build()
    }
}
