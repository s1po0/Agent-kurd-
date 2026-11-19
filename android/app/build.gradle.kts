plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

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
        versionCode = 1
        versionName = "0.1.0"
        ndk { abiFilters += listOf("arm64-v8a", "x86_64") }
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
