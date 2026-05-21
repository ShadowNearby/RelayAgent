package com.appagentcards.data.local

import androidx.room.Database
import androidx.room.RoomDatabase

@Database(entities = [ManifestEntity::class], version = 1, exportSchema = false)
abstract class ManifestDatabase : RoomDatabase() {
    abstract fun manifestDao(): ManifestDao
}
