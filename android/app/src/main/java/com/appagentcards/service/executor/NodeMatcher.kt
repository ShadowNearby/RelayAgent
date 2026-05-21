package com.appagentcards.service.executor

import android.view.accessibility.AccessibilityNodeInfo
import com.appagentcards.domain.model.Selector
import javax.inject.Inject

class NodeMatcher @Inject constructor() {

    fun matches(node: AccessibilityNodeInfo, selector: Selector): Boolean {
        return when (selector) {
            is Selector.ByAccessibilityId ->
                node.contentDescription?.toString() == selector.id
            is Selector.ByResourceId ->
                node.viewIdResourceName?.endsWith(selector.id) == true
            is Selector.ByText ->
                node.text?.toString() == selector.text
            is Selector.ByTextContains ->
                node.text?.toString()?.contains(selector.substring) == true
            is Selector.ByXPath ->
                node.className?.toString() == selector.xpath
            is Selector.ByBounds -> true
        }
    }

    fun findClickableAncestor(node: AccessibilityNodeInfo, maxDepth: Int = 5): AccessibilityNodeInfo? {
        var current: AccessibilityNodeInfo? = node.parent
        var depth = 0
        while (current != null && depth < maxDepth) {
            if (current.isClickable) return current
            current = current.parent
            depth++
        }
        return null
    }
}
