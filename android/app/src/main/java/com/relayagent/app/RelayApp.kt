package com.relayagent.app

import android.app.Application

class RelayApp : Application() {
    override fun onCreate() {
        super.onCreate()
        DeviceBridge.init(this)
        AssetInstaller.ensureInstalled(this)
    }
}
