package com.relayagent.app

import android.os.Handler
import android.os.Looper
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * In-memory rolling buffer of human-readable run log lines. Both the live log
 * pane in MainActivity and the overlay status events write here, so the pane
 * survives navigating away and back. Disk trajectory logs (traj.json / steps)
 * remain the durable record browsed via LogActivity — this is the live tail.
 */
object RunLog {

    private const val CAP = 500
    private val main = Handler(Looper.getMainLooper())
    private val stamp = SimpleDateFormat("HH:mm:ss", Locale.US)
    private val lines = ArrayDeque<String>()

    /** Set by MainActivity while it is visible; called on the main thread.
     * A null argument means "reset" — the listener should reload from snapshot. */
    @Volatile
    var listener: ((String?) -> Unit)? = null

    @Synchronized
    fun append(line: String) {
        val entry = "${stamp.format(Date())}  $line"
        lines.addLast(entry)
        while (lines.size > CAP) lines.removeFirst()
        main.post { listener?.invoke(entry) }
    }

    @Synchronized
    fun snapshot(): String = lines.joinToString("\n")

    @Synchronized
    fun clear() {
        lines.clear()
        main.post { listener?.invoke(null) }
    }
}
