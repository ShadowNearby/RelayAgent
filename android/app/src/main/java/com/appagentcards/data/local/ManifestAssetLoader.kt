package com.appagentcards.data.local

import android.content.Context
import dagger.hilt.android.qualifiers.ApplicationContext
import java.io.IOException
import javax.inject.Inject
import javax.inject.Singleton

@Singleton
class ManifestAssetLoader @Inject constructor(
    @ApplicationContext private val context: Context
) {
    fun loadAll(): List<String> {
        return try {
            context.assets
                .list("manifests")!!
                .filter { it.endsWith(".yaml") || it.endsWith(".yml") }
                .sorted()
                .map { filename ->
                    context.assets.open("manifests/$filename")
                        .bufferedReader()
                        .use { it.readText() }
                }
        } catch (e: IOException) {
            emptyList()
        }
    }

    fun loadSchema(): String {
        return context.assets
            .open("manifests/schema.json")
            .bufferedReader()
            .use { it.readText() }
    }
}
