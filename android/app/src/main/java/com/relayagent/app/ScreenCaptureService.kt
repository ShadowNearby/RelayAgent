package com.relayagent.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
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
        val mgr = getSystemService(MediaProjectionManager::class.java)
        val proj = mgr.getMediaProjection(resultCode, data) ?: run {
            Log.w(TAG, "getMediaProjection returned null")
            stopSelf()
            return
        }
        proj.registerCallback(object : MediaProjection.Callback() {
            override fun onStop() {
                Log.w(TAG, "projection revoked/stopped")
                tearDownProjection()
            }
        }, null)

        val metrics = resources.displayMetrics
        val w = metrics.widthPixels
        val h = metrics.heightPixels
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
                    null  // reader closing under us during teardown
                } ?: return@setOnImageAvailableListener
                latestImage?.close()
                latestImage = img
                frameSeq += 1
            }
        }, Handler(Looper.getMainLooper()))
        virtualDisplay = proj.createVirtualDisplay(
            "relay-capture", w, h, metrics.densityDpi,
            DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
            reader.surface, null, null,
        )
        imageReader = reader
        projection = proj
        Log.i(TAG, "projection up: ${w}x$h")
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

    private fun tearDownProjection() {
        virtualDisplay?.release()
        virtualDisplay = null
        synchronized(frameLock) {
            latestImage?.close()
            latestImage = null
        }
        imageReader?.close()
        imageReader = null
        projection = null
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
