package com.appagentcards.data.repository

import com.appagentcards.data.local.ManifestAssetLoader
import com.appagentcards.data.local.ManifestDao
import com.appagentcards.data.local.ManifestEntity
import com.appagentcards.data.parser.ManifestParser
import com.appagentcards.data.remote.ManifestRemoteDataSource
import com.appagentcards.domain.model.Card
import com.appagentcards.domain.model.CardSummary
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import javax.inject.Inject
import javax.inject.Singleton

@Singleton
class ManifestRepository @Inject constructor(
    private val assetLoader: ManifestAssetLoader,
    private val remoteDataSource: ManifestRemoteDataSource,
    private val dao: ManifestDao,
    private val parser: ManifestParser
) {
    suspend fun loadAllCards(): Result<List<Card>> = withContext(Dispatchers.IO) {
        runCatching {
            val cached = dao.getAll()
            if (cached.isNotEmpty()) {
                cached.map { parser.parse(it.yamlContent) }
            } else {
                val fromAssets = assetLoader.loadAll()
                if (fromAssets.isEmpty()) {
                    throw IllegalStateException("No manifests found in assets or cache")
                }
                val entities = fromAssets.map { yaml ->
                    val card = parser.parse(yaml)
                    ManifestEntity(
                        appId = card.appId,
                        yamlContent = yaml,
                        source = "assets"
                    )
                }
                dao.insertAll(entities)
                fromAssets.map { parser.parse(it) }
            }
        }
    }

    suspend fun loadCard(appId: String): Result<Card> = withContext(Dispatchers.IO) {
        runCatching {
            val entity = dao.getByAppId(appId)
                ?: throw IllegalStateException("Card not found: $appId")
            parser.parse(entity.yamlContent)
        }
    }

    suspend fun refreshFromRemote(): Result<Int> = withContext(Dispatchers.IO) {
        runCatching {
            val yamlList = remoteDataSource.fetchAll()
            var updated = 0
            val entities = yamlList.mapNotNull { yaml ->
                try {
                    val card = parser.parse(yaml)
                    val existing = dao.getByAppId(card.appId)
                    if (existing == null || existing.yamlContent != yaml) {
                        updated++
                    }
                    ManifestEntity(
                        appId = card.appId,
                        yamlContent = yaml,
                        source = "remote"
                    )
                } catch (e: Exception) {
                    null
                }
            }
            if (entities.isNotEmpty()) {
                dao.insertAll(entities)
            }
            updated
        }
    }

    suspend fun listAvailableApps(): List<CardSummary> = withContext(Dispatchers.IO) {
        loadAllCards().getOrDefault(emptyList()).map { card ->
            CardSummary(
                appId = card.appId,
                appName = card.appName,
                agentName = card.embeddedAgent.name,
                agentType = card.embeddedAgent.type
            )
        }
    }
}
