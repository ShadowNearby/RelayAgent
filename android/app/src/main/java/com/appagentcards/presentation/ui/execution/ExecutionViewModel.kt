package com.appagentcards.presentation.ui.execution

import androidx.lifecycle.ViewModel
import com.appagentcards.service.model.ExecutionState
import com.appagentcards.service.model.ExecutionStateHolder
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.flow.StateFlow
import javax.inject.Inject

@HiltViewModel
class ExecutionViewModel @Inject constructor(
    executionStateHolder: ExecutionStateHolder
) : ViewModel() {

    val executionState: StateFlow<ExecutionState> = executionStateHolder.state
}
