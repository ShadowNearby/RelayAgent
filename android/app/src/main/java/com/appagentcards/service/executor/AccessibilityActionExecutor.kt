package com.appagentcards.service.executor

import android.accessibilityservice.AccessibilityService
import android.content.Intent
import android.view.accessibility.AccessibilityNodeInfo
import com.appagentcards.domain.engine.CoordinateRemapper
import com.appagentcards.domain.engine.PromptTemplateEngine
import com.appagentcards.domain.engine.SafetyEnforcer
import com.appagentcards.domain.engine.SafetyViolationException
import com.appagentcards.domain.model.*
import com.appagentcards.service.model.ExecutionState
import com.appagentcards.service.model.ExecutionStateHolder
import kotlinx.coroutines.*
import javax.inject.Inject
import javax.inject.Singleton

@Singleton
class AccessibilityActionExecutor @Inject constructor(
    private val selectorResolver: SelectorResolver,
    private val gesturePerformer: GesturePerformer,
    private val waitController: WaitController,
    private val blockerDismisser: BlockerDismisser,
    private val safetyEnforcer: SafetyEnforcer,
    private val coordinateRemapper: CoordinateRemapper,
    private val promptTemplateEngine: PromptTemplateEngine,
    private val executionStateHolder: ExecutionStateHolder
) {
    private var job: Job? = null

    fun execute(plan: ExecutionPlan, service: AccessibilityService, scope: CoroutineScope) {
        job?.cancel()
        job = scope.launch {
            try {
                runExecution(plan, service)
            } catch (e: CancellationException) {
                executionStateHolder.update(ExecutionState.Idle)
            } catch (e: SafetyViolationException) {
                executionStateHolder.update(
                    ExecutionState.Failed("Safety violation: ${e.message}")
                )
            } catch (e: Exception) {
                executionStateHolder.update(
                    ExecutionState.Failed(e.message ?: "Unknown error")
                )
            }
        }
    }

    fun cancel() {
        job?.cancel()
        executionStateHolder.reset()
    }

    private suspend fun runExecution(plan: ExecutionPlan, service: AccessibilityService) {
        val totalSteps = plan.entrySteps.size + 2
        val appName = plan.card.appName

        // 1. Launch target app
        emitStep(0, totalSteps, "Launching ${plan.card.appName}", appName)
        launchApp(plan, service)
        delay(2000) // wait for app to start

        val root = waitForRoot(service) ?: run {
            executionStateHolder.update(ExecutionState.Failed("Could not get accessibility root"))
            return
        }

        // 2. Dismiss any blockers (ads, dialogs)
        blockerDismisser.dismissCommonBlockers(root)
        root.recycle()

        // 3. Execute entry steps
        for ((index, step) in plan.entrySteps.withIndex()) {
            safetyEnforcer.enforceHandoff(plan, index)

            val currentRoot = waitForRoot(service) ?: continue
            val stepDesc = describeStep(step)
            emitStep(index + 1, totalSteps, stepDesc, appName)

            executeStep(step, currentRoot, service)
            delay(500)
            currentRoot.recycle()
        }

        // 4. Focus input field and type text
        val entryLastIndex = plan.entrySteps.size
        safetyEnforcer.enforceHandoff(plan, entryLastIndex)

        val invokeRoot = waitForRoot(service) ?: run {
            executionStateHolder.update(ExecutionState.Failed("Lost window after entry steps"))
            return
        }

        emitStep(entryLastIndex + 1, totalSteps, "Focusing input field", appName)
        val inputField = selectorResolver.resolve(plan.invocationField, invokeRoot)
        if (inputField != null) {
            gesturePerformer.tap(inputField.centerX, inputField.centerY, service)
            delay(300)

            val focusedRoot = waitForRoot(service)
            if (focusedRoot != null) {
                val focused = focusedRoot.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)
                if (focused != null) {
                    val renderedPrompt = promptTemplateEngine.render(
                        template = plan.card.embeddedAgent.invocation.promptTemplate,
                        userPrompt = plan.renderedPrompt,
                        capabilityId = plan.capability.id
                    )
                    gesturePerformer.typeText(renderedPrompt, focused)
                    focused.recycle()
                }
                focusedRoot.recycle()
            }
        }
        invokeRoot.recycle()

        // 5. Submit
        emitStep(entryLastIndex + 2, totalSteps, "Submitting prompt", appName)
        delay(500)
        val submitRoot = waitForRoot(service) ?: return
        val submitTrigger = selectorResolver.resolve(plan.invocationSubmit, submitRoot)
        if (submitTrigger != null) {
            gesturePerformer.tap(submitTrigger.centerX, submitTrigger.centerY, service)
        }
        submitRoot.recycle()

        // 6. Safety enforcement - handoff
        val handoffIndex = safetyEnforcer.handoffStopIndex(plan)
        if (handoffIndex != null) {
            executionStateHolder.update(
                ExecutionState.HandedOff(
                    "Control returned to user. Complete the action in ${plan.card.appName} manually."
                )
            )
        } else {
            executionStateHolder.update(
                ExecutionState.Completed("Task submitted to ${plan.card.appName}.")
            )
        }
    }

    private suspend fun launchApp(plan: ExecutionPlan, service: AccessibilityService) {
        val entry = plan.card.embeddedAgent.entry.primary
        when (entry) {
            is EntryMethod.DeepLink -> {
                val intent = Intent(Intent.ACTION_VIEW, android.net.Uri.parse(entry.uri))
                intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                service.startActivity(intent)
            }
            is EntryMethod.Intent -> {
                val intent = Intent(entry.action)
                intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                entry.component?.let { intent.setClassName(plan.card.appId, it) }
                service.startActivity(intent)
            }
            is EntryMethod.TapSequence -> {
                val launchIntent = service.packageManager.getLaunchIntentForPackage(plan.card.appId)
                    ?: run {
                        executionStateHolder.update(
                            ExecutionState.Failed("Cannot launch ${plan.card.appId}: app not found")
                        )
                        return
                    }
                launchIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                service.startActivity(launchIntent)
            }
        }
    }

    private suspend fun executeStep(
        step: Step,
        root: AccessibilityNodeInfo,
        service: AccessibilityService
    ) {
        when (step) {
            is Step.Tap -> {
                val resolved = selectorResolver.resolve(step.selector, root)
                if (resolved != null) {
                    gesturePerformer.tap(resolved.centerX, resolved.centerY, service)
                }
            }
            is Step.TapLabel -> {
                val resolved = selectorResolver.resolveWithScroll(step, root)
                if (resolved != null) {
                    gesturePerformer.tap(resolved.centerX, resolved.centerY, service)
                } else if (step.required) {
                    throw IllegalStateException("Required tap_label not found: ${step.label}")
                }
            }
            is Step.TapScreenFraction -> {
                val displayMetrics = service.resources.displayMetrics
                val x = (displayMetrics.widthPixels * step.xRatio).toInt()
                val y = (displayMetrics.heightPixels * step.yRatio).toInt()
                gesturePerformer.tap(x, y, service)
            }
            is Step.Swipe -> {
                gesturePerformer.swipe(
                    step.fromX, step.fromY,
                    step.toX, step.toY,
                    step.durationMs,
                    service
                )
            }
            is Step.Wait -> {
                waitController.wait(step.duration, root)
            }
        }
        delay(300)
        blockerDismisser.dismissCommonBlockers(root)
    }

    private fun waitForRoot(service: AccessibilityService, timeoutMs: Long = 5000): AccessibilityNodeInfo? {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            val root = service.rootInActiveWindow
            if (root != null) return root
            Thread.sleep(200)
        }
        return null
    }

    private suspend fun emitStep(index: Int, total: Int, description: String, appName: String) {
        executionStateHolder.update(
            ExecutionState.InProgress(
                stepIndex = index,
                totalSteps = total,
                stepDescription = description,
                appName = appName
            )
        )
        delay(200)
    }

    private fun describeStep(step: Step): String = when (step) {
        is Step.Tap -> "Tapping: ${step.selector}"
        is Step.TapLabel -> "Finding and tapping: ${step.label}"
        is Step.TapScreenFraction -> "Tapping screen at (${step.xRatio}, ${step.yRatio})"
        is Step.Swipe -> "Swiping"
        is Step.Wait -> "Waiting"
    }
}
