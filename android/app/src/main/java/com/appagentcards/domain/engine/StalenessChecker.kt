package com.appagentcards.domain.engine

import com.appagentcards.domain.model.Card
import java.time.LocalDate
import java.time.format.DateTimeFormatter
import java.time.temporal.ChronoUnit
import javax.inject.Inject

class StalenessChecker @Inject constructor() {

    companion object {
        private const val STALE_DAYS = 90
    }

    fun check(card: Card): StalenessResult {
        val reason = mutableListOf<String>()

        val lastVerified = try {
            LocalDate.parse(card.provenance.lastVerified, DateTimeFormatter.ISO_DATE)
        } catch (e: Exception) {
            return StalenessResult.Stale("Cannot parse last_verified date: ${card.provenance.lastVerified}")
        }

        val daysSince = ChronoUnit.DAYS.between(lastVerified, LocalDate.now())
        if (daysSince > STALE_DAYS) {
            reason.add("Last verified $daysSince days ago (threshold: $STALE_DAYS days)")
        }

        return if (reason.isEmpty()) {
            StalenessResult.Fresh
        } else {
            StalenessResult.Stale(reason.joinToString("; "))
        }
    }
}

sealed class StalenessResult {
    object Fresh : StalenessResult()
    data class Stale(val reason: String) : StalenessResult()
}
