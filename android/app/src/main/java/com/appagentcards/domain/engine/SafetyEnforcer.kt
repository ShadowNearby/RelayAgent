package com.appagentcards.domain.engine

import com.appagentcards.domain.model.Capability
import com.appagentcards.domain.model.ExecutionPlan
import com.appagentcards.domain.model.SafetyAssessment
import com.appagentcards.domain.model.SideEffect
import javax.inject.Inject

class SafetyEnforcer @Inject constructor() {

    fun assess(capability: Capability): SafetyAssessment {
        val warnings = mutableListOf<String>()

        if (capability.handoffToUserRequired) {
            warnings.add("Control will be returned to you before the final action. You must manually confirm.")
        }
        if (!capability.executable) {
            warnings.add("This agent provides information only. It does not complete the action.")
        }
        if (capability.requiresLogin) {
            warnings.add("Requires login to the target app.")
        }

        val hasPayment = SideEffect.PAYMENT in capability.sideEffects
        val hasDataDeletion = SideEffect.DATA_DELETE in capability.sideEffects
        val hasExternal = SideEffect.EXTERNAL_COMMUNICATION in capability.sideEffects

        if ((hasPayment || hasDataDeletion || hasExternal) && !capability.handoffToUserRequired) {
            warnings.add("WARNING: capability has irreversible side effects but handoff_to_user_required is false.")
        }

        val requiresUserConfirmation = hasPayment || hasDataDeletion || !capability.reversible

        return SafetyAssessment(
            executable = capability.executable,
            hasPayment = hasPayment,
            hasDataDeletion = hasDataDeletion,
            hasExternalCommunication = hasExternal,
            handoffToUserRequired = capability.handoffToUserRequired,
            requiresUserConfirmation = requiresUserConfirmation,
            warnings = warnings
        )
    }

    fun handoffStopIndex(plan: ExecutionPlan): Int? {
        return if (plan.safetyAssessment.handoffToUserRequired) {
            plan.entrySteps.size + 1
        } else {
            null
        }
    }

    fun enforceHandoff(plan: ExecutionPlan, currentStepIndex: Int) {
        val stopIndex = handoffStopIndex(plan)
        if (stopIndex != null && currentStepIndex >= stopIndex) {
            throw SafetyViolationException(
                "Cannot auto-execute step $currentStepIndex: handoff required after step ${stopIndex - 1}"
            )
        }
    }
}

class SafetyViolationException(message: String) : IllegalStateException(message)
