package com.relayagent.app

import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * In-memory rolling buffer of human-readable run log lines, written by
 * [RunSession] and the overlay status events ([OverlayController.postStatus]).
 *
 * The conversation-style home screen replaced the old MainActivity live log
 * pane, so nothing renders this buffer right now: it is kept as a cheap
 * in-process breadcrumb trail (debugger / heap-dump visible) behind the
 * documented postStatus fan-out. The former reader API (listener / snapshot /
 * clear) had no callers left and was removed with the pane — a future
 * live-tail viewer can add a snapshot() reader back. Disk trajectory logs
 * (traj.json / steps) remain the durable record browsed via LogActivity.
 */
object RunLog {

    private const val CAP = 500
    private val stamp = SimpleDateFormat("HH:mm:ss", Locale.US)
    private val lines = ArrayDeque<String>()

    @Synchronized
    fun append(line: String) {
        lines.addLast("${stamp.format(Date())}  $line")
        while (lines.size > CAP) lines.removeFirst()
    }
}
