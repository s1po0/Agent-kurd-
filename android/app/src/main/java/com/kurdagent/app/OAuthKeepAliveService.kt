package com.kurdagent.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.os.IBinder
import android.os.PowerManager

/** Keeps Kurd Agent's local server alive for browser callbacks and model downloads. */
class OAuthKeepAliveService : Service() {
    private var wakeLock: PowerManager.WakeLock? = null

    override fun onCreate() {
        super.onCreate()
        getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "Kurd Agent background task", NotificationManager.IMPORTANCE_LOW).apply {
                description = "Shows when Kurd Agent is downloading a model or completing account authorization."
            }
        )
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val downloading = intent?.getBooleanExtra(EXTRA_MODEL_DOWNLOAD, false) == true
        val openApp = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        val notification = Notification.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setContentTitle(if (downloading) "Kurd Agent model download" else "Kurd Agent connection in progress")
            .setContentText(if (downloading) "Download continues while Kurd Agent is in the background." else "Return to Kurd Agent after approving access in your browser.")
            .setContentIntent(openApp)
            .setOngoing(true)
            .setCategory(Notification.CATEGORY_SERVICE)
            .build()
        startForeground(NOTIFICATION_ID, notification)
        if (downloading) acquireDownloadWakeLock() else releaseDownloadWakeLock()
        return START_NOT_STICKY
    }

    private fun acquireDownloadWakeLock() {
        if (wakeLock?.isHeld == true) return
        val power = getSystemService(PowerManager::class.java)
        wakeLock = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "KurdAgent:ModelDownload").apply {
            setReferenceCounted(false)
            acquire(4 * 60 * 60 * 1000L)
        }
    }

    private fun releaseDownloadWakeLock() {
        wakeLock?.let { if (it.isHeld) it.release() }
        wakeLock = null
    }

    override fun onDestroy() {
        releaseDownloadWakeLock()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    companion object {
        const val EXTRA_MODEL_DOWNLOAD = "model_download"
        private const val CHANNEL_ID = "kurd_agent_background_task"
        private const val NOTIFICATION_ID = 7104
    }
}
