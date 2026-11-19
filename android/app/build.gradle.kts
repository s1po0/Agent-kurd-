plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

val stableKeystorePath = System.getenv("KURD_AGENT_KEYSTORE_PATH")
val stableKeystorePassword = System.getenv("KURD_AGENT_KEYSTORE_PASSWORD")
val stableKeyAlias = System.getenv("KURD_AGENT_KEY_ALIAS")
val useStableSigning = !stableKeystorePath.isNullOrBlank() && !stableKeystorePassword.isNullOrBlank() && !stableKeyAlias.isNullOrBlank()

val generatedAssetRoot = layout.buildDirectory.dir("generated/kurd-agent-assets")
val generatedWebAssets = generatedAssetRoot.map { it.dir("www") }
val copyKurdAgentWeb by tasks.registering(Copy::class) {
    from(rootProject.layout.projectDirectory.dir("../")) {
        include("server.py", "index.html", "app.js", "styles.css")
    }
    into(generatedWebAssets)
}

android {
    namespace = "com.kurdagent.app"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.kurdagent.app"
        minSdk = 24
        targetSdk = 28
        val buildNumber = System.getenv("GITHUB_RUN_NUMBER")?.toIntOrNull() ?: 1
        versionCode = buildNumber
        versionName = "0.1.$buildNumber"
        ndk { abiFilters += listOf("arm64-v8a", "x86_64") }
    }

    if (useStableSigning) {
        signingConfigs.create("stable") {
            storeFile = file(stableKeystorePath!!)
            storeType = "PKCS12"
            storePassword = stableKeystorePassword!!
            keyAlias = stableKeyAlias!!
            keyPassword = stableKeystorePassword!!
        }
    }

    buildTypes.getByName("debug") {
        if (useStableSigning) signingConfig = signingConfigs.getByName("stable")
    }

    sourceSets.getByName("main").assets.srcDir(generatedAssetRoot)

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}

chaquopy {
    defaultConfig { version = "3.13" }
}

tasks.named("preBuild").configure { dependsOn(copyKurdAgentWeb) }
