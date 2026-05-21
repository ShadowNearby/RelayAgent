package com.appagentcards.data.local

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import kotlinx.coroutines.flow.Flow

@Dao
interface ManifestDao {

    @Query("SELECT * FROM manifests")
    suspend fun getAll(): List<ManifestEntity>

    @Query("SELECT * FROM manifests")
    fun observeAll(): Flow<List<ManifestEntity>>

    @Query("SELECT * FROM manifests WHERE appId = :appId")
    suspend fun getByAppId(appId: String): ManifestEntity?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertAll(entities: List<ManifestEntity>)

    @Query("DELETE FROM manifests")
    suspend fun deleteAll()

    @Query("SELECT COUNT(*) FROM manifests")
    suspend fun count(): Int
}
