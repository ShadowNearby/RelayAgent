package com.relayagent.app

import android.graphics.BitmapFactory
import android.os.Bundle
import android.view.View
import androidx.appcompat.app.AppCompatActivity
import com.relayagent.app.databinding.ActivityLogDetailBinding
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * Render one trajectory-log file: images (step_*.png) into an ImageView,
 * everything else as text (pretty-printed when it parses as JSON).
 */
class LogDetailActivity : AppCompatActivity() {

    companion object {
        const val EXTRA_FILE = "file"
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val ui = ActivityLogDetailBinding.inflate(layoutInflater)
        setContentView(ui.root)
        ui.toolbar.setNavigationOnClickListener { finish() }

        val path = intent.getStringExtra(EXTRA_FILE)
        val file = path?.let { File(it) }
        ui.toolbar.title = file?.name ?: getString(R.string.title_logs)

        if (file == null || !file.exists()) {
            ui.text.text = "文件不存在"
            return
        }

        val name = file.name.lowercase()
        if (name.endsWith(".png") || name.endsWith(".jpg") || name.endsWith(".jpeg")) {
            val bmp = BitmapFactory.decodeFile(file.absolutePath)
            if (bmp != null) {
                ui.textScroll.visibility = View.GONE
                ui.image.visibility = View.VISIBLE
                ui.image.setImageBitmap(bmp)
                return
            }
        }
        ui.text.text = renderText(file)
    }

    private fun renderText(file: File): String {
        val raw = try {
            file.readText()
        } catch (e: Exception) {
            return "无法读取：$e"
        }
        val trimmed = raw.trimStart()
        return try {
            when {
                trimmed.startsWith("{") -> JSONObject(raw).toString(2)
                trimmed.startsWith("[") -> JSONArray(raw).toString(2)
                else -> raw
            }
        } catch (e: Exception) {
            raw // not valid JSON after all — show as-is
        }
    }
}
