package com.relayagent.app

import android.app.Application

class RelayApp : Application() {
    override fun onCreate() {
        super.onCreate()
        SettingsActivity.applyAppearance(this)
        DeviceBridge.init(this)
        AssetInstaller.ensureInstalled(this)
    }
}
