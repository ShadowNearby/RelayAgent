package com.appagentcards.data.parser

import com.appagentcards.domain.model.*
import com.charleskorn.kaml.Yaml
import com.charleskorn.kaml.YamlList
import com.charleskorn.kaml.YamlMap
import com.charleskorn.kaml.YamlNode
import com.charleskorn.kaml.YamlScalar

class ManifestParser {

    private val yaml = Yaml.default

    fun parse(yamlString: String): Card {
        val root = yaml.parseToYamlNode(yamlString) as? YamlMap
            ?: error("Manifest root is not a YAML map")
        return parseCard(root)
    }

    fun parseAll(yamlStrings: List<String>): List<Card> = yamlStrings.map { parse(it) }

    // ---- Root ----

    private fun parseCard(m: YamlMap): Card = Card(
        specVersion = m.reqStr("spec_version"),
        cardVersion = m.reqStr("card_version"),
        appId = m.reqStr("app_id"),
        appName = m.reqStr("app_name"),
        platforms = m.reqStrList("platforms"),
        locale = m.reqStrList("locale"),
        embeddedAgent = parseEmbeddedAgent(m.reqYamlMap("embedded_agent")),
        provenance = parseProvenance(m.reqYamlMap("provenance")),
        constraints = parseConstraints(m.reqYamlMap("constraints"))
    )

    private fun parseEmbeddedAgent(m: YamlMap): EmbeddedAgent = EmbeddedAgent(
        name = m.reqStr("name"),
        type = parseAgentType(m.reqStr("type")),
        description = m.reqStr("description"),
        entry = parseEntry(m.reqYamlMap("entry")),
        invocation = parseInvocation(m.reqYamlMap("invocation")),
        output = m.yamlMap("output")?.let { parseOutput(it) } ?: Output(),
        capabilities = m.reqYamlList("capabilities").items.map {
            parseCapability(it.asMap("capability"))
        }
    )

    private fun parseEntry(m: YamlMap): Entry = Entry(
        primary = parseEntryMethod(m.reqYamlMap("primary")),
        fallback = m.yamlList("fallback")?.items?.map {
            parseEntryMethod(it.asMap("fallback entry"))
        } ?: emptyList(),
        preconditions = m.yamlList("preconditions")?.items?.map {
            parsePrecondition(it.asMap("precondition"))
        } ?: emptyList()
    )

    private fun parsePrecondition(m: YamlMap): Precondition = Precondition(
        type = m.reqStr("type"),
        permission = m.str("permission"),
        dismiss = m.yamlMap("dismiss")?.let { parseStep(it) }
    )

    private fun parseInvocation(m: YamlMap): Invocation = Invocation(
        input = InputField(
            field = parseSelector(m.reqYamlMap("input").reqYamlMap("field")),
            maxChars = m.reqYamlMap("input").int("max_chars")
        ),
        submit = SubmitAction(
            trigger = parseSelector(m.reqYamlMap("submit").reqYamlMap("trigger"))
        ),
        promptTemplate = m.str("prompt_template") ?: "{{user_prompt}}"
    )

    private fun parseOutput(m: YamlMap): Output = Output(
        method = parseOutputMethod(m.str("method") ?: "none"),
        completionSignal = m.yamlMap("completion_signal")?.let { c ->
            CompletionSignal(
                type = c.reqStr("type"),
                patterns = c.reqStrList("patterns"),
                timeoutMs = c.int("timeout_ms")?.toLong() ?: 30000L
            )
        }
    )

    private fun parseCapability(m: YamlMap): Capability = Capability(
        id = m.reqStr("id"),
        description = m.reqStr("description"),
        examplePrompts = m.reqStrList("example_prompts"),
        executable = m.bool("executable") ?: false,
        sideEffects = m.yamlList("side_effects")?.items?.map {
            parseSideEffect((it as? YamlScalar)?.content ?: "none")
        } ?: emptyList(),
        requiresLogin = m.bool("requires_login") ?: false,
        reversible = m.bool("reversible") ?: false,
        handoffToUserRequired = m.bool("handoff_to_user_required") ?: false,
        typicalLatencySeconds = m.float("typical_latency_seconds")?.toDouble(),
        failureModes = m.yamlList("failure_modes")?.items?.mapNotNull {
            (it as? YamlScalar)?.content
        } ?: emptyList()
    )

    private fun parseProvenance(m: YamlMap): Provenance = Provenance(
        lastVerified = m.reqStr("last_verified"),
        verifiedAppVersion = m.reqStr("verified_app_version"),
        verifiedOs = m.reqStr("verified_os"),
        verifiedDevice = m.str("verified_device"),
        verificationMethod = m.reqStr("verification_method"),
        evidenceUrl = m.str("evidence_url"),
        xDeviceMetrics = m.yamlMap("x_device_metrics")?.let { dm ->
            val res = dm.reqYamlList("resolution_px").items
            DeviceMetrics(
                resolutionPx = (res[0] as YamlScalar).toInt() to (res[1] as YamlScalar).toInt(),
                densityDpi = dm.reqInt("density_dpi")
            )
        }
    )

    private fun parseConstraints(m: YamlMap): Constraints = Constraints(
        appVersionMin = m.reqStr("app_version_min"),
        appVersionMax = m.str("app_version_max"),
        region = m.yamlList("region")?.items?.mapNotNull { (it as? YamlScalar)?.content },
        networkRequired = m.bool("network_required") ?: true,
        knownIssues = m.yamlList("known_issues")?.items?.mapNotNull {
            (it as? YamlScalar)?.content
        } ?: emptyList()
    )

    // ---- YamlMap / YamlNode helpers (kaml 0.57 API) ----

    private fun YamlMap.has(key: String): Boolean = get<YamlNode>(key) != null

    private fun YamlMap.str(key: String): String? = get<YamlScalar>(key)?.content

    private fun YamlMap.reqStr(key: String): String =
        str(key) ?: error("Missing required key '$key'")

    private fun YamlMap.int(key: String): Int? = get<YamlScalar>(key)?.toInt()

    private fun YamlMap.reqInt(key: String): Int =
        int(key) ?: error("Missing required int key '$key'")

    private fun YamlMap.float(key: String): Float? = get<YamlScalar>(key)?.toFloat()

    private fun YamlMap.bool(key: String): Boolean? = get<YamlScalar>(key)?.toBoolean()

    private fun YamlMap.yamlMap(key: String): YamlMap? = get<YamlMap>(key)

    private fun YamlMap.reqYamlMap(key: String): YamlMap =
        yamlMap(key) ?: error("Missing required map key '$key'")

    private fun YamlMap.yamlList(key: String): YamlList? = get<YamlList>(key)

    private fun YamlMap.reqYamlList(key: String): YamlList =
        yamlList(key) ?: error("Missing required list key '$key'")

    private fun YamlMap.reqStrList(key: String): List<String> =
        reqYamlList(key).items.mapNotNull { (it as? YamlScalar)?.content }

    private fun YamlNode.asMap(label: String): YamlMap =
        this as? YamlMap ?: error("Expected $label to be a YAML map")

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
