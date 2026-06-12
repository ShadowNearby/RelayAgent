package com.relayagent.app

import android.content.Context
import android.util.Log
import java.io.File

/**
 * Extracts the bundled data assets (manifests/*.yaml + the capability
 * matrix CSV, synced from the repo at build time) into filesDir/relay/ so
 * the Python runtime reads them as ordinary files. Re-extracts when the app
 * version changes.
 */
object AssetInstaller {

    private const val TAG = "RelayAssets"
    private const val PREFS = "relay_assets"
    private const val KEY_VERSION = "installed_version"

    fun ensureInstalled(context: Context) {
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val current = context.packageManager
            .getPackageInfo(context.packageName, 0).longVersionCode
        if (prefs.getLong(KEY_VERSION, -1) == current &&
            File(context.filesDir, "relay").isDirectory
        ) return
        copyAssetDir(context, "relay", File(context.filesDir, "relay"))
        prefs.edit().putLong(KEY_VERSION, current).apply()
        Log.i(TAG, "assets installed (version $current)")
    }

    private fun copyAssetDir(context: Context, assetPath: String, dest: File) {
        val names = context.assets.list(assetPath) ?: return
        if (names.isEmpty()) return // a file, not a dir — handled by caller
        dest.mkdirs()
        for (name in names) {
            val childAsset = "$assetPath/$name"
            val childNames = context.assets.list(childAsset)
            val childDest = File(dest, name)
            if (childNames.isNullOrEmpty()) {
                context.assets.open(childAsset).use { input ->
                    childDest.outputStream().use { input.copyTo(it) }
                }
            } else {
                copyAssetDir(context, childAsset, childDest)
            }
        }
    }
}
