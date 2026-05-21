package com.appagentcards.domain.engine

import javax.inject.Inject

class PromptTemplateEngine @Inject constructor() {

    fun render(
        template: String,
        userPrompt: String,
        capabilityId: String,
        userLocale: String = "zh-CN"
    ): String {
        return template
            .replace("{{user_prompt}}", userPrompt)
            .replace("{{capability_id}}", capabilityId)
            .replace("{{user_locale}}", userLocale)
    }
}
