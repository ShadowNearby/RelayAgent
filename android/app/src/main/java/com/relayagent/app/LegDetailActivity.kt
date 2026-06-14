package com.relayagent.app

import android.content.Intent
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import androidx.appcompat.app.AppCompatActivity
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.relayagent.app.databinding.ActivityLegDetailBinding
import com.relayagent.app.databinding.ItemStepBinding
import org.json.JSONObject
import java.io.File

/**
 * One subtask (leg): a header with status / steps / wall / tokens / reply,
 * then the step timeline — each step shows the frame the agent acted on, the
 * action it took, the click position and its thought. Tapping a step opens the
 * full-resolution frame.
 */
class LegDetailActivity : AppCompatActivity() {

    companion object {
        const val EXTRA_DIR = "dir"
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val ui = ActivityLegDetailBinding.inflate(layoutInflater)
        setContentView(ui.root)
        ui.toolbar.setNavigationOnClickListener { finish() }

        val legDir = File(intent.getStringExtra(EXTRA_DIR) ?: run { finish(); return })
        val leg = TrajLog.parseLeg(legDir)
        ui.toolbar.title = LegFormat.title(leg)

        ui.legMeta.text = buildString {
            val status = leg.goalStatus ?: leg.verdictStatus
            if (status != null) append("状态：${LegFormat.statusText(status)}\n")
            append(LegFormat.meta(leg))
            leg.lastAction?.let { append("\n末动作：$it") }
            leg.verdictReason?.let { append("\n判定：$it") }
        }

        val reply = leg.reply?.trim()
        if (!reply.isNullOrEmpty()) {
            ui.replyLabel.visibility = View.VISIBLE
            ui.reply.visibility = View.VISIBLE
            ui.reply.text = reply
        }

        val steps = TrajLog.parseSteps(legDir)
        ui.noSteps.visibility = if (steps.isEmpty()) View.VISIBLE else View.GONE
        ui.stepsLabel.visibility = if (steps.isEmpty()) View.GONE else View.VISIBLE
        ui.list.layoutManager = LinearLayoutManager(this)
        ui.list.adapter = Adapter(steps) { step ->
            val frame = step.marked ?: step.screenshot ?: return@Adapter
            startActivity(
                Intent(this, LogDetailActivity::class.java)
                    .putExtra(LogDetailActivity.EXTRA_FILE, frame.absolutePath)
            )
        }
    }

    private class Adapter(
        val items: List<TrajLog.Step>,
        val onClick: (TrajLog.Step) -> Unit,
    ) : RecyclerView.Adapter<Adapter.VH>() {

        class VH(val b: ItemStepBinding) : RecyclerView.ViewHolder(b.root)

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): VH =
            VH(ItemStepBinding.inflate(LayoutInflater.from(parent.context), parent, false))

        override fun getItemCount() = items.size

        override fun onBindViewHolder(holder: VH, position: Int) {
            val step = items[position]
            holder.b.stepNum.text = "步骤 ${step.n}"
            holder.b.actionType.text = step.actionType
            holder.b.actionDetail.text = formatAction(step)

            val thought = step.thought.trim()
            if (thought.isNotEmpty()) {
                holder.b.thought.visibility = View.VISIBLE
                holder.b.thought.text = thought
            } else {
                holder.b.thought.visibility = View.GONE
            }

            val frame = step.marked ?: step.screenshot
            if (frame != null) {
                holder.b.thumb.visibility = View.VISIBLE
                holder.b.thumb.setImageBitmap(decodeThumb(frame, 240))
                holder.b.thumb.setOnClickListener { onClick(step) }
            } else {
                holder.b.thumb.visibility = View.GONE
            }
        }

        private fun formatAction(step: TrajLog.Step): String {
            step.click?.let { return "(${it[0]}, ${it[1]})" }
            val a = step.action ?: return ""
            val parts = a.keys().asSequence()
                .filter { it != "action_type" }
                .map { k -> "$k=${a.opt(k)}" }
                .toList()
            return parts.joinToString("  ").take(160)
        }

        /** Decode a down-sampled bitmap so a timeline of frames stays light. */
        private fun decodeThumb(file: File, targetW: Int): Bitmap? {
            val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
            BitmapFactory.decodeFile(file.absolutePath, bounds)
            var sample = 1
            while (bounds.outWidth / sample > targetW * 2) sample *= 2
            val opts = BitmapFactory.Options().apply { inSampleSize = sample }
            return BitmapFactory.decodeFile(file.absolutePath, opts)
        }
    }
}
