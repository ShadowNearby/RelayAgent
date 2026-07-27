plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

// ---------------------------------------------------------------------------
// Pull the shared Python runtime (agents/) and data assets (manifests/,
// capability matrix) from the repo root into the build, so the app always
// ships the same code the host runs. Copy-at-build (not symlink) keeps the
// packaged set explicit.
// ---------------------------------------------------------------------------
val repoRoot = rootProject.layout.projectDirectory.dir("..")

val syncRelayPython by tasks.registering(Sync::class) {
    from(repoRoot.dir("agents")) {
        into("agents")
        exclude("**/__pycache__/**")
        // Host-only module: the MobileWorld LLM probe (imports mobile_world)
        // is only ever loaded via scripts/_mw_probe/sitecustomize.py on the
        // host. The pattern must be **-anchored — Ant-style excludes match
        // relative to the from() root and the file lives at
        // agents/llm/mw_llm_probe.py, so a bare filename matches nothing.
        // (agents/runtime/_recorder.py stays packaged: relay_agent lazily
        // imports it when RELAY_RECORD_DIR is set.)
        exclude("**/mw_llm_probe.py")
    }
    into(layout.buildDirectory.dir("relayPython"))
}

val syncRelayAssets by tasks.registering(Sync::class) {
    from(repoRoot.dir("manifests")) {
        into("relay/manifests")
        include("*.yaml")
        exclude("_generated/**")
    }
    from(repoRoot.dir("docs")) {
        into("relay")
        include("app_capability_matrix.csv")
    }
    into(layout.buildDirectory.dir("relayAssets"))
}

tasks.named("preBuild") {
    dependsOn(syncRelayPython, syncRelayAssets)
}

// Gradle 8.7 strict task validation requires an explicit producer->consumer
// dependency, not just preBuild ordering: Chaquopy's python-source merge reads
// build/relayPython and Android's asset merge reads build/relayAssets.
tasks.matching { it.name.matches(Regex("merge.*PythonSources")) }
    .configureEach { dependsOn(syncRelayPython) }
tasks.matching { it.name.matches(Regex("merge.*Assets")) }
    .configureEach { dependsOn(syncRelayAssets) }

android {
    namespace = "com.relayagent.app"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.relayagent.app"
        // minSdk 30 (Android 11): AccessibilityService.takeScreenshot +
        // ACTION_IME_ENTER need API 30. Target devices are modern flagships.
        minSdk = 30
        targetSdk = 34
        versionCode = 1
        versionName = "0.1.0"
        // x86_64 covers the AVD emulator (docs/emulator_testing.zh.md); Chaquopy
        // only reads abiFilters from defaultConfig, so both live here. Drop
        // x86_64 for a slimmer release APK when shipping to real devices only.
        ndk { abiFilters += listOf("arm64-v8a", "x86_64") }

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    sourceSets {
        getByName("main") {
            assets.srcDir(layout.buildDirectory.dir("relayAssets"))
        }
    }

    // Release signing: only wired up when RELAY_KEYSTORE_PATH is set (CI
    // release job, or a developer signing locally). Absent -> release build
    // stays unsigned, same as before (no behavior change for local dev/CI PR
    // builds).
    val releaseKeystorePath = System.getenv("RELAY_KEYSTORE_PATH")
    signingConfigs {
        if (releaseKeystorePath != null) {
            create("release") {
                storeFile = file(releaseKeystorePath)
                storePassword = System.getenv("RELAY_KEYSTORE_PASSWORD")
                keyAlias = System.getenv("RELAY_KEY_ALIAS")
                keyPassword = System.getenv("RELAY_KEY_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false // Chaquopy + reflection; revisit later
            if (releaseKeystorePath != null) {
                signingConfig = signingConfigs.getByName("release")
            }
        }
    }

    buildFeatures {
        viewBinding = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}

chaquopy {
    defaultConfig {
        // Spike A verifies 3.12 is available in this Chaquopy release;
        // fall back to "3.11" if not (agents/ code is 3.10+-compatible).
        version = "3.12"
        pip {
            // The full on-device dependency set. No openai, no pydantic —
            // Phase 0 removed both from the runtime import chain
            // (agents/llm/llm_client.py + pure-Python JSONAction).
            install("pyyaml")
            install("pillow")
            install("loguru")
        }
    }
    sourceSets {
        getByName("main") {
            srcDir("src/main/python")
            srcDir(layout.buildDirectory.dir("relayPython"))
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.security:security-crypto:1.1.0-alpha06")
    // Material 3 components (also brings RecyclerView/ConstraintLayout in) for
    // the designed UI: app bars, cards, text fields, buttons, list rows.
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    implementation("androidx.recyclerview:recyclerview:1.3.2")

    // On-device (connected) tests — androidTest runs in the app process, so
    // the embedded CPython + packaged agents/ code and relay assets are all
    // exercisable on a real phone.
    // espresso-core >= 3.7 is required on Android 15+: older versions still
    // reflect on the removed InputManager.getInstance hidden API.
    androidTestImplementation("androidx.test.ext:junit:1.3.0")
    androidTestImplementation("androidx.test:runner:1.7.0")
    androidTestImplementation("androidx.test.espresso:espresso-core:3.7.0")
}
