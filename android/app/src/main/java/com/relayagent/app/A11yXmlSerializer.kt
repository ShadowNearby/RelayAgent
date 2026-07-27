package com.relayagent.app

import android.graphics.Rect
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityWindowInfo

/**
 * Serializes the accessibility tree in **uiautomator dump format**, so the
 * Python XML consumers (tap_text grounding, reply-text extraction, text-hash
 * done detection, permission-popup scan in agents/agent/relay_agent.py) parse it
 * unchanged. uiautomator dump is itself built on AccessibilityNodeInfo, so
 * parity is structural, not approximated.
 *
 * Format rules mirrored from AOSP AccessibilityNodeInfoDumper:
 *   - one <node> per visible-to-user node, attributes in the canonical order
 *     (index class package text content-desc resource-id ... bounds)
 *   - bounds as "[l,t][r,b]", clipped to the screen
 *   - index = position among the parent's children
 *   - skip nodes not visibleToUser
 *
 * Difference (deliberate): uiautomator dumps only the active window; we emit
 * every interactive window in z-order under one <hierarchy>, so system
 * dialogs (permission popups) and the app window coexist in one dump — the
 * Python side filters by the node `package` attribute already.
 *
 * Parity is pinned by Spike B (host script diffing this output against
 * `adb shell uiautomator dump` on the same screens — see android/README.md).
 */
object A11yXmlSerializer {

    fun serialize(
        windows: List<AccessibilityWindowInfo>,
        fallbackRoot: AccessibilityNodeInfo?,
    ): String {
        val sb = StringBuilder(16 * 1024)
        sb.append("<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>")
        sb.append("<hierarchy rotation=\"0\">")
        var emitted = false
        // z-order: windows list is top-first; emit bottom-first so the
        // topmost (active dialog) lands last, like layered dumps read best.
        for (w in windows.reversed()) {
            if (w.type != AccessibilityWindowInfo.TYPE_APPLICATION &&
                w.type != AccessibilityWindowInfo.TYPE_SYSTEM
            ) continue
            val root = w.root ?: continue
            emitNode(sb, root, index = 0)
            emitted = true
        }
        if (!emitted && fallbackRoot != null) {
            emitNode(sb, fallbackRoot, index = 0)
        }
        sb.append("</hierarchy>")
        return sb.toString()
    }

    private fun emitNode(sb: StringBuilder, node: AccessibilityNodeInfo, index: Int) {
        if (!node.isVisibleToUser) return
        val bounds = Rect()
        node.getBoundsInScreen(bounds)
        sb.append("<node index=\"").append(index).append('"')
        attr(sb, "text", node.text)
        attr(sb, "resource-id", node.viewIdResourceName)
        attr(sb, "class", node.className)
        attr(sb, "package", node.packageName)
        attr(sb, "content-desc", node.contentDescription)
        bool(sb, "checkable", node.isCheckable)
        bool(sb, "checked", node.isChecked)
        bool(sb, "clickable", node.isClickable)
        bool(sb, "enabled", node.isEnabled)
        bool(sb, "focusable", node.isFocusable)
        bool(sb, "focused", node.isFocused)
        bool(sb, "scrollable", node.isScrollable)
        bool(sb, "long-clickable", node.isLongClickable)
        bool(sb, "password", node.isPassword)
        bool(sb, "selected", node.isSelected)
        sb.append(" bounds=\"[").append(bounds.left).append(',').append(bounds.top)
            .append("][").append(bounds.right).append(',').append(bounds.bottom)
            .append("]\"")
        val count = node.childCount
        if (count == 0) {
            sb.append(" />")
            return
        }
        sb.append('>')
        for (i in 0 until count) {
            val child = node.getChild(i) ?: continue
            try {
                emitNode(sb, child, i)
            } finally {
                @Suppress("DEPRECATION")
                child.recycle()
            }
        }
        sb.append("</node>")
    }

    private fun attr(sb: StringBuilder, name: String, value: CharSequence?) {
        sb.append(' ').append(name).append("=\"")
            .append(escape(value?.toString() ?: "")).append('"')
    }

    private fun bool(sb: StringBuilder, name: String, value: Boolean) {
        sb.append(' ').append(name).append("=\"").append(value).append('"')
    }

    private fun escape(s: String): String {
        if (s.none { it == '&' || it == '<' || it == '>' || it == '"' || it == '\'' || it.code < 0x20 }) {
            return s
        }
        val sb = StringBuilder(s.length + 16)
        for (c in s) {
            when {
                c == '&' -> sb.append("&amp;")
                c == '<' -> sb.append("&lt;")
                c == '>' -> sb.append("&gt;")
                c == '"' -> sb.append("&quot;")
                c == '\'' -> sb.append("&apos;")
                // Whitespace controls must be character references: a literal
                // LF/TAB/CR in an attribute value is normalized to a space by
                // conforming parsers (ElementTree, which backend.dump_ui_tree
                // uses), while uiautomator's kxml2 serializer emits
                // &#10;/&#9;/&#13; and thus preserves them — without this,
                // multi-line node text (single-bubble long replies) loses its
                // line breaks and Spike B parity.
                c == '\n' -> sb.append("&#10;")
                c == '\t' -> sb.append("&#9;")
                c == '\r' -> sb.append("&#13;")
                c.code < 0x20 -> {} // strip remaining control chars
                else -> sb.append(c)
            }
        }
        return sb.toString()
    }
}
