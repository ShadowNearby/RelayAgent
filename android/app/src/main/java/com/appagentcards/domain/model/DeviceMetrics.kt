package com.appagentcards.domain.model

data class DeviceMetrics(
    val resolutionPx: Pair<Int, Int>,
    val densityDpi: Int
) {
    val widthPx: Int get() = resolutionPx.first
    val heightPx: Int get() = resolutionPx.second

    companion object {
        fun fromCurrentDevice(
            widthPx: Int,
            heightPx: Int,
            densityDpi: Int
        ): DeviceMetrics = DeviceMetrics(widthPx to heightPx, densityDpi)
    }
}
