package com.appagentcards.presentation.ui.home

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.appagentcards.domain.model.DeviceMetrics
import com.appagentcards.domain.model.ExecutionPlan
import com.appagentcards.domain.model.RoutingCandidate
import com.appagentcards.domain.usecase.BuildExecutionPlanUseCase
import com.appagentcards.domain.usecase.MatchIntentUseCase
import com.appagentcards.service.model.ExecutionStateHolder
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import javax.inject.Inject

@HiltViewModel
class HomeViewModel @Inject constructor(
    private val matchIntentUseCase: MatchIntentUseCase,
    private val buildExecutionPlanUseCase: BuildExecutionPlanUseCase,
    private val executionStateHolder: ExecutionStateHolder
) : ViewModel() {

    private val _uiState = MutableStateFlow<HomeUiState>(HomeUiState.Idle)
    val uiState: StateFlow<HomeUiState> = _uiState.asStateFlow()

    private val _prompt = MutableStateFlow("")
    val prompt: StateFlow<String> = _prompt.asStateFlow()

    private var currentCandidates: List<RoutingCandidate> = emptyList()
    private var currentPlan: ExecutionPlan? = null

    val executionState: StateFlow<com.appagentcards.service.model.ExecutionState>
        get() = executionStateHolder.state

    fun onPromptChanged(newPrompt: String) {
        _prompt.value = newPrompt
    }

    fun onSearch() {
        val promptText = _prompt.value.trim()
        if (promptText.isEmpty()) return

        _uiState.value = HomeUiState.Loading
        viewModelScope.launch {
            matchIntentUseCase.invoke(promptText)
                .onSuccess { candidates ->
                    currentCandidates = candidates
                    _uiState.value = if (candidates.isEmpty()) {
                        HomeUiState.Error("No matching capabilities found. Try a different prompt.")
                    } else {
                        HomeUiState.Candidates(promptText, candidates)
                    }
                }
                .onFailure { e ->
                    _uiState.value = HomeUiState.Error(
                        e.message ?: "Failed to match intent"
                    )
                }
        }
    }

    fun onSelectCandidate(candidate: RoutingCandidate) {
        _uiState.value = HomeUiState.Loading
        viewModelScope.launch {
            val targetMetrics = DeviceMetrics(1080 to 2424, 420) // will be replaced with actual device metrics
            buildExecutionPlanUseCase.invoke(candidate, targetMetrics)
                .onSuccess { result ->
                    currentPlan = result.plan
                    _uiState.value = HomeUiState.PlanReady(
                        result.plan,
                        result.staleness is com.appagentcards.domain.engine.StalenessResult.Stale
                    )
                }
                .onFailure { e ->
                    _uiState.value = HomeUiState.Error(
                        e.message ?: "Failed to build execution plan"
                    )
                }
        }
    }

    fun onConfirmExecution() {
        currentPlan?.let { plan ->
            executionStateHolder.reset()
            _uiState.value = HomeUiState.Executing(plan)
        }
    }

    fun onRefreshManifests() {
        // will trigger remote refresh in a future phase
    }

    fun reset() {
        _uiState.value = HomeUiState.Idle
        currentPlan = null
        executionStateHolder.reset()
    }
}

sealed class HomeUiState {
    object Idle : HomeUiState()
    object Loading : HomeUiState()
    data class Candidates(
        val prompt: String,
        val candidates: List<RoutingCandidate>
    ) : HomeUiState()
    data class PlanReady(
        val plan: ExecutionPlan,
        val isStale: Boolean
    ) : HomeUiState()
    data class Executing(val plan: ExecutionPlan) : HomeUiState()
    data class Error(val message: String) : HomeUiState()
}
