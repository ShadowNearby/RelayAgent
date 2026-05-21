package com.appagentcards.presentation.ui.execution

import androidx.compose.foundation.layout.*
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import com.appagentcards.service.model.ExecutionState

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ExecutionScreen(
    onBack: () -> Unit,
    viewModel: ExecutionViewModel = hiltViewModel()
) {
    val state by viewModel.executionState.collectAsState()

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Execution") },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back")
                    }
                }
            )
        }
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(16.dp),
            horizontalAlignment = Alignment.CenterHorizontally
        ) {
            when (val s = state) {
                is ExecutionState.Idle -> {
                    Text("Waiting for execution to start...")
                }
                is ExecutionState.InProgress -> {
                    LinearProgressIndicator(
                        progress = { s.stepIndex.toFloat() / s.totalSteps },
                        modifier = Modifier.fillMaxWidth()
                    )
                    Spacer(modifier = Modifier.height(16.dp))
                    Text(
                        "Step ${s.stepIndex + 1} of ${s.totalSteps}",
                        style = MaterialTheme.typography.titleMedium
                    )
                    Text(s.stepDescription)
                    Text("App: ${s.appName}", style = MaterialTheme.typography.bodySmall)
                }
                is ExecutionState.HandedOff -> {
                    Text(
                        "Control Returned",
                        style = MaterialTheme.typography.headlineMedium,
                        color = MaterialTheme.colorScheme.primary
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    Text(s.message)
                    Spacer(modifier = Modifier.height(16.dp))
                    Button(onClick = onBack) {
                        Text("Done")
                    }
                }
                is ExecutionState.Completed -> {
                    Text(
                        "Completed",
                        style = MaterialTheme.typography.headlineMedium,
                        color = MaterialTheme.colorScheme.primary
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    Text(s.message)
                    Spacer(modifier = Modifier.height(16.dp))
                    Button(onClick = onBack) {
                        Text("Done")
                    }
                }
                is ExecutionState.Failed -> {
                    Text(
                        "Execution Failed",
                        style = MaterialTheme.typography.headlineMedium,
                        color = MaterialTheme.colorScheme.error
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    Text(s.error)
                    Spacer(modifier = Modifier.height(16.dp))
                    Button(onClick = onBack) {
                        Text("Back")
                    }
                }
            }
        }
    }
}
