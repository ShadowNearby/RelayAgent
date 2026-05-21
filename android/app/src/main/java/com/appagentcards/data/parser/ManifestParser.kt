package com.appagentcards.data.parser

import com.appagentcards.data.model.*
import com.appagentcards.domain.model.*
import com.charleskorn.kaml.Yaml
import com.charleskorn.kaml.YamlList
import com.charleskorn.kaml.YamlMap
import com.charleskorn.kaml.YamlNode
import com.charleskorn.kaml.YamlScalar

class ManifestParser {

    private val yaml = Yaml.default

    fun parse(yamlString: String): Card {
        val dto = yaml.decodeFromString(ManifestDto.serializer(), yamlString)
        return dto.toDomain()
    }

    fun parseAll(yamlStrings: List<String>): List<Card> = yamlStrings.map { parse(it) }

    // ---- DTO → Domain ----

    private fun ManifestDto.toDomain(): Card = Card(
        specVersion = spec_version, cardVersion = card_version,
        appId = app_id, appName = app_name,
        platforms = platforms, locale = locale,
        embeddedAgent = embedded_agent.toDomain(),
        provenance = provenance.toDomain(),
        constraints = constraints.toDomain()
    )

    private fun EmbeddedAgentDto.toDomain(): EmbeddedAgent = EmbeddedAgent(
        name = name, type = parseAgentType(type), description = description,
        entry = entry.toDomain(), invocation = invocation.toDomain(),
        output = output?.toDomain() ?: Output(),
        capabilities = capabilities.map { it.toDomain() }
    )

    private fun EntryDto.toDomain(): Entry = Entry(
        primary = parseEntryMethod(primary),
        fallback = fallback.map { parseEntryMethod(it) },
        preconditions = preconditions.map { it.toDomain() }
    )

    private fun PreconditionDto.toDomain(): Precondition = Precondition(
        type = type, permission = permission,
        dismiss = dismiss?.let { parseStep(it) }
    )

    private fun InvocationDto.toDomain(): Invocation = Invocation(
        input = InputField(field = parseSelector(input.field), maxChars = input.max_chars),
        submit = SubmitAction(trigger = parseSelector(submit.trigger)),
        promptTemplate = prompt_template ?: "{{user_prompt}}"
    )

    private fun OutputDto.toDomain(): Output = Output(
        method = parseOutputMethod(method),
        completionSignal = completion_signal?.let {
            CompletionSignal(type = it.type, patterns = it.patterns, timeoutMs = it.timeout_ms)
        }
    )

    private fun CapabilityDto.toDomain(): Capability = Capability(
        id = id, description = description, examplePrompts = example_prompts,
        executable = executable, sideEffects = side_effects.map { parseSideEffect(it) },
        requiresLogin = requires_login, reversible = reversible,
        handoffToUserRequired = handoff_to_user_required,
        typicalLatencySeconds = typical_latency_seconds, failureModes = failure_modes
    )

    private fun ProvenanceDto.toDomain(): Provenance = Provenance(
        lastVerified = last_verified, verifiedAppVersion = verified_app_version,
        verifiedOs = verified_os, verifiedDevice = verified_device,
        verificationMethod = verification_method, evidenceUrl = evidence_url,
        xDeviceMetrics = x_device_metrics?.let {
            DeviceMetrics(it.resolution_px[0] to it.resolution_px[1], it.density_dpi)
        }
    )

    private fun ConstraintsDto.toDomain(): Constraints = Constraints(
        appVersionMin = app_version_min, appVersionMax = app_version_max,
        region = region, networkRequired = network_required, knownIssues = known_issues
    )

    // ---- YamlMap helpers using kaml 0.57 API ----

    private fun YamlMap.has(key: String): Boolean = get<YamlNode>(key) != null

    private fun YamlMap.str(key: String): String? = get<YamlScalar>(key)?.content

    private fun YamlMap.reqStr(key: String): String =
        str(key) ?: error("Missing required key '$key'")

    private fun YamlMap.int(key: String): Int? = get<YamlScalar>(key)?.toInt()

    private fun YamlMap.float(key: String): Float? = get<YamlScalar>(key)?.toFloat()

    private fun YamlMap.bool(key: String): Boolean? = get<YamlScalar>(key)?.toBoolean()

    private fun YamlMap.yamlMap(key: String): YamlMap? = get<YamlMap>(key)

    private fun YamlMap.reqYamlMap(key: String): YamlMap =
        yamlMap(key) ?: error("Missing required map key '$key'")

    private fun YamlMap.yamlList(key: String): YamlList? = get<YamlList>(key)

    private fun YamlMap.reqYamlList(key: String): YamlList =
        yamlList(key) ?: error("Missing required list key '$key'")

    // ---- Selector parsing ----

    private fun parseSelector(m: YamlMap): Selector {
        return when {
            m.has("accessibility_id") -> Selector.ByAccessibilityId(m.reqStr("accessibility_id"))
            m.has("resource_id") -> Selector.ByResourceId(m.reqStr("resource_id"))
            m.has("text") -> Selector.ByText(m.reqStr("text"))
            m.has("text_contains") -> Selector.ByTextContains(m.reqStr("text_contains"))
            m.has("xpath") -> Selector.ByXPath(m.reqStr("xpath"))
            m.has("x_bounds") -> {
                val bounds = m.reqYamlMap("x_bounds")
                val box = bounds.reqYamlList("box")
                Selector.ByBounds(
                    box = Rect(
                        boxItemInt(box.items[0]), boxItemInt(box.items[1]),
                        boxItemInt(box.items[2]), boxItemInt(box.items[3])
                    ),
                    anchor = bounds.str("anchor")?.let { parseAnchor(it) } ?: Anchor.NONE
                )
            }
            else -> error("Unknown selector: keys=${m.entries.keys.map { it.content }}")
        }
    }

    private fun boxItemInt(node: YamlNode): Int =
        (node as? YamlScalar)?.toInt() ?: 0

    // ---- Entry method parsing ----

    private fun parseEntryMethod(m: YamlMap): EntryMethod {
        return when (val method = m.reqStr("method")) {
            "deep_link" -> EntryMethod.DeepLink(uri = m.reqStr("uri"))
            "intent" -> EntryMethod.Intent(
                action = m.reqStr("action"),
                component = m.str("component"),
                extras = m.yamlMap("extras")?.entries?.mapKeys {
                    it.key.content
                }?.mapValues { (_, v) ->
                    (v as? YamlScalar)?.content ?: ""
                }
            )
            "tap_sequence" -> EntryMethod.TapSequence(
                steps = m.yamlList("steps")?.items?.map { stepNode ->
                    parseStep(stepNode as? YamlMap ?: error("step is not a map"))
                } ?: emptyList()
            )
            else -> error("Unknown entry method: $method")
        }
    }

    // ---- Step parsing ----

    private fun parseStep(m: YamlMap): Step {
        return when {
            m.has("tap") -> Step.Tap(parseSelector(m.reqYamlMap("tap")))
            m.has("tap_label") -> {
                val labelMap = m.reqYamlMap("tap_label")
                val (label, matchMode) = parseTapLabel(labelMap)
                Step.TapLabel(
                    label = label, matchMode = matchMode,
                    timeoutSeconds = labelMap.float("timeout_seconds") ?: 6f,
                    scrollAttempts = labelMap.int("scroll_attempts") ?: 3,
                    required = labelMap.bool("required") ?: true
                )
            }
            m.has("tap_screen_fraction") -> {
                val f = m.reqYamlMap("tap_screen_fraction")
                Step.TapScreenFraction(
                    xRatio = f.float("x_ratio") ?: error("missing x_ratio"),
                    yRatio = f.float("y_ratio") ?: error("missing y_ratio"),
                    label = f.str("label")
                )
            }
            m.has("swipe") -> {
                val s = m.reqYamlMap("swipe")
                val from = parseCoord(s.reqStr("from"))
                val to = parseCoord(s.reqStr("to"))
                Step.Swipe(
                    fromX = from.first, fromY = from.second,
                    toX = to.first, toY = to.second,
                    durationMs = s.int("duration_ms") ?: 300
                )
            }
            m.has("wait") -> {
                val w = m.reqYamlMap("wait")
                when {
                    w.has("ms") -> Step.Wait(WaitDuration.Milliseconds(w.int("ms") ?: 0))
                    w.has("until") -> Step.Wait(
                        WaitDuration.UntilSelector(
                            selector = parseSelector(w.reqYamlMap("until")),
                            timeoutSeconds = w.float("timeout_seconds") ?: 10f
                        )
                    )
                    else -> error("Unknown wait: keys=${w.entries.keys.map { it.content }}")
                }
            }
            else -> error("Unknown step: keys=${m.entries.keys.map { it.content }}")
        }
    }

    private fun parseTapLabel(m: YamlMap): Pair<String, LabelMatchMode> {
        for (key in LABEL_KEYS) {
            if (m.has(key)) {
                return m.reqStr(key) to LABEL_KEY_TO_MODE[key]!!
            }
        }
        error("tap_label missing label key; one of: ${LABEL_KEYS.joinToString()}")
    }

    private fun parseCoord(s: String): Pair<Int, Int> {
        val parts = s.trim('[', ']').split(',').map { it.trim().toInt() }
        return parts[0] to parts[1]
    }

    // ---- Enums ----

    private fun parseAgentType(s: String): AgentType = when (s.lowercase()) {
        "native_in_app_agent" -> AgentType.NATIVE_IN_APP_AGENT
        "chat_widget" -> AgentType.CHAT_WIDGET
        "smart_search" -> AgentType.SMART_SEARCH
        "voice_assistant" -> AgentType.VOICE_ASSISTANT
        else -> AgentType.NATIVE_IN_APP_AGENT
    }

    private fun parseSideEffect(s: String): SideEffect = when (s.lowercase()) {
        "payment" -> SideEffect.PAYMENT
        "external_communication" -> SideEffect.EXTERNAL_COMMUNICATION
        "data_write" -> SideEffect.DATA_WRITE
        "data_delete" -> SideEffect.DATA_DELETE
        "physical_action" -> SideEffect.PHYSICAL_ACTION
        "none" -> SideEffect.NONE
        else -> SideEffect.NONE
    }

    private fun parseOutputMethod(s: String): OutputMethod = when (s.lowercase()) {
        "none" -> OutputMethod.NONE
        "screen_text_extract" -> OutputMethod.SCREEN_TEXT_EXTRACT
        "accessibility_tree" -> OutputMethod.ACCESSIBILITY_TREE
        else -> OutputMethod.NONE
    }

    private fun parseAnchor(s: String): Anchor = when (s.lowercase()) {
        "top_left" -> Anchor.TOP_LEFT
        "top_right" -> Anchor.TOP_RIGHT
        "bottom_left" -> Anchor.BOTTOM_LEFT
        "bottom_right" -> Anchor.BOTTOM_RIGHT
        "center" -> Anchor.CENTER
        "none" -> Anchor.NONE
        else -> Anchor.NONE
    }

    companion object {
        private val LABEL_KEYS = listOf(
            "text", "text_contains", "text_or_desc",
            "text_or_desc_contains", "accessibility_id"
        )
        private val LABEL_KEY_TO_MODE = mapOf(
            "text" to LabelMatchMode.TEXT,
            "text_contains" to LabelMatchMode.TEXT_CONTAINS,
            "text_or_desc" to LabelMatchMode.TEXT_OR_DESC,
            "text_or_desc_contains" to LabelMatchMode.TEXT_OR_DESC_CONTAINS,
            "accessibility_id" to LabelMatchMode.ACCESSIBILITY_ID
        )
    }
}
