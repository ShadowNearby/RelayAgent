package com.appagentcards.presentation.ui.home

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.hilt.navigation.compose.hiltViewModel
import com.appagentcards.domain.model.Capability
import com.appagentcards.domain.model.RoutingCandidate
import com.appagentcards.domain.model.SideEffect

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun HomeScreen(
    onNavigateToRouting: () -> Unit,
    onNavigateToExecution: () -> Unit,
    onNavigateToSettings: () -> Unit,
    viewModel: HomeViewModel = hiltViewModel()
) {
    val uiState by viewModel.uiState.collectAsState()
    val prompt by viewModel.prompt.collectAsState()

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("AppAgentCards") },
                actions = {
                    IconButton(onClick = onNavigateToSettings) {
                        Icon(Icons.Default.Settings, contentDescription = "Settings")
                    }
                }
            )
        }
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(horizontal = 16.dp)
        ) {
            Spacer(modifier = Modifier.height(24.dp))

            Text(
                text = "What do you want to do?",
                style = MaterialTheme.typography.headlineMedium
            )

            Spacer(modifier = Modifier.height(16.dp))

            OutlinedTextField(
                value = prompt,
                onValueChange = viewModel::onPromptChanged,
                modifier = Modifier.fillMaxWidth(),
                placeholder = { Text("e.g. 叫一辆车去虹桥机场") },
                singleLine = true,
                trailingIcon = {
                    IconButton(onClick = { viewModel.onSearch() }) {
                        Icon(Icons.Default.Search, contentDescription = "Search")
                    }
                }
            )

            Spacer(modifier = Modifier.height(8.dp))

            when (val state = uiState) {
                is HomeUiState.Idle -> {
                    Text(
                        text = "Enter a request above to find the best in-app AI assistant.",
                        style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        textAlign = TextAlign.Center,
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(top = 32.dp)
                    )
                }
                is HomeUiState.Loading -> {
                    Box(
                        modifier = Modifier.fillMaxWidth(),
                        contentAlignment = Alignment.Center
                    ) {
                        CircularProgressIndicator(modifier = Modifier.padding(32.dp))
                    }
                }
                is HomeUiState.Candidates -> {
                    Text(
                        text = "Found ${state.candidates.size} match(es):",
                        style = MaterialTheme.typography.titleMedium
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    LazyColumn(
                        verticalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        items(state.candidates) { candidate ->
                            CandidateCard(
                                candidate = candidate,
                                onClick = { viewModel.onSelectCandidate(candidate) }
                            )
                        }
                    }
                }
                is HomeUiState.PlanReady -> {
                    PlanReadyContent(
                        state = state,
                        onConfirm = viewModel::onConfirmExecution,
                        onCancel = viewModel::reset
                    )
                }
                is HomeUiState.Executing -> {
                    onNavigateToExecution()
                }
                is HomeUiState.Error -> {
                    Text(
                        text = state.message,
                        style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.error,
                        modifier = Modifier.padding(top = 16.dp)
                    )
                }
            }
        }
    }
}

@Composable
private fun CandidateCard(candidate: RoutingCandidate, onClick: () -> Unit) {
    Card(
        onClick = onClick,
        modifier = Modifier.fillMaxWidth()
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text(
                    text = "${candidate.cardSummary.appName} · ${candidate.cardSummary.agentName}",
                    style = MaterialTheme.typography.titleMedium
                )
                Text(
                    text = "Score: ${candidate.score}",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.primary
                )
            }
            Spacer(modifier = Modifier.height(4.dp))
            Text(
                text = candidate.capability.description,
                style = MaterialTheme.typography.bodyMedium,
                maxLines = 3
            )
            Spacer(modifier = Modifier.height(8.dp))
            CapabilityBadges(candidate.capability)
        }
    }
}

@Composable
fun CapabilityBadges(capability: Capability) {
    Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
        val badges = buildList {
            if (capability.handoffToUserRequired) add("Needs Handoff" to BadgeColor.WARNING)
            if (!capability.executable) add("Info Only" to BadgeColor.INFO)
            if (SideEffect.PAYMENT in capability.sideEffects) add("Payment" to BadgeColor.DANGER)
            if (capability.requiresLogin) add("Login Required" to BadgeColor.INFO)
        }
        badges.forEach { (label, color) ->
            AssistChip(
                onClick = {},
                label = {
                    Text(
                        label,
                        style = MaterialTheme.typography.labelSmall
                    )
                }
            )
        }
    }
}

enum class BadgeColor { INFO, WARNING, DANGER }

@Composable
private fun PlanReadyContent(
    state: HomeUiState.PlanReady,
    onConfirm: () -> Unit,
    onCancel: () -> Unit
) {
    val plan = state.plan
    val safety = plan.safetyAssessment

    Column(modifier = Modifier.fillMaxWidth()) {
        Text("Execution Plan", style = MaterialTheme.typography.titleLarge)
        Spacer(modifier = Modifier.height(8.dp))

        Text("App: ${plan.card.appName} (${plan.card.appId})")
        Text("Agent: ${plan.card.embeddedAgent.name}")
        Text("Capability: ${plan.capability.id}")

        Spacer(modifier = Modifier.height(8.dp))

        if (state.isStale) {
            Card(
                colors = CardDefaults.cardColors(
                    containerColor = MaterialTheme.colorScheme.errorContainer
                )
            ) {
                Text(
                    "This card may be stale. Proceed with caution.",
                    modifier = Modifier.padding(12.dp),
                    style = MaterialTheme.typography.bodySmall
                )
            }
        }

        if (safety.warnings.isNotEmpty()) {
            Spacer(modifier = Modifier.height(8.dp))
            safety.warnings.forEach { warning ->
                Text(
                    "WARNING: $warning",
                    color = MaterialTheme.colorScheme.error,
                    style = MaterialTheme.typography.bodySmall
                )
            }
        }

        Spacer(modifier = Modifier.height(16.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            Button(onClick = onConfirm) {
                Text("Execute")
            }
            OutlinedButton(onClick = onCancel) {
                Text("Cancel")
            }
        }
    }
}
