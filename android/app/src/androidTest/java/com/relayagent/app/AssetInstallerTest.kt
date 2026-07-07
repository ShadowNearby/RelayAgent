package com.relayagent.app

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/**
 * The build-time asset sync (manifests + capability matrix from the repo
 * root) must land in filesDir/relay/ where entry._install_env points the
 * Python runtime.
 */
@RunWith(AndroidJUnit4::class)
class AssetInstallerTest {

    private val context: Context = ApplicationProvider.getApplicationContext()

    @Test
    fun installsManifestsAndMatrix() {
        AssetInstaller.ensureInstalled(context)
        val relay = File(context.filesDir, "relay")

        val yamls = File(relay, "manifests")
            .listFiles { f -> f.name.endsWith(".yaml") } ?: emptyArray()
        assertTrue("no manifest yaml extracted under $relay/manifests", yamls.isNotEmpty())
        yamls.forEach { assertTrue("${it.name} is empty", it.length() > 0) }

        val matrix = File(relay, "app_capability_matrix.csv")
        assertTrue("capability matrix missing/empty", matrix.isFile && matrix.length() > 0)
    }

    @Test
    fun reinstallForSameVersionIsANoOp() {
        AssetInstaller.ensureInstalled(context)
        val matrix = File(context.filesDir, "relay/app_capability_matrix.csv")
        assertTrue(matrix.isFile)
        val before = matrix.lastModified()
        AssetInstaller.ensureInstalled(context) // same versionCode → skip copy
        assertEquals(before, matrix.lastModified())
    }
}
