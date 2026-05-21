package com.appagentcards.service.model

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import javax.inject.Inject
import javax.inject.Singleton

sealed class ExecutionState {
    object Idle : ExecutionState()
    data class InProgress(
        val stepIndex: Int,
        val totalSteps: Int,
        val stepDescription: String,
        val appName: String
    ) : ExecutionState()
    data class HandedOff(val message: String) : ExecutionState()
    data class Completed(val message: String) : ExecutionState()
    data class Failed(val error: String, val stepIndex: Int = -1) : ExecutionState()
}

@Singleton
class ExecutionStateHolder @Inject constructor() {
    private val _state = MutableStateFlow<ExecutionState>(ExecutionState.Idle)
    val state: StateFlow<ExecutionState> = _state.asStateFlow()

    fun update(newState: ExecutionState) {
        _state.value = newState
    }

    fun reset() {
        _state.value = ExecutionState.Idle
    }
}
