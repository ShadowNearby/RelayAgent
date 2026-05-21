package com.appagentcards.domain.model

sealed class Selector {
    data class ByAccessibilityId(val id: String) : Selector()
    data class ByResourceId(val id: String) : Selector()
    data class ByText(val text: String) : Selector()
    data class ByTextContains(val substring: String) : Selector()
    data class ByXPath(val xpath: String) : Selector()
    data class ByBounds(
        val box: Rect,
        val anchor: Anchor = Anchor.NONE
    ) : Selector()

    val priority: Int
        get() = when (this) {
            is ByAccessibilityId -> 0
            is ByResourceId -> 1
            is ByText -> 2
            is ByTextContains -> 3
            is ByXPath -> 4
            is ByBounds -> 5
        }
}

enum class Anchor {
    TOP_LEFT, TOP_RIGHT, BOTTOM_LEFT, BOTTOM_RIGHT, CENTER, NONE
}

data class Rect(
    val left: Int,
    val top: Int,
    val right: Int,
    val bottom: Int
) {
    val centerX: Int get() = (left + right) / 2
    val centerY: Int get() = (top + bottom) / 2
    val width: Int get() = right - left
    val height: Int get() = bottom - top
}
