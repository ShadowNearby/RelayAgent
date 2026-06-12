package com.relayagent.app

import android.content.Context
import android.util.Log
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import org.json.JSONObject
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

/**
 * Owns the embedded CPython interpreter and the single worker thread all
 * Python entrypoints run on (the runtime is sequential by design: one task,
 * one obs->predict->execute loop).
 */
object PythonRuntime {

    private const val TAG = "RelayPython"
    private val executor: ExecutorService = Executors.newSingleThreadExecutor {
        Thread(it, "relay-python").apply { isDaemon = true }
    }

    fun ensureStarted(context: Context) {
        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(context))
            Log.i(TAG, "CPython started")
        }
    }

    /**
     * Run the full NL flow (relay_android.entry.run_flow) off the main
     * thread. `config` carries the LLM endpoint + knobs from Settings;
     * onDone receives the result JSON (or {"error": ...}).
     */
    fun runFlow(context: Context, goal: String, config: JSONObject, onDone: (String) -> Unit) {
        ensureStarted(context)
        DeviceBridge.resetStop()
        executor.execute {
            val result = try {
                Python.getInstance()
                    .getModule("relay_android.entry")
                    .callAttr("run_flow", goal, config.toString())
                    .toString()
            } catch (e: Exception) {
                Log.e(TAG, "run_flow crashed", e)
                JSONObject().put("error", e.toString()).toString()
            }
            onDone(result)
        }
    }

    /** Single-app debug entry (relay_android.entry.run_single) — the
     * on-device `python -m agents.native_runner <pkg> <goal>`. */
    fun runSingle(
        context: Context, pkg: String, goal: String, config: JSONObject,
        onDone: (String) -> Unit,
    ) {
        ensureStarted(context)
        DeviceBridge.resetStop()
        executor.execute {
            val result = try {
                Python.getInstance()
                    .getModule("relay_android.entry")
                    .callAttr("run_single", pkg, goal, config.toString())
                    .toString()
            } catch (e: Exception) {
                Log.e(TAG, "run_single crashed", e)
                JSONObject().put("error", e.toString()).toString()
            }
            onDone(result)
        }
    }
}
