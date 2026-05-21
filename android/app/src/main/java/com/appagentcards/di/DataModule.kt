package com.appagentcards.di

import android.content.Context
import androidx.room.Room
import com.appagentcards.data.local.ManifestDao
import com.appagentcards.data.local.ManifestDatabase
import dagger.Module
import dagger.Provides
import dagger.hilt.InstallIn
import dagger.hilt.android.qualifiers.ApplicationContext
import dagger.hilt.components.SingletonComponent
import javax.inject.Singleton

@Module
@InstallIn(SingletonComponent::class)
object DataModule {

    @Provides
    @Singleton
    fun provideManifestDatabase(@ApplicationContext context: Context): ManifestDatabase =
        Room.databaseBuilder(
            context,
            ManifestDatabase::class.java,
            "appagentcards.db"
        ).build()

    @Provides
    @Singleton
    fun provideManifestDao(database: ManifestDatabase): ManifestDao =
        database.manifestDao()
}
