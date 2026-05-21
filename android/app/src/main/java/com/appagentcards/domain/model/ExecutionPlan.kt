package com.appagentcards.domain.model

data class ExecutionPlan(
    val card: Card,
    val capability: Capability,
    val renderedPrompt: String,
    val targetDeviceMetrics: DeviceMetrics,
    val entrySteps: List<Step>,
    val invocationField: Selector,
    val invocationSubmit: Selector,
    val preconditions: List<Precondition>,
    val safetyAssessment: SafetyAssessment,
    val outputMethod: OutputMethod
)
