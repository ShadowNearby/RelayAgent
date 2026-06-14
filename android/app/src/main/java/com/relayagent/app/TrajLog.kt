package com.relayagent.app

import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * Parses the on-device trajectory-log layout into structured run / leg / step
 * objects so the log UI can render a real run viewer instead of a file tree.
 *
 * Layout (per run, written by relay_android.entry + the flow runner):
 *
 *   traj_logs/<ts>_plan_<apps>/
 *     meta.json                 # {request, created_at, kind, app?, error?}
 *     token_usage.json          # optional, flow-level token accounting
 *     NN_<legId>/
 *       summary.json            # {steps, last_action_type, last_goal_status, token_usage}
 *       wall_clock.json         # {wall_s, phase}
 *       agent_reply.json        # optional {reply, target_app}
 *       leg_verdict.json        # optional {app, capability, status, score, reason}
 *       traj.json               # raw agent record
 *       steps/steps.json        # [ {step, action_type, action, click, thought, screenshot, marked_screenshot, ts} ]
 *       steps/step_*.png
 *
 * Every field is best-effort: missing or malformed files degrade gracefully.
 */
object TrajLog {

    data class Run(
        val dir: File,
        val request: String?,
        val createdAt: String?,
        val kind: String?,
        val error: String?,
        val apps: List<String>,
        val legCount: Int,
    )

    data class Leg(
        val dir: File,
        val id: String,
        val app: String?,
        val capability: String?,
        val steps: Int,
        val wallSeconds: Double?,
        val goalStatus: String?,
        val lastAction: String?,
        val reply: String?,
        val verdictStatus: String?,
        val verdictReason: String?,
        val totalTokens: Int?,
    )

    data class Step(
        val n: Int,
        val actionType: String,
        val action: JSONObject?,
        val click: IntArray?,
        val thought: String,
        val screenshot: File?,
        val marked: File?,
    )

    fun trajRoot(filesDir: File): File = File(filesDir, "traj_logs")

    /** Runs newest-first. A run is any top-level dir holding at least one leg. */
    fun listRuns(root: File): List<Run> {
        val dirs = root.listFiles()?.filter { it.isDirectory } ?: emptyList()
        return dirs.sortedByDescending { it.name }
            .map { parseRun(it) }
            .filter { it.legCount > 0 }
    }

    private fun parseRun(dir: File): Run {
        val meta = readJson(File(dir, "meta.json"))
        return Run(
            dir = dir,
            request = meta?.optStringOrNull("request"),
            createdAt = meta?.optStringOrNull("created_at"),
            kind = meta?.optStringOrNull("kind"),
            error = meta?.optStringOrNull("error"),
            apps = appsFromName(dir.name),
            legCount = legDirs(dir).size,
        )
    }

    /** Apps encoded in the run dir name: "<ts>_plan_<a>_<b>" -> [a, b]. */
    private fun appsFromName(name: String): List<String> {
        val marker = "_plan_"
        val i = name.indexOf(marker)
        if (i < 0) return emptyList()
        return name.substring(i + marker.length).split("_").filter { it.isNotEmpty() }
    }

    fun legDirs(run: File): List<File> {
        val children = run.listFiles()?.filter { it.isDirectory } ?: emptyList()
        return children
            .filter {
                File(it, "summary.json").exists() ||
                    File(it, "traj.json").exists() ||
                    File(it, "steps").isDirectory
            }
            .sortedBy { it.name }
    }

    fun parseLeg(dir: File): Leg {
        val summary = readJson(File(dir, "summary.json"))
        val wall = readJson(File(dir, "wall_clock.json"))
        val reply = readJson(File(dir, "agent_reply.json"))
        val verdict = readJson(File(dir, "leg_verdict.json"))
        val tokens = summary?.optJSONObject("token_usage")?.optInt("total_tokens", -1)
            ?.takeIf { it >= 0 }
        return Leg(
            dir = dir,
            id = dir.name,
            app = verdict?.optStringOrNull("app") ?: reply?.optStringOrNull("target_app"),
            capability = verdict?.optStringOrNull("capability"),
            steps = summary?.optInt("steps", -1)?.takeIf { it >= 0 } ?: stepsCount(dir),
            wallSeconds = wall?.optDoubleOrNull("wall_s"),
            goalStatus = summary?.optStringOrNull("last_goal_status"),
            lastAction = summary?.optStringOrNull("last_action_type"),
            reply = reply?.optStringOrNull("reply"),
            verdictStatus = verdict?.optStringOrNull("status"),
            verdictReason = verdict?.optStringOrNull("reason"),
            totalTokens = tokens,
        )
    }

    fun parseSteps(legDir: File): List<Step> {
        val stepsDir = File(legDir, "steps")
        val arr = readJsonArray(File(stepsDir, "steps.json")) ?: return emptyList()
        return (0 until arr.length()).mapNotNull { i ->
            val o = arr.optJSONObject(i) ?: return@mapNotNull null
            val clickArr = o.optJSONArray("click")
            val click = if (clickArr != null && clickArr.length() >= 2)
                intArrayOf(clickArr.optInt(0), clickArr.optInt(1)) else null
            Step(
                n = o.optInt("step", i + 1),
                actionType = o.optString("action_type", "?"),
                action = o.optJSONObject("action"),
                click = click,
                thought = o.optString("thought", ""),
                screenshot = fileOrNull(stepsDir, o.optStringOrNull("screenshot")),
                marked = fileOrNull(stepsDir, o.optStringOrNull("marked_screenshot")),
            )
        }
    }

    private fun stepsCount(legDir: File): Int =
        File(legDir, "steps").listFiles { f -> f.name.endsWith(".png") && !f.name.contains("marked") }
            ?.size ?: 0

    // --- small JSON helpers --------------------------------------------------

    private fun readJson(f: File): JSONObject? = try {
        if (f.exists()) JSONObject(f.readText()) else null
    } catch (e: Exception) {
        null
    }

    private fun readJsonArray(f: File): JSONArray? = try {
        if (f.exists()) JSONArray(f.readText()) else null
    } catch (e: Exception) {
        null
    }

    private fun fileOrNull(dir: File, name: String?): File? =
        name?.let { File(dir, it) }?.takeIf { it.exists() }

    private fun JSONObject.optStringOrNull(key: String): String? =
        if (has(key) && !isNull(key)) optString(key).takeIf { it.isNotEmpty() } else null

    private fun JSONObject.optDoubleOrNull(key: String): Double? =
        if (has(key) && !isNull(key)) optDouble(key).takeIf { !it.isNaN() } else null
}
