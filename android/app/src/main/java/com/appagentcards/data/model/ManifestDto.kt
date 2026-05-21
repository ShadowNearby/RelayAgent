package com.appagentcards.data.model

import com.charleskorn.kaml.YamlMap
import kotlinx.serialization.Contextual
import kotlinx.serialization.Serializable

@Serializable
data class ManifestDto(
    val spec_version: String,
    val card_version: String,
    val app_id: String,
    val app_name: String,
    val platforms: List<String>,
    val locale: List<String>,
    val embedded_agent: EmbeddedAgentDto,
    val provenance: ProvenanceDto,
    val constraints: ConstraintsDto
)

@Serializable
data class EmbeddedAgentDto(
    val name: String,
    val type: String,
    val description: String,
    val entry: EntryDto,
    val invocation: InvocationDto,
    val output: OutputDto? = null,
    val capabilities: List<CapabilityDto>
)

@Serializable
data class EntryDto(
    @Contextual val primary: YamlMap,
    @Contextual val fallback: List<YamlMap> = emptyList(),
    val preconditions: List<PreconditionDto> = emptyList()
)

@Serializable
data class PreconditionDto(
    val type: String,
    val permission: String? = null,
    @Contextual val dismiss: YamlMap? = null
)

@Serializable
data class InvocationDto(
    val input: InputFieldDto,
    val submit: SubmitActionDto,
    val prompt_template: String? = null
)

@Serializable
data class InputFieldDto(
    @Contextual val field: YamlMap,
    val max_chars: Int? = null
)

@Serializable
data class SubmitActionDto(
    @Contextual val trigger: YamlMap
)

@Serializable
data class OutputDto(
    val method: String = "none",
    val completion_signal: CompletionSignalDto? = null
)

@Serializable
data class CompletionSignalDto(
    val type: String,
    val patterns: List<String>,
    val timeout_ms: Long = 30000
)

@Serializable
data class CapabilityDto(
    val id: String,
    val description: String,
    val example_prompts: List<String>,
    val executable: Boolean,
    val side_effects: List<String>,
    val requires_login: Boolean,
    val reversible: Boolean,
    val handoff_to_user_required: Boolean,
    val typical_latency_seconds: Double? = null,
    val failure_modes: List<String> = emptyList()
)

@Serializable
data class ProvenanceDto(
    val last_verified: String,
    val verified_app_version: String,
    val verified_os: String,
    val verified_device: String? = null,
    val verification_method: String,
    val evidence_url: String? = null,
    val x_device_metrics: DeviceMetricsDto? = null
)

@Serializable
data class DeviceMetricsDto(
    val resolution_px: List<Int>,
    val density_dpi: Int
)

@Serializable
data class ConstraintsDto(
    val app_version_min: String,
    val app_version_max: String? = null,
    val region: List<String>? = null,
    val network_required: Boolean = true,
    val known_issues: List<String> = emptyList()
)
