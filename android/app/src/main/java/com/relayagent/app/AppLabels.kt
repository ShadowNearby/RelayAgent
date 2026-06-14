package com.relayagent.app

/**
 * Package id -> human launcher label for the manifest apps, plus the short
 * "apps" token used in run dir names (e.g. "tongyi", "bard"). Keeps the log
 * UI readable without shipping the manifests to Kotlin.
 */
object AppLabels {

    private val BY_PACKAGE = mapOf(
        "com.aliyun.tongyi" to "通义千问",
        "com.autonavi.minimap" to "高德地图",
        "ctrip.android.view" to "携程旅行",
        "com.google.android.apps.bard" to "Gemini",
        "com.tencent.mm" to "微信",
        "com.xingin.xhs" to "小红书",
        "cn.wps.moffice_eng" to "WPS",
        "com.booking" to "Booking.com",
        "com.microsoft.copilot" to "Copilot",
        "com.reddit.frontpage" to "Reddit",
    )

    // Tokens that appear in run dir names ("<ts>_plan_<token>_<token>").
    private val BY_TOKEN = mapOf(
        "tongyi" to "通义千问",
        "minimap" to "高德地图",
        "amap" to "高德地图",
        "ctrip" to "携程旅行",
        "bard" to "Gemini",
        "gemini" to "Gemini",
        "wechat" to "微信",
        "mm" to "微信",
        "xhs" to "小红书",
        "wps" to "WPS",
        "booking" to "Booking.com",
        "copilot" to "Copilot",
        "reddit" to "Reddit",
    )

    /** Best-effort label for a package id OR a dir-name token. */
    fun label(idOrToken: String): String =
        BY_PACKAGE[idOrToken] ?: BY_TOKEN[idOrToken] ?: idOrToken
}
