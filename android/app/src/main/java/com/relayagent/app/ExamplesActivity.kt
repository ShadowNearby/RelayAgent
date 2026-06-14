package com.relayagent.app

import android.content.Intent
import android.graphics.Color
import android.os.Bundle
import android.view.LayoutInflater
import android.view.ViewGroup
import androidx.appcompat.app.AppCompatActivity
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.google.android.material.chip.Chip
import com.relayagent.app.databinding.ActivityExamplesBinding
import com.relayagent.app.databinding.ItemExampleBinding
import org.json.JSONObject

/**
 * Browse the 50 bundled task examples (res/raw/examples.json, generated from
 * RelayBench + AndroidDaily by scripts/android/gen_app_examples.py). Tapping a row
 * returns its instruction to MainActivity to fill the goal box.
 */
class ExamplesActivity : AppCompatActivity() {

    companion object {
        const val EXTRA_INSTRUCTION = "instruction"
    }

    data class Example(
        val instruction: String,
        val app: String,
        val category: String,
        val difficulty: String,
        val type: String,
        val source: String,
    )

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val ui = ActivityExamplesBinding.inflate(layoutInflater)
        setContentView(ui.root)
        ui.toolbar.setNavigationOnClickListener { finish() }

        val items = loadExamples()
        ui.list.layoutManager = LinearLayoutManager(this)
        ui.list.adapter = Adapter(items) { example ->
            setResult(RESULT_OK, Intent().putExtra(EXTRA_INSTRUCTION, example.instruction))
            finish()
        }
    }

    private fun loadExamples(): List<Example> {
        val raw = resources.openRawResource(R.raw.examples)
            .bufferedReader().use { it.readText() }
        val arr = JSONObject(raw).getJSONArray("examples")
        return (0 until arr.length()).map { i ->
            val o = arr.getJSONObject(i)
            Example(
                instruction = o.optString("instruction"),
                app = o.optString("app"),
                category = o.optString("category"),
                difficulty = o.optString("difficulty"),
                type = o.optString("type"),
                source = o.optString("source"),
            )
        }
    }

    private class Adapter(
        val items: List<Example>,
        val onClick: (Example) -> Unit,
    ) : RecyclerView.Adapter<Adapter.VH>() {

        class VH(val b: ItemExampleBinding) : RecyclerView.ViewHolder(b.root)

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): VH {
            val b = ItemExampleBinding.inflate(
                LayoutInflater.from(parent.context), parent, false
            )
            return VH(b)
        }

        override fun getItemCount() = items.size

        override fun onBindViewHolder(holder: VH, position: Int) {
            val item = items[position]
            holder.b.instruction.text = item.instruction
            holder.b.tags.removeAllViews()
            val tags = buildList {
                if (item.source.isNotEmpty()) add(item.source to true)
                if (item.app.isNotEmpty()) add(item.app to false)
                if (item.category.isNotEmpty()) add(item.category to false)
                if (item.difficulty.isNotEmpty()) add(item.difficulty to false)
            }
            for ((text, accent) in tags) {
                holder.b.tags.addView(makeChip(holder, text, accent))
            }
            holder.b.root.setOnClickListener { onClick(item) }
        }

        private fun makeChip(holder: VH, text: String, accent: Boolean): Chip {
            return Chip(holder.b.root.context).apply {
                this.text = text
                isClickable = false
                isCheckable = false
                // Compact chip metrics so the tag row stays tight.
                setEnsureMinTouchTargetSize(false)
                chipMinHeight = resources.displayMetrics.density * 26
                if (accent) {
                    setChipBackgroundColorResource(R.color.brand_primary)
                    setTextColor(Color.WHITE)
                }
            }
        }
    }
}
