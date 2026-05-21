package com.appagentcards.domain.model

data class SafetyAssessment(
    val executable: Boolean,
    val hasPayment: Boolean,
    val hasDataDeletion: Boolean,
    val hasExternalCommunication: Boolean,
    val handoffToUserRequired: Boolean,
    val requiresUserConfirmation: Boolean,
    val warnings: List<String> = emptyList()
)
