package com.appagentcards.presentation.navigation

import androidx.compose.runtime.Composable
import androidx.navigation.NavHostController
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable

object Routes {
    const val HOME = "home"
    const val ROUTING = "routing"
    const val EXECUTION = "execution"
    const val SETTINGS = "settings"
}

@Composable
fun AppNavGraph(navController: NavHostController) {
    NavHost(navController = navController, startDestination = Routes.HOME) {
        composable(Routes.HOME) {
            com.appagentcards.presentation.ui.home.HomeScreen(
                onNavigateToRouting = { navController.navigate(Routes.ROUTING) },
                onNavigateToExecution = { navController.navigate(Routes.EXECUTION) },
                onNavigateToSettings = { navController.navigate(Routes.SETTINGS) }
            )
        }
        composable(Routes.ROUTING) {
            com.appagentcards.presentation.ui.routing.RoutingResultScreen(
                onBack = { navController.popBackStack() },
                onConfirmExecution = { navController.navigate(Routes.EXECUTION) }
            )
        }
        composable(Routes.EXECUTION) {
            com.appagentcards.presentation.ui.execution.ExecutionScreen(
                onBack = { navController.popBackStack(Routes.HOME, false) }
            )
        }
        composable(Routes.SETTINGS) {
            com.appagentcards.presentation.ui.settings.SettingsScreen(
                onBack = { navController.popBackStack() }
            )
        }
    }
}
