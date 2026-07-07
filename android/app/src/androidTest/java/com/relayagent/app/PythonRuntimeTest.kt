package com.relayagent.app

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.chaquo.python.Kwarg
import com.chaquo.python.PyException
import com.chaquo.python.Python
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/**
 * On-device checks for the embedded CPython runtime: the packaged agents/
 * code must import through the same chain entry.run_flow uses, and the data
 * assets extracted by AssetInstaller must build a usable catalog + matrix.
 * No LLM endpoint or accessibility service is needed.
 */
@RunWith(AndroidJUnit4::class)
class PythonRuntimeTest {

    private val context: Context = ApplicationProvider.getApplicationContext()

    private fun py(): Python {
        PythonRuntime.ensureStarted(context)
        return Python.getInstance()
    }

    @Test
    fun bootsAndImportsTheFlowImportChain() {
        val py = py()
        listOf(
            "agents.agent.action_model",
            "agents.routing.card_loader",
            "agents.routing.card_catalog",
            "agents.routing.capability_matrix_router",
            "agents.flow.nl_flow",
            "agents.flow.flow_planner",
            "agents.flow.flow_runner",
            "agents.llm.llm_client",
            "agents.runtime.native_runner",
            "relay_android.entry",
            "relay_android.backend",
            "relay_android.interaction",
        ).forEach { mod ->
            assertNotNull("import $mod returned null", py.getModule(mod))
        }
    }

    @Test
    fun jsonActionMirrorsHostPinnedBehavior() {
        // A slice of tests/test_action_model.py, re-run inside Chaquopy: the
        // pure-Python JSONAction must behave the same without pydantic wheels.
        val m = py().getModule("agents.agent.action_model")

        val a = m.callAttr(
            "JSONAction", Kwarg("action_type", "click"), Kwarg("x", 100), Kwarg("y", 200)
        )
        assertEquals("click", a.get("action_type").toString())
        assertEquals(100, a.get("x")!!.toInt())
        assertEquals(200, a.get("y")!!.toInt())

        val r = m.callAttr(
            "JSONAction", Kwarg("action_type", "click"), Kwarg("x", 10.6), Kwarg("y", 20.4)
        )
        assertEquals(11, r.get("x")!!.toInt())
        assertEquals(20, r.get("y")!!.toInt())

        try {
            m.callAttr("JSONAction", Kwarg("action_type", "bogus"))
            fail("invalid action_type should raise ValueError")
        } catch (e: PyException) {
            assertTrue(e.message ?: "", (e.message ?: "").contains("Invalid action type"))
        }

        try {
            m.callAttr(
                "JSONAction", Kwarg("action_type", "scroll"), Kwarg("direction", "sideways")
            )
            fail("invalid direction should raise ValueError")
        } catch (e: PyException) {
            assertTrue(e.message ?: "", (e.message ?: "").contains("Invalid scroll direction"))
        }
    }

    @Test
    fun catalogAndMatrixBuildFromInstalledAssets() {
        AssetInstaller.ensureInstalled(context)
        val relay = File(context.filesDir, "relay")
        val py = py()
        fun path(f: File) = py.getModule("pathlib").callAttr("Path", f.absolutePath)

        val catalog = py.getModule("agents.routing.card_catalog")
            .callAttr("build_catalog", path(File(relay, "manifests")))
        val apps = catalog.callAttr("get", "apps")!!.asList()
        assertTrue("catalog has no apps", apps.isNotEmpty())
        var capCount = 0
        val manifestIds = apps.map { app ->
            assertNotNull(app.callAttr("get", "app_id"))
            capCount += app.callAttr("get", "capabilities")!!.asList().size
            app.callAttr("get", "app_id").toString()
        }
        assertTrue("catalog has no capabilities", capCount > 0)

        val matrix = py.getModule("agents.routing.capability_matrix_router")
            .callAttr("load_matrix", path(File(relay, "app_capability_matrix.csv")))
        val appIds = matrix.callAttr("get", "app_ids")!!.asList().map { it.toString() }
        assertTrue("matrix has no app columns", appIds.isNotEmpty())
        assertTrue(
            "matrix caps empty",
            matrix.callAttr("get", "cap_to_apps")!!.callAttr("__len__").toInt() > 0,
        )
        // Both ship from the same repo — they must agree on at least one app.
        assertTrue(
            "matrix apps $appIds share none with manifests $manifestIds",
            appIds.any { it in manifestIds },
        )
    }

    @Test
    fun deviceBridgeIsReachableFromPython() {
        // relay_android.entry resolves jclass(DeviceBridge) at import time and
        // reads filesDir through it — the same seam the real flow uses.
        val filesDir = py().getModule("relay_android.entry").callAttr("_files_dir").toString()
        assertEquals(context.filesDir.absolutePath, filesDir)

        // Pure bridge call needing no accessibility service.
        val size = DeviceBridge.screenSize()
        assertTrue("bogus screen size ${size.toList()}", size[0] > 0 && size[1] > 0)
    }
}
