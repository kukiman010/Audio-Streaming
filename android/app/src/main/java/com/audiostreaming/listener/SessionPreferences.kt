package com.audiostreaming.listener

import android.content.Context

class SessionPreferences(context: Context) {
    private val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    fun load(): SessionConfig {
        migrateLegacyIfNeeded()
        return SessionConfig(
            serverHost = prefs.getString(KEY_SERVER_HOST, DEFAULT_HOST).orEmpty(),
            helperPort = prefs.getInt(KEY_HELPER_PORT, ConnectionConfig.DEFAULT_HELPER_PORT),
            livekitPort = prefs.getInt(KEY_LIVEKIT_PORT, ConnectionConfig.DEFAULT_LIVEKIT_PORT),
        )
    }

    fun save(config: SessionConfig) {
        prefs.edit()
            .putString(KEY_SERVER_HOST, config.serverHost.trim())
            .putInt(KEY_HELPER_PORT, config.helperPort)
            .putInt(KEY_LIVEKIT_PORT, config.livekitPort)
            .apply()
    }

    private fun migrateLegacyIfNeeded() {
        if (prefs.getBoolean(KEY_MIGRATED, false)) {
            return
        }
        val legacyHelper = prefs.getString(KEY_LEGACY_HELPER_URL, null)
        val legacyLivekit = prefs.getString(KEY_LEGACY_LIVEKIT_URL, null)
        if (!legacyHelper.isNullOrBlank() || !legacyLivekit.isNullOrBlank()) {
            val host = ConnectionConfig.extractHostFromUrl(legacyHelper.orEmpty())
                ?: ConnectionConfig.extractHostFromUrl(legacyLivekit.orEmpty())
                ?: DEFAULT_HOST
            val helperPort = legacyHelper?.let {
                ConnectionConfig.extractPortFromUrl(it, ConnectionConfig.DEFAULT_HELPER_PORT)
            } ?: ConnectionConfig.DEFAULT_HELPER_PORT
            val livekitPort = legacyLivekit?.let {
                ConnectionConfig.extractPortFromUrl(it, ConnectionConfig.DEFAULT_LIVEKIT_PORT)
            } ?: ConnectionConfig.DEFAULT_LIVEKIT_PORT
            prefs.edit()
                .putString(KEY_SERVER_HOST, host)
                .putInt(KEY_HELPER_PORT, helperPort)
                .putInt(KEY_LIVEKIT_PORT, livekitPort)
                .remove(KEY_LEGACY_HELPER_URL)
                .remove(KEY_LEGACY_LIVEKIT_URL)
                .remove(KEY_LEGACY_ROOM)
                .remove(KEY_LEGACY_IDENTITY)
                .putBoolean(KEY_MIGRATED, true)
                .apply()
            return
        }
        prefs.edit().putBoolean(KEY_MIGRATED, true).apply()
    }

    data class SessionConfig(
        val serverHost: String,
        val helperPort: Int,
        val livekitPort: Int,
    ) {
        fun helperUrl(): String = ConnectionConfig.buildHelperUrl(serverHost, helperPort)
    }

    companion object {
        private const val PREFS_NAME = "audio_stream_listener"
        private const val KEY_SERVER_HOST = "server_host"
        private const val KEY_HELPER_PORT = "helper_port"
        private const val KEY_LIVEKIT_PORT = "livekit_port"
        private const val KEY_MIGRATED = "prefs_migrated_v2"

        private const val KEY_LEGACY_HELPER_URL = "helper_url"
        private const val KEY_LEGACY_LIVEKIT_URL = "livekit_url"
        private const val KEY_LEGACY_ROOM = "room"
        private const val KEY_LEGACY_IDENTITY = "identity"

        private const val DEFAULT_HOST = "192.168.1.100"
    }
}
