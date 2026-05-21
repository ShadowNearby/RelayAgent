package com.appagentcards.service.executor

import android.view.accessibility.AccessibilityNodeInfo
import com.appagentcards.domain.model.Selector
import com.appagentcards.domain.model.WaitDuration
import kotlinx.coroutines.delay
import javax.inject.Inject

class WaitController @Inject constructor(
    private val selectorResolver: SelectorResolver
) {
    suspend fun wait(duration: WaitDuration, root: AccessibilityNodeInfo): Boolean {
        return when (duration) {
            is WaitDuration.Milliseconds -> {
                delay(duration.ms.toLong())
                true
            }
            is WaitDuration.UntilSelector -> {
                val deadline = System.currentTimeMillis() + (duration.timeoutSeconds * 1000).toLong()
                while (System.currentTimeMillis() < deadline) {
                    val node = selectorResolver.resolve(duration.selector, root, 1f)
                    if (node != null) return true
                    delay(300)
                }
                false
            }
        }
    }
}
