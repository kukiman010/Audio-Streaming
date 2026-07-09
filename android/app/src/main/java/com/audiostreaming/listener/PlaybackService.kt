package com.audiostreaming.listener

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.SystemClock
import android.support.v4.media.session.MediaSessionCompat
import android.support.v4.media.session.PlaybackStateCompat
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.media.app.NotificationCompat as MediaNotificationCompat
import io.livekit.android.AudioOptions
import io.livekit.android.AudioType
import io.livekit.android.LiveKit
import io.livekit.android.LiveKitOverrides
import io.livekit.android.events.RoomEvent
import io.livekit.android.events.collect
import io.livekit.android.room.Room
import io.livekit.android.room.track.AudioTrack
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.util.concurrent.CancellationException as JavaCancellationException

class PlaybackService : Service() {
    private val serviceScope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private var room: Room? = null
    private var eventsJob: Job? = null
    private var connectJob: Job? = null
    private var levelPollJob: Job? = null
    private var currentRoomName: String = ""
    private var activeConnectKey: String? = null
    private var isPaused = false
    private var isSessionConnected = false
    private var lastLevelBroadcastAt = 0L
    private val audioTracks = mutableListOf<AudioTrack>()
    private lateinit var mediaSession: MediaSessionCompat

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        mediaSession = MediaSessionCompat(this, TAG).apply {
            setFlags(
                MediaSessionCompat.FLAG_HANDLES_MEDIA_BUTTONS or
                    MediaSessionCompat.FLAG_HANDLES_TRANSPORT_CONTROLS,
            )
            setCallback(
                object : MediaSessionCompat.Callback() {
                    override fun onPause() {
                        pausePlayback()
                    }

                    override fun onPlay() {
                        resumePlayback()
                    }
                },
            )
            isActive = true
        }
        updatePlaybackState()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_CONNECT -> {
                val helperUrl = intent.getStringExtra(EXTRA_HELPER_URL).orEmpty()
                val livekitUrl = intent.getStringExtra(EXTRA_LIVEKIT_URL).orEmpty()
                val roomName = intent.getStringExtra(EXTRA_ROOM).orEmpty()
                val identity = intent.getStringExtra(EXTRA_IDENTITY).orEmpty()
                if (helperUrl.isBlank() || livekitUrl.isBlank() || roomName.isBlank() || identity.isBlank()) {
                    broadcastStatus(STATUS_ERROR, "Missing connection parameters")
                    stopSelf()
                    return START_NOT_STICKY
                }
                startForegroundWithType(buildNotification(getString(R.string.notification_connecting)))
                connect(helperUrl, livekitUrl, roomName, identity)
            }

            ACTION_DISCONNECT -> disconnect(stopService = true)
            ACTION_PAUSE -> pausePlayback()
            ACTION_RESUME -> resumePlayback()
        }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        releaseMediaSession()
        disconnect(stopService = false)
        serviceScope.cancel()
        super.onDestroy()
    }

    private fun connect(helperUrl: String, livekitUrl: String, roomName: String, identity: String) {
        val connectKey = listOf(helperUrl, livekitUrl, roomName, identity).joinToString("|")
        if (connectJob?.isActive == true && activeConnectKey == connectKey) {
            Log.d(TAG, "connect already in progress for $connectKey")
            return
        }

        activeConnectKey = connectKey
        connectJob = serviceScope.launch {
            broadcastStatus(STATUS_CONNECTING, "connecting")
            try {
                val tokenResponse = withContext(Dispatchers.IO) {
                    HelperApi.fetchViewerToken(helperUrl, roomName, identity)
                }

                cleanupPreviousSession()

                val liveKitRoom = LiveKit.create(
                    appContext = applicationContext,
                    overrides = LiveKitOverrides(
                        audioOptions = AudioOptions(
                            audioOutputType = AudioType.MediaAudioType(),
                        ),
                    ),
                )
                room = liveKitRoom
                currentRoomName = tokenResponse.room
                isPaused = false
                isSessionConnected = false

                liveKitRoom.connect(livekitUrl, tokenResponse.token)
                isSessionConnected = true

                eventsJob = launch {
                    liveKitRoom.events.collect { event ->
                        when (event) {
                            is RoomEvent.TrackSubscribed -> {
                                val track = event.track
                                if (track is AudioTrack) {
                                    synchronized(audioTracks) {
                                        if (!audioTracks.contains(track)) {
                                            audioTracks.add(track)
                                        }
                                    }
                                    track.enabled = !isPaused
                                    broadcastStatus(STATUS_AUDIO, "audio subscribed")
                                    updateNotification(
                                        getString(R.string.notification_connected, currentRoomName),
                                    )
                                }
                            }

                            is RoomEvent.TrackUnsubscribed -> {
                                val track = event.track
                                if (track is AudioTrack) {
                                    synchronized(audioTracks) {
                                        audioTracks.remove(track)
                                    }
                                }
                            }

                            is RoomEvent.ActiveSpeakersChanged -> {
                                if (!isPaused) {
                                    val level = event.speakers.maxOfOrNull { it.audioLevel } ?: 0f
                                    broadcastLevel(level)
                                }
                            }

                            is RoomEvent.Disconnected -> {
                                isSessionConnected = false
                                broadcastStatus(STATUS_DISCONNECTED, "disconnected")
                                updateNotification(getString(R.string.notification_disconnected))
                            }

                            else -> Unit
                        }
                    }
                }

                startLevelPolling()
                broadcastStatus(STATUS_CONNECTED, "connected")
                updateNotification(getString(R.string.notification_connected, currentRoomName))
                updatePlaybackState()
            } catch (error: CancellationException) {
                Log.d(TAG, "connect cancelled", error)
            } catch (error: Exception) {
                if (error.isBenignCancellation()) {
                    Log.d(TAG, "connect cancelled (wrapped)", error)
                    return@launch
                }
                Log.e(TAG, "connect failed", error)
                broadcastStatus(STATUS_ERROR, formatConnectError(error))
                updateNotification(getString(R.string.notification_disconnected))
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelf()
            } finally {
                if (activeConnectKey == connectKey) {
                    activeConnectKey = null
                }
                connectJob = null
            }
        }
    }

    private fun startLevelPolling() {
        levelPollJob?.cancel()
        levelPollJob = serviceScope.launch {
            while (isActive && isSessionConnected) {
                delay(LEVEL_POLL_MS)
                if (isPaused) {
                    broadcastLevel(0f)
                    continue
                }
                val activeRoom = room ?: continue
                val level = activeRoom.remoteParticipants.values.maxOfOrNull { it.audioLevel } ?: 0f
                broadcastLevel(level)
            }
        }
    }

    private fun pausePlayback() {
        if (!isSessionConnected || isPaused) {
            return
        }
        isPaused = true
        setAudioTracksEnabled(false)
        broadcastLevel(0f)
        broadcastStatus(STATUS_PAUSED, getString(R.string.notification_paused))
        updateNotification(getString(R.string.notification_paused))
        updatePlaybackState()
    }

    private fun resumePlayback() {
        if (!isSessionConnected || !isPaused) {
            return
        }
        isPaused = false
        setAudioTracksEnabled(true)
        broadcastStatus(STATUS_PLAYING, "playing")
        updateNotification(getString(R.string.notification_connected, currentRoomName))
        updatePlaybackState()
    }

    private fun setAudioTracksEnabled(enabled: Boolean) {
        synchronized(audioTracks) {
            audioTracks.forEach { track ->
                track.enabled = enabled
            }
        }
    }

    private fun cleanupPreviousSession() {
        levelPollJob?.cancel()
        levelPollJob = null
        eventsJob?.cancel()
        eventsJob = null
        synchronized(audioTracks) {
            audioTracks.clear()
        }
        isSessionConnected = false
        isPaused = false

        val activeRoom = room
        room = null
        if (activeRoom != null) {
            serviceScope.launch {
                try {
                    activeRoom.disconnect()
                } catch (_: Exception) {
                }
            }
        }
    }

    private fun disconnect(stopService: Boolean) {
        activeConnectKey = null
        connectJob?.cancel()
        connectJob = null
        cleanupPreviousSession()
        broadcastLevel(0f)

        if (stopService) {
            broadcastStatus(STATUS_DISCONNECTED, "disconnected")
            updatePlaybackState()
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf()
        }
    }

    private fun releaseMediaSession() {
        if (::mediaSession.isInitialized) {
            mediaSession.isActive = false
            mediaSession.release()
        }
    }

    private fun updatePlaybackState() {
        if (!::mediaSession.isInitialized) {
            return
        }
        val state = when {
            !isSessionConnected -> PlaybackStateCompat.STATE_NONE
            isPaused -> PlaybackStateCompat.STATE_PAUSED
            else -> PlaybackStateCompat.STATE_PLAYING
        }
        mediaSession.setPlaybackState(
            PlaybackStateCompat.Builder()
                .setActions(
                    PlaybackStateCompat.ACTION_PLAY or
                        PlaybackStateCompat.ACTION_PAUSE,
                )
                .setState(state, PlaybackStateCompat.PLAYBACK_POSITION_UNKNOWN, 1f)
                .build(),
        )
    }

    private fun broadcastStatus(status: String, message: String) {
        val intent = Intent(ACTION_STATUS_CHANGED).apply {
            setPackage(packageName)
            putExtra(EXTRA_STATUS, status)
            putExtra(EXTRA_STATUS_MESSAGE, message)
        }
        sendBroadcast(intent)
    }

    private fun broadcastLevel(level: Float) {
        val now = SystemClock.elapsedRealtime()
        if (now - lastLevelBroadcastAt < LEVEL_BROADCAST_MS) {
            return
        }
        lastLevelBroadcastAt = now
        val intent = Intent(ACTION_LEVEL_CHANGED).apply {
            setPackage(packageName)
            putExtra(EXTRA_AUDIO_LEVEL, level.coerceIn(0f, 1f))
        }
        sendBroadcast(intent)
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) {
            return
        }
        val manager = getSystemService(NotificationManager::class.java)
        val channel = NotificationChannel(
            CHANNEL_ID,
            getString(R.string.notification_channel_name),
            NotificationManager.IMPORTANCE_LOW,
        ).apply {
            description = getString(R.string.notification_channel_description)
            setShowBadge(false)
        }
        manager.createNotificationChannel(channel)
    }

    private fun startForegroundWithType(notification: Notification) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                NOTIFICATION_ID,
                notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK,
            )
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
    }

    private fun buildNotification(contentText: String): Notification {
        val openIntent = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val builder = NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(getString(R.string.notification_title))
            .setContentText(contentText)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentIntent(openIntent)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)

        if (isSessionConnected) {
            if (isPaused) {
                builder.addAction(
                    R.drawable.ic_play,
                    getString(R.string.action_play),
                    playbackPendingIntent(ACTION_RESUME, REQUEST_RESUME),
                )
            } else {
                builder.addAction(
                    R.drawable.ic_pause,
                    getString(R.string.action_pause),
                    playbackPendingIntent(ACTION_PAUSE, REQUEST_PAUSE),
                )
            }
            builder.setStyle(
                MediaNotificationCompat.MediaStyle()
                    .setMediaSession(mediaSession.sessionToken)
                    .setShowActionsInCompactView(0),
            )
        }
        return builder.build()
    }

    private fun playbackPendingIntent(action: String, requestCode: Int): PendingIntent =
        PendingIntent.getService(
            this,
            requestCode,
            Intent(this, PlaybackService::class.java).apply { this.action = action },
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )

    private fun updateNotification(contentText: String) {
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        manager.notify(NOTIFICATION_ID, buildNotification(contentText))
    }

    private fun Throwable.isBenignCancellation(): Boolean {
        var current: Throwable? = this
        while (current != null) {
            if (current is CancellationException || current is JavaCancellationException) {
                return true
            }
            current = current.cause
        }
        val message = message.orEmpty()
        return message.contains("cancelled", ignoreCase = true) ||
            message.contains("canceled", ignoreCase = true)
    }

    private fun formatConnectError(error: Throwable): String {
        val message = generateSequence(error) { it.cause }
            .mapNotNull { it.message?.trim() }
            .firstOrNull { it.isNotEmpty() }
            .orEmpty()

        if (message.startsWith("HTTP 401")) {
            return "LiveKit rejected token (401). Check API keys on server."
        }
        if (message.startsWith("HTTP 403")) {
            return "Helper rejected request (403)."
        }
        if (message.startsWith("HTTP 5")) {
            return "Helper server error: $message"
        }
        if (message.contains("Unable to resolve host", ignoreCase = true) ||
            message.contains("Failed to connect", ignoreCase = true) ||
            message.contains("Connection refused", ignoreCase = true)
        ) {
            return "Cannot reach server. Check IP, ports 8000/7880 and firewall."
        }
        if (message.isNotEmpty()) {
            return message
        }
        return error.javaClass.simpleName.ifBlank { "connection failed" }
    }

    companion object {
        private const val TAG = "PlaybackService"
        private const val LEVEL_BROADCAST_MS = 50L
        private const val LEVEL_POLL_MS = 80L
        private const val REQUEST_PAUSE = 11
        private const val REQUEST_RESUME = 12

        const val ACTION_CONNECT = "com.audiostreaming.listener.action.CONNECT"
        const val ACTION_DISCONNECT = "com.audiostreaming.listener.action.DISCONNECT"
        const val ACTION_PAUSE = "com.audiostreaming.listener.action.PAUSE"
        const val ACTION_RESUME = "com.audiostreaming.listener.action.RESUME"
        const val ACTION_STATUS_CHANGED = "com.audiostreaming.listener.action.STATUS_CHANGED"
        const val ACTION_LEVEL_CHANGED = "com.audiostreaming.listener.action.LEVEL_CHANGED"

        const val EXTRA_HELPER_URL = "extra_helper_url"
        const val EXTRA_LIVEKIT_URL = "extra_livekit_url"
        const val EXTRA_ROOM = "extra_room"
        const val EXTRA_IDENTITY = "extra_identity"
        const val EXTRA_STATUS = "extra_status"
        const val EXTRA_STATUS_MESSAGE = "extra_status_message"
        const val EXTRA_AUDIO_LEVEL = "extra_audio_level"

        const val STATUS_CONNECTING = "connecting"
        const val STATUS_CONNECTED = "connected"
        const val STATUS_AUDIO = "audio"
        const val STATUS_PAUSED = "paused"
        const val STATUS_PLAYING = "playing"
        const val STATUS_DISCONNECTED = "disconnected"
        const val STATUS_ERROR = "error"

        private const val CHANNEL_ID = "audio_playback"
        private const val NOTIFICATION_ID = 1001

        fun connectIntent(
            context: Context,
            helperUrl: String,
            livekitUrl: String,
            room: String,
            identity: String,
        ): Intent = Intent(context, PlaybackService::class.java).apply {
            action = ACTION_CONNECT
            putExtra(EXTRA_HELPER_URL, helperUrl)
            putExtra(EXTRA_LIVEKIT_URL, livekitUrl)
            putExtra(EXTRA_ROOM, room)
            putExtra(EXTRA_IDENTITY, identity)
        }

        fun disconnectIntent(context: Context): Intent =
            Intent(context, PlaybackService::class.java).apply {
                action = ACTION_DISCONNECT
            }
    }
}
