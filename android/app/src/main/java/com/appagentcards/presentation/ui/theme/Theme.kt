package com.appagentcards.presentation.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable

private val LightColorScheme = lightColorScheme(
    primary = Blue700,
    onPrimary = Gray50,
    primaryContainer = Blue600,
    secondary = Green500,
    tertiary = Orange500,
    error = Red500,
    background = Gray50,
    surface = Gray50,
    onBackground = Gray900,
    onSurface = Gray900
)

@Composable
fun AppAgentCardsTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = LightColorScheme,
        typography = AppTypography,
        content = content
    )
}
