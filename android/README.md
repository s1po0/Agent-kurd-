# Kurd Agent for Android

The Android APK embeds the existing Python API and Kurdish web UI in a WebView. It stores app settings and GitHub credentials in Android's private app storage.

## Build a debug APK

Use JDK 17 and Android SDK 35, then from this directory:

```bash
gradle --no-daemon :app:assembleDebug
```

The installable, debug-signed APK is produced at:

```text
app/build/outputs/apk/debug/app-debug.apk
```

A GitHub Actions workflow builds the APK and publishes it as the `Kurd-Agent-Android-APK` artifact on pushes to the project branch or by manual dispatch.

## Upgrade-safe signing

For Android to install future APKs over an existing installation, each build must use the same signing keystore and a higher version code. The workflow supports a persistent PKCS#12 keystore through these **repository Actions secrets**: `KURD_AGENT_KEYSTORE_BASE64`, `KURD_AGENT_KEYSTORE_PASSWORD`, and `KURD_AGENT_KEY_ALIAS`. Without them, GitHub's temporary debug key is used, so the APK will not be upgrade-compatible between CI runs. Never commit the private keystore or its password to this repository. Keep a secure backup of the signing key: losing it means users must uninstall before switching keys. After adding the three secrets, any Android-source push triggers a new signed build and updates the downloadable release.

## Model engine note

The APK contains the Kurd Agent interface and API, **not model weights or a native Android inference engine**. The model provider remains configurable in Settings. To run inference without sending prompts to a cloud provider, point it at an Ollama-compatible service reachable from the phone. Hugging Face model download through this app currently uses Ollama's GGUF import endpoint, so the Android device also needs an Ollama-compatible endpoint for that download route; desktop Ollama is the tested target. The first model download needs internet and storage, then a local inference service can run offline.

Do not expose a model endpoint or this app's local API to the public internet without adding authentication and TLS.
