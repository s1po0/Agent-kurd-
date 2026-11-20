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

val githubOAuthClientId = providers.environmentVariable("KURD_AGENT_GITHUB_OAUTH_CLIENT_ID").orElse("").get()
val hfOAuthClientId = providers.environmentVariable("KURD_AGENT_HF_OAUTH_CLIENT_ID").orElse("").get()

android {
    namespace = "com.kurdagent.app"
    compileSdk = 37
    buildFeatures { buildConfig = true }

    defaultConfig {
        applicationId = "com.kurdagent.app"
        buildConfigField("String", "GITHUB_OAUTH_CLIENT_ID", "\"$githubOAuthClientId\"")
        buildConfigField("String", "HF_OAUTH_CLIENT_ID", "\"$hfOAuthClientId\"")
        minSdk = 26
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

    sourceSets.getByName("main").assets.srcDir(generatedAssetRoot.get().asFile)

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

kotlin {
    compilerOptions {
        jvmTarget = org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17
    }
}

chaquopy {
    defaultConfig { version = "3.13" }
}

dependencies {
    implementation(files("libs/llama-kotlin-0.3.0.aar"))
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.10.2")
}

tasks.named("preBuild").configure { dependsOn(copyKurdAgentWeb) }
