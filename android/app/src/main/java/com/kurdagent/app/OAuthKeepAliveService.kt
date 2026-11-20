package com.kurdagent.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.os.IBinder

/** Keeps the loopback callback server alive while GitHub or Hugging Face is open in the browser. */
class OAuthKeepAliveService : Service() {
    override fun onCreate() {
        super.onCreate()
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "Account connection", NotificationManager.IMPORTANCE_LOW).apply {
                description = "Keeps Kurd Agent's local sign-in callback available while you authorize an account."
            }
        )
        val openApp = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        val notification = Notification.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.stat_notify_sync)
            .setContentTitle("Kurd Agent connection in progress")
            .setContentText("Return to Kurd Agent after approving access in your browser.")
            .setContentIntent(openApp)
            .setOngoing(true)
            .setCategory(Notification.CATEGORY_SERVICE)
            .build()
        startForeground(NOTIFICATION_ID, notification)
    }

    override fun onBind(intent: Intent?): IBinder? = null

    companion object {
        private const val CHANNEL_ID = "kurd_agent_account_connection"
        private const val NOTIFICATION_ID = 7104
    }
}
