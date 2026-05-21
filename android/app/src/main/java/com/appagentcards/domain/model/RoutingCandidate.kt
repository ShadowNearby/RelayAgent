package com.appagentcards.domain.model

data class CardSummary(
    val appId: String,
    val appName: String,
    val agentName: String,
    val agentType: AgentType
)

data class RoutingCandidate(
    val cardSummary: CardSummary,
    val capability: Capability,
    val score: Int
)
