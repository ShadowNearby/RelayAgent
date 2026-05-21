package com.appagentcards.domain.engine

import com.appagentcards.domain.model.Capability
import javax.inject.Inject

class IntentMatcher @Inject constructor() {

    private val latinWord = Regex("[a-z0-9]+")

    fun tokenize(text: String): Set<String> {
        val lower = text.lowercase()
        val words = latinWord.findAll(lower).map { it.value }.toSet()
        val chars = lower.filter { it in '一'..'鿿' }.map { it.toString() }.toSet()
        return words + chars
    }

    fun scoreCapability(promptTokens: Set<String>, capability: Capability): Int {
        var s = 0
        for (example in capability.examplePrompts) {
            val matched = (promptTokens intersect tokenize(example)).size
            s += 3 * matched
        }
        s += (promptTokens intersect tokenize(capability.description)).size
        return s
    }
}
