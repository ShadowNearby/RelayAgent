package com.appagentcards.service.executor

import android.view.accessibility.AccessibilityNodeInfo
import javax.inject.Inject

class BlockerDismisser @Inject constructor(
    private val nodeMatcher: NodeMatcher
) {
    private val skipLabels = listOf("跳过", "关闭", "取消", "我知道了", "确定")

    fun dismissCommonBlockers(root: AccessibilityNodeInfo): Boolean {
        var dismissed = false
        for (label in skipLabels) {
            val node = findNodeByText(root, label)
            if (node != null && node.isClickable) {
                node.performAction(AccessibilityNodeInfo.ACTION_CLICK)
                node.recycle()
                dismissed = true
            }
        }
        return dismissed
    }

    private fun findNodeByText(node: AccessibilityNodeInfo, text: String): AccessibilityNodeInfo? {
        if (node.text?.toString() == text) {
            return AccessibilityNodeInfo.obtain(node)
        }
        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            val result = findNodeByText(child, text)
            child.recycle()
            if (result != null) return result
        }
        return null
    }
}
