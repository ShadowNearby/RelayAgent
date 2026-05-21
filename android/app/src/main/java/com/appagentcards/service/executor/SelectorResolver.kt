package com.appagentcards.service.executor

import android.graphics.Rect
import android.view.accessibility.AccessibilityNodeInfo
import com.appagentcards.domain.model.*
import kotlinx.coroutines.delay
import kotlinx.coroutines.suspendCancellableCoroutine
import javax.inject.Inject
import kotlin.coroutines.resume

data class ResolvedNode(
    val centerX: Int,
    val centerY: Int,
    val bounds: Rect
) {
    companion object {
        fun fromNode(node: AccessibilityNodeInfo): ResolvedNode {
            val bounds = Rect()
            node.getBoundsInScreen(bounds)
            return ResolvedNode(
                centerX = bounds.centerX(),
                centerY = bounds.centerY(),
                bounds = bounds
            )
        }
    }
}

class SelectorResolver @Inject constructor(
    private val nodeMatcher: NodeMatcher
) {
    suspend fun resolve(
        selector: Selector,
        root: AccessibilityNodeInfo,
        timeoutSeconds: Float = 10f
    ): ResolvedNode? {
        val deadline = System.currentTimeMillis() + (timeoutSeconds * 1000).toLong()

        while (System.currentTimeMillis() < deadline) {
            val result = searchNode(root, selector)
            if (result != null) return result

            val clickable = findClickableForBounds(selector, root)
            if (clickable != null) return clickable

            delay(200)
        }
        return null
    }

    suspend fun resolveWithScroll(
        labelSpec: Step.TapLabel,
        root: AccessibilityNodeInfo
    ): ResolvedNode? {
        val labelKeys = listOf("text", "text_contains", "text_or_desc", "text_or_desc_contains", "accessibility_id")
        // Search in current viewport first
        for (key in labelKeys) {
            val result = searchByText(root, labelSpec.label, labelSpec.matchMode)
            if (result != null) return result
        }
        // Scrolling would require gesture dispatch which needs service reference
        return null
    }

    private fun searchNode(node: AccessibilityNodeInfo, selector: Selector): ResolvedNode? {
        if (nodeMatcher.matches(node, selector)) {
            return processMatch(node)
        }
        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            val result = searchNode(child, selector)
            child.recycle()
            if (result != null) return result
        }
        return null
    }

    private fun searchByText(
        node: AccessibilityNodeInfo,
        text: String,
        mode: LabelMatchMode
    ): ResolvedNode? {
        val nodeText = node.text?.toString() ?: ""
        val nodeDesc = node.contentDescription?.toString() ?: ""

        val matches = when (mode) {
            LabelMatchMode.TEXT -> nodeText == text
            LabelMatchMode.TEXT_CONTAINS -> text in nodeText
            LabelMatchMode.TEXT_OR_DESC -> nodeText == text || nodeDesc == text
            LabelMatchMode.TEXT_OR_DESC_CONTAINS -> text in nodeText || text in nodeDesc
            LabelMatchMode.ACCESSIBILITY_ID -> nodeDesc == text
        }

        if (matches) return processMatch(node)

        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            val result = searchByText(child, text, mode)
            child.recycle()
            if (result != null) return result
        }
        return null
    }

    private fun processMatch(node: AccessibilityNodeInfo): ResolvedNode? {
        return if (node.isClickable) {
            ResolvedNode.fromNode(node)
        } else {
            val ancestor = nodeMatcher.findClickableAncestor(node)
            if (ancestor != null) {
                ResolvedNode.fromNode(ancestor)
            } else {
                ResolvedNode.fromNode(node)
            }
        }
    }

    private fun findClickableForBounds(
        selector: Selector,
        root: AccessibilityNodeInfo
    ): ResolvedNode? {
        if (selector !is Selector.ByBounds) return null
        val bounds = Rect(selector.box.left, selector.box.top, selector.box.right, selector.box.bottom)
        return ResolvedNode(centerX = bounds.centerX(), centerY = bounds.centerY(), bounds = bounds)
    }
}
