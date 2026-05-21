package com.appagentcards.domain.model

data class Capability(
    val id: String,
    val description: String,
    val examplePrompts: List<String>,
    val executable: Boolean,
    val sideEffects: List<SideEffect>,
    val requiresLogin: Boolean,
    val reversible: Boolean,
    val handoffToUserRequired: Boolean,
    val typicalLatencySeconds: Double? = null,
    val failureModes: List<String> = emptyList()
)

enum class SideEffect {
    PAYMENT,
    EXTERNAL_COMMUNICATION,
    DATA_WRITE,
    DATA_DELETE,
    PHYSICAL_ACTION,
    NONE
}
