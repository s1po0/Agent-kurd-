package com.kurdagent.app

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.webkit.JavascriptInterface
import android.webkit.WebView
import com.thalzeronlabs.llama.ChatMessage
import com.thalzeronlabs.llama.LlamaModel
import kotlinx.coroutines.flow.collect
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.concurrent.Executors

/** JavaScript bridge for genuine on-device GGUF inference. Model files remain in app-private storage. */
class LocalAiBridge(
    private val activity: Activity,
    private val webView: WebView,
    private val modelDirectory: File,
) {
    private val worker = Executors.newSingleThreadExecutor()
    @Volatile private var model: LlamaModel? = null
    @Volatile private var loadedPath: String? = null

    @JavascriptInterface
    fun openVerificationUrl(url: String) {
        val uri = Uri.parse(url)
        if (uri.scheme != "https" || uri.host !in setOf("github.com", "huggingface.co")) return
        try { activity.startActivity(Intent(Intent.ACTION_VIEW, uri)) } catch (_: Exception) { }
    }

    @JavascriptInterface
    fun loadModel(path: String, callbackId: String) {
        worker.execute {
            try {
                val root = modelDirectory.canonicalFile
                val file = File(path).canonicalFile
                require(file.path.startsWith(root.path + File.separator) && file.isFile && file.extension.equals("gguf", true)) {
                    "Choose a GGUF model downloaded into Kurd Agent's private storage."
                }
                if (loadedPath != file.path) {
                    model?.close()
                    model = null
                    loadedPath = null
                    model = runBlocking { LlamaModel.load(file) }
                    loadedPath = file.path
                }
                respond(callbackId, JSONObject().put("ok", true).toString())
            } catch (error: Throwable) {
                respond(callbackId, JSONObject().put("ok", false).put("error", error.message ?: "Could not load this GGUF model").toString())
            }
        }
    }

    @JavascriptInterface
    fun chat(messagesJson: String, callbackId: String) {
        worker.execute {
            try {
                val activeModel = checkNotNull(model) { "Load a downloaded GGUF model in Settings first." }
                val request = JSONObject(messagesJson)
                val array = request.optJSONArray("messages") ?: JSONArray()
                require(array.length() in 1..80) { "Conversation is empty or too long." }
                val temperature = request.optDouble("temperature", 0.7).toFloat().coerceIn(0f, 2f)
                val messages = (0 until array.length()).map { index ->
                    val item = array.getJSONObject(index)
                    val content = item.optString("content").take(20_000)
                    when (item.optString("role")) {
                        "system" -> ChatMessage.system(content)
                        "assistant" -> ChatMessage.assistant(content)
                        else -> ChatMessage.user(content)
                    }
                }
                val answer = StringBuilder()
                runBlocking {
                    activeModel.newSession().use { session ->
                        session.chat(messages, maxTokens = 384, temperature = temperature).collect { answer.append(it) }
                    }
                }
                respond(callbackId, JSONObject().put("ok", true).put("reply", answer.toString()).toString())
            } catch (error: Throwable) {
                respond(callbackId, JSONObject().put("ok", false).put("error", error.message ?: "Local inference failed").toString())
            }
        }
    }

    private fun respond(callbackId: String, json: String) {
        if (!callbackId.matches(Regex("[A-Za-z0-9_-]{1,80}"))) return
        webView.post {
            webView.evaluateJavascript("window.onKurdLocalAiResult('$callbackId',${JSONObject.quote(json)})", null)
        }
    }

    fun close() {
        worker.execute { model?.close(); model = null; loadedPath = null }
        worker.shutdown()
    }
}
