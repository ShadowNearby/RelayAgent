package com.appagentcards.domain.model

data class Card(
    val specVersion: String,
    val cardVersion: String,
    val appId: String,
    val appName: String,
    val platforms: List<String>,
    val locale: List<String>,
    val embeddedAgent: EmbeddedAgent,
    val provenance: Provenance,
    val constraints: Constraints
)

data class EmbeddedAgent(
    val name: String,
    val type: AgentType,
    val description: String,
    val entry: Entry,
    val invocation: Invocation,
    val output: Output = Output(),
    val capabilities: List<Capability>
)

enum class AgentType {
    NATIVE_IN_APP_AGENT,
    CHAT_WIDGET,
    SMART_SEARCH,
    VOICE_ASSISTANT
}

data class Entry(
    val primary: EntryMethod,
    val fallback: List<EntryMethod> = emptyList(),
    val preconditions: List<Precondition> = emptyList()
)

sealed class EntryMethod {
    data class DeepLink(val uri: String) : EntryMethod()
    data class Intent(
        val action: String,
        val component: String? = null,
        val extras: Map<String, String>? = null
    ) : EntryMethod()
    data class TapSequence(val steps: List<Step>) : EntryMethod()
}

data class Invocation(
    val input: InputField,
    val submit: SubmitAction,
    val promptTemplate: String = "{{user_prompt}}"
)

data class InputField(
    val field: Selector,
    val maxChars: Int? = null
)

data class SubmitAction(
    val trigger: Selector
)

data class Output(
    val method: OutputMethod = OutputMethod.NONE,
    val completionSignal: CompletionSignal? = null
)

enum class OutputMethod {
    NONE,
    SCREEN_TEXT_EXTRACT,
    ACCESSIBILITY_TREE
}

data class CompletionSignal(
    val type: String,
    val patterns: List<String>,
    val timeoutMs: Long = 30000
)

data class Precondition(
    val type: String,
    val permission: String? = null,
    val dismiss: Step? = null
)

data class Provenance(
    val lastVerified: String,
    val verifiedAppVersion: String,
    val verifiedOs: String,
    val verifiedDevice: String? = null,
    val verificationMethod: String,
    val evidenceUrl: String? = null,
    val xDeviceMetrics: DeviceMetrics? = null
)

data class Constraints(
    val appVersionMin: String,
    val appVersionMax: String? = null,
    val region: List<String>? = null,
    val networkRequired: Boolean = true,
    val knownIssues: List<String> = emptyList()
)
