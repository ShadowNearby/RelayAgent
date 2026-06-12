"""Android-side glue between the RelayAgent runtime (agents/, synced into the
APK at build time) and the Kotlin device layer (DeviceBridge).

- backend.py      AndroidBackend — device primitives over DeviceBridge
- interaction.py  OverlayInteraction — ask_user / status chip / stop button
- entry.py        run_single / run_flow — the Kotlin-callable entrypoints
"""
