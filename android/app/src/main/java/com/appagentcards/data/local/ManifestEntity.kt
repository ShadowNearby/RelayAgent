package com.appagentcards.data.local

import androidx.room.Entity
import androidx.room.PrimaryKey

@Entity(tableName = "manifests")
data class ManifestEntity(
    @PrimaryKey val appId: String,
    val yamlContent: String,
    val fetchedAtMillis: Long = System.currentTimeMillis(),
    val source: String = "assets"
)
