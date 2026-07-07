package com.relayagent.app

import android.content.Intent
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import androidx.appcompat.app.AppCompatActivity
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.relayagent.app.databinding.ActivityLogListBinding
import com.relayagent.app.databinding.ItemLogEntryBinding
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Raw file browser over a run/leg directory — the power-user fallback behind
 * the structured viewer (LogActivity -> RunDetailActivity -> LegDetailActivity).
 * Directories drill in; files open in LogDetailActivity.
 */
class RawLogActivity : AppCompatActivity() {

    companion object {
        const val EXTRA_DIR = "dir"
        const val EXTRA_TITLE = "title"
    }

    private val fmt = SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val ui = ActivityLogListBinding.inflate(layoutInflater)
        setContentView(ui.root)
        ui.toolbar.setNavigationOnClickListener { finish() }

        val dirPath = intent.getStringExtra(EXTRA_DIR)
        val root = if (dirPath != null) File(dirPath) else File(filesDir, "traj_logs")
        ui.toolbar.title = intent.getStringExtra(EXTRA_TITLE) ?: getString(R.string.title_logs)

        val entries = listEntries(root)
        ui.empty.visibility = if (entries.isEmpty()) View.VISIBLE else View.GONE
        ui.list.layoutManager = LinearLayoutManager(this)
        ui.list.adapter = Adapter(entries) { onEntryClick(it) }
    }

    private fun listEntries(dir: File): List<File> {
        val children = dir.listFiles()?.toList() ?: emptyList()
        val (dirs, files) = children.partition { it.isDirectory }
        return dirs.sortedByDescending { it.name } + files.sortedBy { it.name }
    }

    private fun onEntryClick(file: File) {
        if (file.isDirectory) {
            startActivity(
                Intent(this, RawLogActivity::class.java)
                    .putExtra(EXTRA_DIR, file.absolutePath)
                    .putExtra(EXTRA_TITLE, file.name)
            )
        } else {
            startActivity(
                Intent(this, LogDetailActivity::class.java)
                    .putExtra(LogDetailActivity.EXTRA_FILE, file.absolutePath)
            )
        }
    }

    private inner class Adapter(
        val items: List<File>,
        val onClick: (File) -> Unit,
    ) : RecyclerView.Adapter<Adapter.VH>() {

        inner class VH(val b: ItemLogEntryBinding) : RecyclerView.ViewHolder(b.root)

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): VH {
            val b = ItemLogEntryBinding.inflate(
                LayoutInflater.from(parent.context), parent, false
            )
            return VH(b)
        }

        override fun getItemCount() = items.size

        override fun onBindViewHolder(holder: VH, position: Int) {
            val f = items[position]
            holder.b.name.text = f.name
            if (f.isDirectory) {
                holder.b.icon.text = "📁"
                val n = f.listFiles()?.size ?: 0
                holder.b.subtitle.text =
                    holder.b.root.context.getString(R.string.raw_items_subtitle, n, fmt.format(Date(f.lastModified())))
            } else {
                holder.b.icon.text = iconFor(f.name)
                holder.b.subtitle.text =
                    "${humanSize(f.length())} · ${fmt.format(Date(f.lastModified()))}"
            }
            holder.b.root.setOnClickListener { onClick(f) }
        }
    }

    private fun iconFor(name: String): String = when {
        name.endsWith(".png") || name.endsWith(".jpg") -> "🖼️"
        name.endsWith(".json") -> "🧾"
        else -> "📄"
    }

    private fun humanSize(bytes: Long): String = when {
        bytes >= 1 shl 20 -> "%.1f MB".format(bytes / (1 shl 20).toDouble())
        bytes >= 1 shl 10 -> "%.1f KB".format(bytes / (1 shl 10).toDouble())
        else -> "$bytes B"
    }
}
