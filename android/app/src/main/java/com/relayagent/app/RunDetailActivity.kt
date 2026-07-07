package com.relayagent.app

import android.content.Intent
import android.graphics.Color
import android.os.Bundle
import android.view.LayoutInflater
import android.view.ViewGroup
import androidx.appcompat.app.AppCompatActivity
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.relayagent.app.databinding.ActivityRunDetailBinding
import com.relayagent.app.databinding.ItemLegBinding
import java.io.File

/**
 * One run: its request + metadata header, then a card per subtask (leg) with
 * status, step count, wall time and a reply preview. Tapping a leg opens the
 * step timeline (LegDetailActivity).
 */
class RunDetailActivity : AppCompatActivity() {

    companion object {
        const val EXTRA_DIR = "dir"
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val ui = ActivityRunDetailBinding.inflate(layoutInflater)
        setContentView(ui.root)
        ui.toolbar.setNavigationOnClickListener { finish() }

        val runDir = File(intent.getStringExtra(EXTRA_DIR) ?: run { finish(); return })
        ui.toolbar.inflateMenu(R.menu.run_detail)
        ui.toolbar.setOnMenuItemClickListener {
            if (it.itemId == R.id.action_raw_files) {
                startActivity(
                    Intent(this, RawLogActivity::class.java)
                        .putExtra(RawLogActivity.EXTRA_DIR, runDir.absolutePath)
                        .putExtra(RawLogActivity.EXTRA_TITLE, runDir.name)
                )
                true
            } else false
        }

        val run = TrajLog.listRuns(runDir.parentFile ?: filesDir)
            .firstOrNull { it.dir.name == runDir.name }
        val legs = TrajLog.legDirs(runDir).map { TrajLog.parseLeg(it) }

        ui.request.text = run?.request ?: runDir.name
        ui.runMeta.text = buildString {
            run?.createdAt?.let { append(it) }
            if (run?.apps?.isNotEmpty() == true) {
                if (isNotEmpty()) append("  ·  ")
                append(run.apps.joinToString("、") { AppLabels.label(it) })
            }
            if (isNotEmpty()) append("  ·  ")
            append(getString(R.string.leg_count, legs.size))
        }

        ui.list.layoutManager = LinearLayoutManager(this)
        ui.list.adapter = Adapter(legs) { leg ->
            startActivity(
                Intent(this, LegDetailActivity::class.java)
                    .putExtra(LegDetailActivity.EXTRA_DIR, leg.dir.absolutePath)
            )
        }
    }

    private class Adapter(
        val items: List<TrajLog.Leg>,
        val onClick: (TrajLog.Leg) -> Unit,
    ) : RecyclerView.Adapter<Adapter.VH>() {

        class VH(val b: ItemLegBinding) : RecyclerView.ViewHolder(b.root)

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): VH =
            VH(ItemLegBinding.inflate(LayoutInflater.from(parent.context), parent, false))

        override fun getItemCount() = items.size

        override fun onBindViewHolder(holder: VH, position: Int) {
            val leg = items[position]
            val ctx = holder.b.root.context
            holder.b.legTitle.text = LegFormat.title(leg)
            holder.b.legMeta.text = LegFormat.meta(ctx, leg)

            val status = leg.goalStatus ?: leg.verdictStatus
            if (status != null) {
                holder.b.statusBadge.visibility = ViewGroup.VISIBLE
                holder.b.statusBadge.text = LegFormat.statusText(ctx, status)
                holder.b.statusBadge.setBackgroundColor(LegFormat.statusColor(status))
            } else {
                holder.b.statusBadge.visibility = ViewGroup.GONE
            }

            val reply = leg.reply?.trim()
            if (!reply.isNullOrEmpty()) {
                holder.b.legReply.visibility = ViewGroup.VISIBLE
                holder.b.legReply.text = reply
            } else {
                holder.b.legReply.visibility = ViewGroup.GONE
            }
            holder.b.root.setOnClickListener { onClick(leg) }
        }
    }
}

/** Shared leg formatting used by the run list + leg detail header. */
object LegFormat {

    fun title(leg: TrajLog.Leg): String {
        val name = leg.id.replaceFirst(Regex("^\\d+_"), "").replace('_', ' ')
        val app = leg.app?.let { AppLabels.label(it) }
        return if (app != null) "$name · $app" else name
    }

    fun meta(ctx: android.content.Context, leg: TrajLog.Leg): String = buildString {
        append(ctx.getString(R.string.step_count, leg.steps))
        leg.wallSeconds?.let { append("  ·  ${"%.1f".format(it)}s") }
        leg.totalTokens?.let { append("  ·  $it tokens") }
        leg.capability?.let { append("  ·  $it") }
    }

    fun statusText(ctx: android.content.Context, status: String): String = when (status.lowercase()) {
        "complete", "success", "pass" -> ctx.getString(R.string.verdict_complete)
        "infeasible", "fail", "failed" -> ctx.getString(R.string.verdict_failed)
        "unknown" -> ctx.getString(R.string.verdict_unknown)
        else -> status
    }

    fun statusColor(status: String): Int = when (status.lowercase()) {
        "complete", "success", "pass" -> 0xFF0E9F6E.toInt()
        "infeasible", "fail", "failed" -> 0xFFE02424.toInt()
        else -> 0xFF6B6A75.toInt()
    }
}
