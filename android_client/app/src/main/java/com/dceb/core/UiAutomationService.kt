package com.dceb.core

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.Intent
import android.graphics.Path
import android.graphics.Rect
import android.os.Build
import android.view.accessibility.AccessibilityNodeInfo

/**
 * UI automation on the OWNER's own device: read the screen tree, tap by text
 * or coordinates, swipe, open apps. Every command arrives only through the
 * explicit Approve gate in AiViewModel — nothing runs silently.
 */
class UiAutomationService : AccessibilityService() {

    companion object {
        @Volatile
        var instance: UiAutomationService? = null

        fun isEnabled(): Boolean = instance != null

        /** Read the current screen as a compact text tree (own device only). */
        fun readScreen(): String {
            val svc = instance ?: return "accessibility service not enabled"
            val root = svc.rootInActiveWindow ?: return "no active window"
            val sb = StringBuilder()
            fun walk(node: AccessibilityNodeInfo, depth: Int) {
                if (depth > 12 || sb.length > 8000) return
                val txt = listOfNotNull(
                    node.text?.toString(),
                    node.contentDescription?.toString(),
                    if (node.className?.contains("Button") == true) "[button]" else null,
                    if (node.isClickable) "[clickable]" else null
                ).joinToString(" ")
                if (txt.isNotBlank()) sb.append("  ".repeat(depth)).append(txt).append('\n')
                for (i in 0 until node.childCount) node.getChild(i)?.let { walk(it, depth + 1) }
            }
            walk(root, 0)
            return sb.toString()
        }

        fun tapText(text: String): Boolean {
            val svc = instance ?: return false
            val root = svc.rootInActiveWindow ?: return false
            fun find(node: AccessibilityNodeInfo): AccessibilityNodeInfo? {
                val t = node.text?.toString() ?: node.contentDescription?.toString() ?: ""
                if (t.contains(text, ignoreCase = true) && node.isClickable) return node
                for (i in 0 until node.childCount) {
                    node.getChild(i)?.let { find(it)?.let { r -> return r } }
                }
                return null
            }
            val target = find(root) ?: return false
            return target.performAction(AccessibilityNodeInfo.ACTION_CLICK)
        }

        fun tap(x: Float, y: Float): Boolean {
            val svc = instance ?: return false
            val path = Path().apply { moveTo(x, y) }
            val gesture = GestureDescription.Builder()
                .addStroke(GestureDescription.StrokeDescription(path, 0, 80)).build()
            return svc.dispatchGesture(gesture, null, null)
        }

        fun swipe(x1: Float, y1: Float, x2: Float, y2: Float): Boolean {
            val svc = instance ?: return false
            val path = Path().apply { moveTo(x1, y1); lineTo(x2, y2) }
            val gesture = GestureDescription.Builder()
                .addStroke(GestureDescription.StrokeDescription(path, 0, 250)).build()
            return svc.dispatchGesture(gesture, null, null)
        }

        fun openApp(packageName: String): Boolean {
            val svc = instance ?: return false
            val intent = svc.packageManager.getLaunchIntentForPackage(packageName)
            return if (intent != null) {
                intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                svc.startActivity(intent)
                true
            } else false
        }
    }

    override fun onAccessibilityEvent(event: android.view.accessibility.AccessibilityEvent) {}

    override fun onInterrupt() {}

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
    }

    override fun onUnbind(intent: Intent?): Boolean {
        instance = null
        return super.onUnbind(intent)
    }
}
