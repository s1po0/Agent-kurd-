# Kurd Agent for Android

The Android APK embeds the Kurd Agent Python API and Kurdish web UI in a WebView. It also includes a llama.cpp-based native engine for GGUF models and stores settings, Hugging Face credentials, and model files in Android's private app storage. Minimum supported Android version is API 26 (Android 8.0); supported native ABIs are `arm64-v8a` and `x86_64`.

## Build a debug APK

Use JDK 17 and Android SDK 37. The build needs the upstream `llama-kotlin` 0.3.0 AAR in `app/libs/`; CI verifies its preinstalled SDK 37, downloads the AAR, and SHA-256-verifies it automatically. For a manual build:

```bash
mkdir -p app/libs
curl -fL https://github.com/ThalzeronLabs/llama-kotlin/releases/download/v0.3.0/llama-kotlin-0.3.0.aar -o app/libs/llama-kotlin-0.3.0.aar
echo '5a4af6eeaef31d580adbfb1914b117aff5ff3da79fff4012f70d3c3e4b456d16  app/libs/llama-kotlin-0.3.0.aar' | sha256sum -c
gradle --no-daemon :app:assembleDebug
```

The installable APK is produced at `app/build/outputs/apk/debug/app-debug.apk`. The AAR is downloaded at build time rather than checked into Git. The upstream library is Apache-2.0 licensed and includes its third-party notices in the APK.

A GitHub Actions workflow builds the APK and publishes it as the `Kurd-Agent-Android-APK` artifact on pushes to the project branch or by manual dispatch. GitHub repository access uses a GitHub App (not a broad-scope OAuth App). Register a Kurd Agent GitHub App in GitHub Settings → Developer settings → GitHub Apps, enable device flow, and grant only the repository permissions needed: Contents read/write, Workflows read/write, Actions read/write, and Pull requests read/write; Metadata read is required by GitHub. Add the App's public Client ID and URL slug as repository Actions variables `KURD_AGENT_GITHUB_APP_CLIENT_ID` and `KURD_AGENT_GITHUB_APP_SLUG`. Do not add a client secret or private key. In Kurd Agent → Connections, install the App from its official GitHub page and choose **Only select repositories**, then select the one repository to authorize; afterward, use GitHub device login. The GitHub App installation limits the token to repositories selected there and the permissions configured for the App. The existing Arena AI Coding Agent App belongs to Arena and is separate; it cannot be reused as Kurd Agent's identity. No user token or App private key is bundled. Hugging Face uses the public OAuth client ID Actions variable `KURD_AGENT_HF_OAUTH_CLIENT_ID` and the scopes `openid`, `profile`, `read-repos`, and `gated-repos`. Add only public client IDs/slugs as repository Actions variables, then trigger a build. For GitHub tools, the chat model must support tool calling; commits are limited to feature branches and pull requests are never auto-merged.

## Use Hugging Face and offline local AI

In Settings, select **Android Local AI**, optionally connect a Hugging Face user access token, enter a model repository (`owner/model`), and choose a `.gguf` file. Public repositories can be searched/downloaded without signing in. A read token is needed for private or gated repositories and is validated with Hugging Face before it is saved. The token is kept in app-private storage and is not sent with ordinary chat requests.

Downloading model weights requires internet and sufficient free storage. Once a GGUF is fully downloaded to the phone, inference runs through llama.cpp on the device; chat does not need the network. Model weights are not included in the APK. Start with a small quantized model and check its size, license, and memory requirements—large models may be too slow or may exceed a phone's available RAM. Only `arm64-v8a` and `x86_64` Android devices are supported by this build.

The existing Ollama and OpenAI-compatible provider choices remain available; Ollama's Hugging Face import flow is separate from Android Local AI.

## Upgrade-safe signing

For Android to install future APKs over an existing installation, each build must use the same signing keystore and a higher version code. The workflow supports a persistent PKCS#12 keystore through these **repository Actions secrets**: `KURD_AGENT_KEYSTORE_BASE64`, `KURD_AGENT_KEYSTORE_PASSWORD`, and `KURD_AGENT_KEY_ALIAS`. Without them, GitHub's temporary debug key is used, so the APK will not be upgrade-compatible between CI runs. Never commit the private keystore or its password to this repository. Keep a secure backup of the signing key: losing it means users must uninstall before switching keys. After adding the three secrets, any Android-source push triggers a new signed build and updates the downloadable release.

Do not expose a model endpoint or this app's local API to the public internet without adding authentication and TLS.
