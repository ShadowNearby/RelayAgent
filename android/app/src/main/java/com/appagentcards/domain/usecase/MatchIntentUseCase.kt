package com.appagentcards.domain.usecase

import com.appagentcards.data.repository.ManifestRepository
import com.appagentcards.domain.engine.IntentMatcher
import com.appagentcards.domain.model.Card
import com.appagentcards.domain.model.CardSummary
import com.appagentcards.domain.model.RoutingCandidate
import javax.inject.Inject

class MatchIntentUseCase @Inject constructor(
    private val manifestRepository: ManifestRepository,
    private val intentMatcher: IntentMatcher
) {
    suspend fun invoke(prompt: String): Result<List<RoutingCandidate>> {
        return manifestRepository.loadAllCards().map { cards ->
            val tokens = intentMatcher.tokenize(prompt)
            val candidates = mutableListOf<RoutingCandidate>()

            for (card in cards) {
                for (capability in card.embeddedAgent.capabilities) {
                    val score = intentMatcher.scoreCapability(tokens, capability)
                    if (score > 0) {
                        candidates.add(
                            RoutingCandidate(
                                cardSummary = toSummary(card),
                                capability = capability,
                                score = score
                            )
                        )
                    }
                }
            }

            candidates.sortedByDescending { it.score }
        }
    }

    private fun toSummary(card: Card): CardSummary = CardSummary(
        appId = card.appId,
        appName = card.appName,
        agentName = card.embeddedAgent.name,
        agentType = card.embeddedAgent.type
    )
}
