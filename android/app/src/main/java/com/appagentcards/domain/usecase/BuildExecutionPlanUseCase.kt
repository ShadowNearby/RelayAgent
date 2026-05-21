package com.appagentcards.domain.usecase

import com.appagentcards.data.repository.ManifestRepository
import com.appagentcards.domain.engine.*
import com.appagentcards.domain.model.*
import javax.inject.Inject

class BuildExecutionPlanUseCase @Inject constructor(
    private val manifestRepository: ManifestRepository,
    private val coordinateRemapper: CoordinateRemapper,
    private val safetyEnforcer: SafetyEnforcer,
    private val stalenessChecker: StalenessChecker,
    private val promptTemplateEngine: PromptTemplateEngine
) {
    suspend fun invoke(
        candidate: RoutingCandidate,
        targetMetrics: DeviceMetrics
    ): Result<BuildResult> {
        return manifestRepository.loadCard(candidate.cardSummary.appId).map { card ->
            val capability = card.embeddedAgent.capabilities.first { it.id == candidate.capability.id }
            val sourceMetrics = card.provenance.xDeviceMetrics
            val staleness = stalenessChecker.check(card)
            val safety = safetyEnforcer.assess(capability)

            val entrySteps = when (val primary = card.embeddedAgent.entry.primary) {
                is EntryMethod.TapSequence -> {
                    if (sourceMetrics != null && sourceMetrics != targetMetrics) {
                        coordinateRemapper.remapSteps(primary.steps, sourceMetrics, targetMetrics)
                    } else {
                        primary.steps
                    }
                }
                else -> emptyList()
            }

            val invocationField = if (sourceMetrics != null && sourceMetrics != targetMetrics) {
                coordinateRemapper.remapSelector(
                    card.embeddedAgent.invocation.input.field,
                    sourceMetrics, targetMetrics
                )
            } else {
                card.embeddedAgent.invocation.input.field
            }

            val invocationSubmit = if (sourceMetrics != null && sourceMetrics != targetMetrics) {
                coordinateRemapper.remapSelector(
                    card.embeddedAgent.invocation.submit.trigger,
                    sourceMetrics, targetMetrics
                )
            } else {
                card.embeddedAgent.invocation.submit.trigger
            }

            val renderedPrompt = promptTemplateEngine.render(
                template = card.embeddedAgent.invocation.promptTemplate,
                userPrompt = "",  // filled at execution time
                capabilityId = capability.id
            )

            val plan = ExecutionPlan(
                card = card,
                capability = capability,
                renderedPrompt = renderedPrompt,
                targetDeviceMetrics = targetMetrics,
                entrySteps = entrySteps,
                invocationField = invocationField,
                invocationSubmit = invocationSubmit,
                preconditions = card.embeddedAgent.entry.preconditions,
                safetyAssessment = safety,
                outputMethod = card.embeddedAgent.output.method
            )

            BuildResult(plan, staleness)
        }
    }
}

data class BuildResult(
    val plan: ExecutionPlan,
    val staleness: StalenessResult
)
