package com.appagentcards.domain.model

sealed class Step {
    data class Tap(val selector: Selector) : Step()
    data class TapLabel(
        val label: String,
        val matchMode: LabelMatchMode,
        val timeoutSeconds: Float = 6f,
        val scrollAttempts: Int = 3,
        val required: Boolean = true
    ) : Step()
    data class TapScreenFraction(
        val xRatio: Float,
        val yRatio: Float,
        val label: String? = null
    ) : Step()
    data class Swipe(
        val fromX: Int,
        val fromY: Int,
        val toX: Int,
        val toY: Int,
        val durationMs: Int = 300
    ) : Step()
    data class Wait(val duration: WaitDuration) : Step()
}

sealed class WaitDuration {
    data class Milliseconds(val ms: Int) : WaitDuration()
    data class UntilSelector(
        val selector: Selector,
        val timeoutSeconds: Float = 10f
    ) : WaitDuration()
}

enum class LabelMatchMode {
    TEXT,
    TEXT_CONTAINS,
    TEXT_OR_DESC,
    TEXT_OR_DESC_CONTAINS,
    ACCESSIBILITY_ID
}
