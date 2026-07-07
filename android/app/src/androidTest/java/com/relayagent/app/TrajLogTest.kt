package com.relayagent.app

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/**
 * TrajLog parses the on-device trajectory layout written by
 * relay_android.entry + the flow runner; a synthetic run exercises the happy
 * path and the documented best-effort degradation on malformed files.
 */
@RunWith(AndroidJUnit4::class)
class TrajLogTest {

    private lateinit var root: File

    @Before
    fun setUp() {
        val ctx = ApplicationProvider.getApplicationContext<Context>()
        root = File(ctx.cacheDir, "traj_test_${System.currentTimeMillis()}")
        root.mkdirs()
    }

    @After
    fun tearDown() {
        root.deleteRecursively()
    }

    private fun writeRun(): File {
        val run = File(root, "20260707_120000_plan_amap_qianwen").apply { mkdirs() }
        File(run, "meta.json").writeText(
            """{"request":"导航回家","created_at":"2026-07-07T12:00:00","kind":"flow"}"""
        )
        val leg = File(run, "01_navigate").apply { mkdirs() }
        File(leg, "summary.json").writeText(
            """{"steps":3,"last_action_type":"status","last_goal_status":"complete","token_usage":{"total_tokens":1234}}"""
        )
        File(leg, "wall_clock.json").writeText("""{"wall_s":42.5,"phase":"task"}""")
        File(leg, "agent_reply.json").writeText(
            """{"reply":"已开始导航","target_app":"com.autonavi.minimap"}"""
        )
        File(leg, "leg_verdict.json").writeText(
            """{"app":"com.autonavi.minimap","capability":"navigate_to","status":"complete","reason":"ok"}"""
        )
        val steps = File(leg, "steps").apply { mkdirs() }
        File(steps, "step_1.png").writeBytes(byteArrayOf(1))
        File(steps, "steps.json").writeText(
            """[{"step":1,"action_type":"click","action":{"action_type":"click","x":10,"y":20},"click":[10,20],"thought":"tap the button","screenshot":"step_1.png"}]"""
        )
        return run
    }

    @Test
    fun parsesARunEndToEnd() {
        writeRun()

        val runs = TrajLog.listRuns(root)
        assertEquals(1, runs.size)
        val run = runs[0]
        assertEquals("导航回家", run.request)
        assertEquals("flow", run.kind)
        assertEquals(listOf("amap", "qianwen"), run.apps)
        assertEquals(1, run.legCount)

        val leg = TrajLog.parseLeg(TrajLog.legDirs(run.dir)[0])
        assertEquals("com.autonavi.minimap", leg.app)
        assertEquals("navigate_to", leg.capability)
        assertEquals(3, leg.steps)
        assertEquals(42.5, leg.wallSeconds!!, 1e-9)
        assertEquals("complete", leg.goalStatus)
        assertEquals("已开始导航", leg.reply)
        assertEquals("complete", leg.verdictStatus)
        assertEquals(1234, leg.totalTokens)

        val steps = TrajLog.parseSteps(leg.dir)
        assertEquals(1, steps.size)
        assertEquals("click", steps[0].actionType)
        assertArrayEquals(intArrayOf(10, 20), steps[0].click)
        assertEquals("tap the button", steps[0].thought)
        assertNotNull("screenshot file should resolve", steps[0].screenshot)
        assertNull("no marked frame was written", steps[0].marked)
    }

    @Test
    fun malformedFilesDegradeGracefully() {
        val run = File(root, "20260707_130000_plan_x").apply { mkdirs() }
        File(run, "meta.json").writeText("{not json")
        val leg = File(run, "01_x").apply { mkdirs() }
        File(leg, "summary.json").writeText("also not json")
        File(leg, "steps").mkdirs()
        File(leg, "steps/steps.json").writeText("42")

        val runs = TrajLog.listRuns(root)
        assertEquals(1, runs.size)
        assertNull(runs[0].request)

        val parsed = TrajLog.parseLeg(leg)
        assertEquals(0, parsed.steps) // falls back to counting step pngs
        assertNull(parsed.goalStatus)
        assertTrue(TrajLog.parseSteps(leg).isEmpty())
    }

    @Test
    fun dirsWithoutLegsAreHidden() {
        File(root, "20260707_140000_plan_empty").mkdirs()
        assertTrue(TrajLog.listRuns(root).isEmpty())
    }
}
