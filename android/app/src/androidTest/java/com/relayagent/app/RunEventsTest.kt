package com.relayagent.app

import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.util.concurrent.CountDownLatch
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.TimeUnit

/**
 * RunEvents.dispatch parses emit_status JSON from the Python runtime and
 * delivers typed events on the main thread; unknown/malformed payloads are
 * dropped silently.
 */
@RunWith(AndroidJUnit4::class)
class RunEventsTest {

    @After
    fun clearListener() {
        RunEvents.listener = null
    }

    /** Dispatch the payloads, then wait until [expect] events arrived. */
    private fun collect(expect: Int, vararg payloads: String): List<RunEvents.Event> {
        val got = CopyOnWriteArrayList<RunEvents.Event>()
        val latch = CountDownLatch(expect)
        RunEvents.listener = {
            got.add(it)
            latch.countDown()
        }
        payloads.forEach { RunEvents.dispatch(it) }
        // A trailing marker event proves the queue drained even when the
        // payloads under test are expected to be dropped.
        RunEvents.post(RunEvents.Event.LegEnd("~drain~"))
        assertTrue("main-thread delivery timed out", latch.await(5, TimeUnit.SECONDS))
        return got
    }

    @Test
    fun legStartParsesWithApp() {
        val events = collect(
            2, """{"event":"leg_start","id":"01_navigate","app":"com.autonavi.minimap"}"""
        )
        assertEquals(
            RunEvents.Event.LegStart("01_navigate", "com.autonavi.minimap"), events[0]
        )
    }

    @Test
    fun legStartWithoutAppMapsToNull() {
        val events = collect(2, """{"event":"leg_start","id":"02_x"}""")
        assertEquals(RunEvents.Event.LegStart("02_x", null), events[0])
        assertNull((events[0] as RunEvents.Event.LegStart).app)
    }

    @Test
    fun stepParsesAndFlattensThought() {
        val events = collect(
            2, """{"event":"step","step":3,"action_type":"click","thought":"line1\nline2"}"""
        )
        assertEquals(RunEvents.Event.Step(3, "click", "line1 line2"), events[0])
    }

    @Test
    fun legEndParses() {
        val events = collect(2, """{"event":"leg_end","id":"01_navigate"}""")
        assertEquals(RunEvents.Event.LegEnd("01_navigate"), events[0])
    }

    @Test
    fun unknownAndMalformedPayloadsAreDropped() {
        val events = collect(
            1,
            """{"event":"telemetry","id":"nope"}""",
            "not json at all",
            """{"no_event_key":1}""",
        )
        // Only the drain marker arrives.
        assertEquals(listOf<RunEvents.Event>(RunEvents.Event.LegEnd("~drain~")), events)
    }
}
