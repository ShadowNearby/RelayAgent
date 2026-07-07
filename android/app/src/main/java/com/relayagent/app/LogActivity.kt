package com.relayagent.app

import android.content.Intent
import android.graphics.Color
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import androidx.appcompat.app.AppCompatActivity
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.google.android.material.chip.Chip
import com.relayagent.app.databinding.ActivityLogListBinding
import com.relayagent.app.databinding.ItemRunBinding

/**
 * Structured run viewer (entry point). Lists each on-device run as a card
 * showing the original request + when + which apps + how many subtasks, newest
 * first. Tapping opens RunDetailActivity. The raw file tree lives behind
 * RawLogActivity for power users.
 */
class LogActivity : AppCompatActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val ui = ActivityLogListBinding.inflate(layoutInflater)
        setContentView(ui.root)
        ui.toolbar.setNavigationOnClickListener { finish() }
        ui.toolbar.inflateMenu(R.menu.run_detail)
        ui.toolbar.setOnMenuItemClickListener {
            if (it.itemId == R.id.action_raw_files) {
                startActivity(Intent(this, RawLogActivity::class.java)); true
            } else false
        }

        val runs = TrajLog.listRuns(TrajLog.trajRoot(filesDir))
        ui.empty.visibility = if (runs.isEmpty()) View.VISIBLE else View.GONE
        ui.list.layoutManager = LinearLayoutManager(this)
        ui.list.adapter = Adapter(runs) { run ->
            startActivity(
                Intent(this, RunDetailActivity::class.java)
                    .putExtra(RunDetailActivity.EXTRA_DIR, run.dir.absolutePath)
            )
        }
    }

    private class Adapter(
        val items: List<TrajLog.Run>,
        val onClick: (TrajLog.Run) -> Unit,
    ) : RecyclerView.Adapter<Adapter.VH>() {

        class VH(val b: ItemRunBinding) : RecyclerView.ViewHolder(b.root)

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): VH =
            VH(ItemRunBinding.inflate(LayoutInflater.from(parent.context), parent, false))

        override fun getItemCount() = items.size

        override fun onBindViewHolder(holder: VH, position: Int) {
            val run = items[position]
            val ctx = holder.b.root.context
            holder.b.title.text = run.request ?: prettyName(ctx, run.dir.name)
            holder.b.subtitle.text = buildString {
                append(run.createdAt ?: dateFromName(run.dir.name))
                append(ctx.getString(R.string.log_leg_count, run.legCount))
            }
            holder.b.tags.removeAllViews()
            for (app in run.apps) {
                holder.b.tags.addView(chip(holder, AppLabels.label(app), false))
            }
            if (run.error != null) {
                holder.b.tags.addView(chip(holder, ctx.getString(R.string.badge_failed), true))
            }
            holder.b.root.setOnClickListener { onClick(run) }
        }

        private fun chip(holder: VH, text: String, error: Boolean): Chip =
            Chip(holder.b.root.context).apply {
                this.text = text
                isClickable = false
                isCheckable = false
                setEnsureMinTouchTargetSize(false)
                chipMinHeight = resources.displayMetrics.density * 26
                if (error) {
                    setChipBackgroundColorResource(R.color.status_bad)
                    setTextColor(Color.WHITE)
                }
            }

        private fun prettyName(ctx: android.content.Context, name: String): String {
            val i = name.indexOf("_plan_")
            return if (i >= 0) ctx.getString(R.string.log_run_title, name.substring(0, i)) else name
        }

        private fun dateFromName(name: String): String {
            // "20260613_234336..." -> "2026-06-13 23:43:36"
            val m = Regex("(\\d{4})(\\d{2})(\\d{2})_(\\d{2})(\\d{2})(\\d{2})").find(name)
                ?: return name
            val (y, mo, d, h, mi, s) = m.destructured
            return "$y-$mo-$d $h:$mi:$s"
        }
    }
}
