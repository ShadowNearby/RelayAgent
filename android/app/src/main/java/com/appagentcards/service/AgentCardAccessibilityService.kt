package com.appagentcards.service

import android.accessibilityservice.AccessibilityService
import android.view.accessibility.AccessibilityEvent
import com.appagentcards.domain.model.ExecutionPlan
import com.appagentcards.service.executor.AccessibilityActionExecutor
import dagger.hilt.android.AndroidEntryPoint
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import javax.inject.Inject

@AndroidEntryPoint
class AgentCardAccessibilityService : AccessibilityService() {

    @Inject lateinit var executor: AccessibilityActionExecutor

    private val serviceScope = CoroutineScope(SupervisorJob() + Dispatchers.Main)

    override fun onServiceConnected() {
        super.onServiceConnected()
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        // React to window state changes during execution if needed
    }

    override fun onInterrupt() {
        executor.cancel()
    }

    override fun onDestroy() {
        executor.cancel()
        super.onDestroy()
    }

    fun executePlan(plan: ExecutionPlan) {
        executor.execute(plan, this, serviceScope)
    }
}
