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

    /** Idempotent CPython bring-up. The FIRST start loads the interpreter +
     * stdlib/AssetFinder (1s+ on a real device), so it must stay OFF the main
     * thread — runFlow/runSingle run it inside the worker executor before the
     * entrypoint call. @Synchronized guards a direct caller on another thread
     * (e.g. instrumentation) against a double Python.start race. */
    @Synchronized
    fun ensureStarted(context: Context) {
        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(context.applicationContext))
            Log.i(TAG, "CPython started")
        }
    }

    /**
     * Run the full NL flow (relay_android.entry.run_flow) off the main
     * thread. `config` carries the LLM endpoint + knobs from Settings;
     * onDone receives the result JSON (or {"error": ...}).
     */
    fun runFlow(context: Context, goal: String, config: JSONObject, onDone: (String) -> Unit) {
        val app = context.applicationContext
        DeviceBridge.resetStop()
        executor.execute {
            val result = try {
                // First-call interpreter bring-up happens here on the worker
                // thread, not on the UI thread the send button clicked from.
                ensureStarted(app)
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
     * on-device `python -m agents.runtime.native_runner <pkg> <goal>`. */
    fun runSingle(
        context: Context, pkg: String, goal: String, config: JSONObject,
        onDone: (String) -> Unit,
    ) {
        val app = context.applicationContext
        DeviceBridge.resetStop()
        executor.execute {
            val result = try {
                ensureStarted(app)
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
