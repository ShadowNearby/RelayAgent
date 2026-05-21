package com.appagentcards.data.remote

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.IOException
import javax.inject.Inject
import javax.inject.Singleton

@Singleton
class ManifestRemoteDataSource @Inject constructor(
    private val okHttpClient: OkHttpClient
) {
    companion object {
        private const val BASE_URL = "https://raw.githubusercontent.com/AppAgentCards/AppAgentCards/main"
        private val KNOWN_APP_IDS = listOf(
            "com.autonavi.minimap",
            "com.aliyun.tongyi",
            "ctrip.android.view",
            "com.xingin.xhs",
            "com.taobao.taobao"
        )
    }

    suspend fun fetchAll(): List<String> = withContext(Dispatchers.IO) {
        KNOWN_APP_IDS.mapNotNull { appId ->
            try {
                fetchManifest(appId)
            } catch (e: IOException) {
                null
            }
        }
    }

    suspend fun fetchManifest(appId: String): String = withContext(Dispatchers.IO) {
        val url = "$BASE_URL/manifests/$appId.yaml"
        val request = Request.Builder().url(url).build()
        val response = okHttpClient.newCall(request).execute()
        if (!response.isSuccessful) {
            throw IOException("Failed to fetch $appId: ${response.code}")
        }
        response.body?.string() ?: throw IOException("Empty body for $appId")
    }
}
