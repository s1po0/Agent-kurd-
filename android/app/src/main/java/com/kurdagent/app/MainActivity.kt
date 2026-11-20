package com.kurdagent.app

import android.app.Activity
import android.content.Intent
import android.graphics.Color
import android.net.Uri
import android.os.Bundle
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.webkit.WebChromeClient
import android.widget.FrameLayout
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.io.File

class MainActivity : Activity() {
    private lateinit var webView: WebView
    private lateinit var progress: ProgressBar
    private var localAiBridge: LocalAiBridge? = null

    override fun onResume() {
        super.onResume()
        val downloadActive = getSharedPreferences("kurd_agent_background", MODE_PRIVATE)
            .getBoolean("model_download_active", false)
        if (!downloadActive) stopService(Intent(this, OAuthKeepAliveService::class.java))
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE)
        window.statusBarColor = Color.rgb(10, 14, 20)
        window.navigationBarColor = Color.rgb(10, 14, 20)

        val root = FrameLayout(this).apply { setBackgroundColor(Color.rgb(10, 14, 20)) }
        webView = WebView(this).apply {
            setBackgroundColor(Color.rgb(10, 14, 20))
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.databaseEnabled = true
            settings.allowFileAccess = false
            settings.allowContentAccess = false
            settings.javaScriptCanOpenWindowsAutomatically = false
            settings.setSupportMultipleWindows(false)
            webViewClient = object : WebViewClient() {
                override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                    val uri = request.url
                    if (uri.host == "127.0.0.1" && uri.port == 8000) return false
                    return openExternal(uri)
                }
            }
            webChromeClient = object : WebChromeClient() {
                override fun onProgressChanged(view: WebView, newProgress: Int) {
                    this@MainActivity.progress.progress = newProgress
                    this@MainActivity.progress.visibility = if (newProgress >= 100) View.GONE else View.VISIBLE
                }
            }
        }
        progress = ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal).apply {
            max = 100
            progress = 0
            progressTintList = android.content.res.ColorStateList.valueOf(Color.rgb(169, 229, 191))
            progressBackgroundTintList = android.content.res.ColorStateList.valueOf(Color.rgb(31, 42, 52))
        }
        root.addView(webView, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
        root.addView(progress, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, 3))
        setContentView(root)

        try {
            val modelDirectory = File(filesDir, "kurd-agent/data/models").apply { mkdirs() }
            localAiBridge = LocalAiBridge(this, webView, modelDirectory)
            webView.addJavascriptInterface(localAiBridge!!, "KurdLocalAI")
            copyWebAssets()
            if (!Python.isStarted()) Python.start(AndroidPlatform(applicationContext))
            Python.getInstance().getModule("android_server").callAttr(
                "start_server", filesDir.absolutePath, BuildConfig.HF_OAUTH_CLIENT_ID
            )
            webView.postDelayed({ webView.loadUrl("http://127.0.0.1:8000/") }, 250)
        } catch (error: Exception) {
            Toast.makeText(this, "Kurd Agent startup failed: ${error.message}", Toast.LENGTH_LONG).show()
            setContentView(TextView(this).apply {
                text = "Kurd Agent could not start\\n\\n${error.message}\\n\\nTap to retry"
                setTextColor(Color.WHITE)
                textSize = 16f
                gravity = Gravity.CENTER
                setPadding(28, 28, 28, 28)
                setBackgroundColor(Color.rgb(10, 14, 20))
                setOnClickListener { recreate() }
            })
        }
    }

    private fun copyWebAssets() {
        val target = File(filesDir, "kurd-agent/www")
        target.mkdirs()
        assets.list("www").orEmpty().forEach { name ->
            val output = File(target, name).outputStream()
            assets.open("www/$name").use { input -> output.use { input.copyTo(it) } }
        }
    }

    private fun openExternal(uri: Uri): Boolean {
        return try {
            startActivity(Intent(Intent.ACTION_VIEW, uri))
            true
        } catch (_: Exception) {
            true
        }
    }

    @Deprecated("Deprecated by Android; retained for back navigation compatibility")
    override fun onBackPressed() {
        if (this::webView.isInitialized && webView.canGoBack()) webView.goBack() else super.onBackPressed()
    }

    override fun onDestroy() {
        localAiBridge?.close()
        if (this::webView.isInitialized) webView.destroy()
        super.onDestroy()
    }
}
