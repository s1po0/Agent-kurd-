package com.kurdagent.app

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.StatFs
import android.provider.OpenableColumns
import android.webkit.JavascriptInterface
import android.webkit.WebView
import com.thalzeronlabs.llama.ChatMessage
import com.thalzeronlabs.llama.LlamaModel
import kotlinx.coroutines.flow.collect
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
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
        val allowedHttps = uri.scheme == "https" && uri.host in setOf("github.com", "huggingface.co")
        val allowedLocalSetup = uri.scheme == "http" && uri.host == "127.0.0.1" && uri.port == 8000 && uri.path == "/api/github/app-manifest/start-page"
        if (!allowedHttps && !allowedLocalSetup) return
        val downloadActive = activity.getSharedPreferences("kurd_agent_background", Activity.MODE_PRIVATE)
            .getBoolean("model_download_active", false)
        val keepAlive = Intent(activity, OAuthKeepAliveService::class.java)
            .putExtra(OAuthKeepAliveService.EXTRA_MODEL_DOWNLOAD, downloadActive)
        try {
            activity.startForegroundService(keepAlive)
            activity.startActivity(Intent(Intent.ACTION_VIEW, uri))
        } catch (_: Exception) {
            activity.stopService(keepAlive)
        }
    }

    @JavascriptInterface
    fun setModelDownloadActive(active: Boolean) {
        activity.getSharedPreferences("kurd_agent_background", Activity.MODE_PRIVATE)
            .edit().putBoolean("model_download_active", active).apply()
        val service = Intent(activity, OAuthKeepAliveService::class.java)
            .putExtra(OAuthKeepAliveService.EXTRA_MODEL_DOWNLOAD, active)
        if (active) {
            try { activity.startForegroundService(service) } catch (_: Exception) { }
        } else {
            activity.stopService(service)
        }
    }

    @JavascriptInterface
    fun pickGguf(@Suppress("UNUSED_PARAMETER") unused: String, callbackId: String) {
        if (!callbackId.matches(Regex("[A-Za-z0-9_-]{1,80}"))) return
        activity.runOnUiThread {
            (activity as? MainActivity)?.openGgufPicker(callbackId)
                ?: respond(callbackId, JSONObject().put("ok", false).put("error", "Phone storage picker is unavailable.").toString())
        }
    }

    fun cancelGgufImport(callbackId: String) {
        respond(callbackId, JSONObject().put("ok", true).put("cancelled", true).toString())
    }

    fun finishGgufImport(callbackId: String, error: String) {
        respond(callbackId, JSONObject().put("ok", false).put("error", error).toString())
    }

    fun importGguf(uri: Uri, callbackId: String) {
        worker.execute {
            var temporary: File? = null
            try {
                val resolver = activity.contentResolver
                var displayName = ""
                var expectedSize = -1L
                resolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE), null, null, null)?.use { cursor ->
                    if (cursor.moveToFirst()) {
                        val nameColumn = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                        val sizeColumn = cursor.getColumnIndex(OpenableColumns.SIZE)
                        if (nameColumn >= 0) displayName = cursor.getString(nameColumn).orEmpty()
                        if (sizeColumn >= 0 && !cursor.isNull(sizeColumn)) expectedSize = cursor.getLong(sizeColumn)
                    }
                }
                displayName = displayName.substringAfterLast('/').substringAfterLast('\\').trim()
                require(displayName.lowercase().endsWith(".gguf") && displayName.length <= 255) {
                    "Pick a file with the .gguf extension."
                }
                require(expectedSize != 0L) { "The selected model file is empty." }
                val reserveBytes = 64L * 1024 * 1024
                val available = StatFs(modelDirectory.absolutePath).availableBytes
                require(expectedSize < 0 || (available > reserveBytes && expectedSize <= available - reserveBytes)) {
                    "Not enough free phone storage to import this model."
                }
                val localName = "import_${java.util.UUID.randomUUID().toString().replace("-", "")}.gguf"
                temporary = File(modelDirectory, ".$localName.part")
                var copied = 0L
                resolver.openInputStream(uri)?.use { input ->
                    temporary!!.outputStream().buffered().use { output ->
                        val header = ByteArray(4)
                        var headerBytes = 0
                        while (headerBytes < header.size) {
                            val count = input.read(header, headerBytes, header.size - headerBytes)
                            if (count < 0) break
                            headerBytes += count
                        }
                        require(headerBytes == 4 && String(header, Charsets.US_ASCII) == "GGUF") {
                            "This file does not have a valid GGUF header."
                        }
                        output.write(header)
                        copied = header.size.toLong()
                        val buffer = ByteArray(1024 * 1024)
                        while (true) {
                            val count = input.read(buffer)
                            if (count < 0) break
                            require(StatFs(modelDirectory.absolutePath).availableBytes >= count + reserveBytes) {
                                "Phone storage ran low while importing the model."
                            }
                            output.write(buffer, 0, count)
                            copied += count
                        }
                    }
                } ?: throw IllegalArgumentException("Could not read the selected file from phone storage.")
                require(copied > 0 && (expectedSize < 0 || copied == expectedSize)) {
                    "The model file could not be copied completely."
                }
                val target = File(modelDirectory, localName)
                check(temporary!!.renameTo(target)) { "Could not save the model in Kurd Agent storage." }
                temporary = null
                respond(callbackId, JSONObject().put("ok", true).put("filename", displayName)
                    .put("local_filename", localName).put("size", copied).toString())
            } catch (error: Throwable) {
                temporary?.delete()
                respond(callbackId, JSONObject().put("ok", false)
                    .put("error", error.message ?: "Could not import this GGUF model.").toString())
            }
        }
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
                    withTimeout(5 * 60 * 1000L) {
                        activeModel.newSession().use { session ->
                            session.chat(messages, maxTokens = 128, temperature = temperature).collect { token ->
                                answer.append(token)
                                emitToken(callbackId, token)
                            }
                        }
                    }
                }
                respond(callbackId, JSONObject().put("ok", true).put("reply", answer.toString()).toString())
            } catch (error: Throwable) {
                val message = if (error is TimeoutCancellationException)
                    "Local AI took over five minutes. Try a smaller GGUF model, such as a 1B–3B Q4 model, and free some memory."
                else error.message ?: "Local inference failed"
                respond(callbackId, JSONObject().put("ok", false).put("error", message).toString())
            }
        }
    }

    private fun emitToken(callbackId: String, token: String) {
        if (!callbackId.matches(Regex("[A-Za-z0-9_-]{1,80}"))) return
        webView.post {
            webView.evaluateJavascript("window.onKurdLocalAiToken('$callbackId',${JSONObject.quote(token)})", null)
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
