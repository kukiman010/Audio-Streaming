package com.audiostreaming.listener

import java.net.URI

data class ConnectParams(
    val helperUrl: String,
    val livekitUrl: String,
    val room: String,
    val identity: String,
)

object ConnectionConfig {
    const val DEFAULT_IDENTITY = "android-viewer"
    const val DEFAULT_ROOM = "audio-room"
    const val DEFAULT_HELPER_PORT = 8000
    const val DEFAULT_LIVEKIT_PORT = 7880

    fun buildHelperUrl(host: String, helperPort: Int): String =
        "http://${normalizeHost(host)}:$helperPort"

    fun buildLivekitUrl(host: String, livekitPort: Int): String =
        "ws://${normalizeHost(host)}:$livekitPort"

    fun resolveForConnect(session: SessionPreferences.SessionConfig, discovery: DiscoveryResponse): ConnectParams {
        val host = normalizeHost(session.serverHost)
        val helperUrl = discovery.helperUrl.ifBlank { buildHelperUrl(host, session.helperPort) }
        val livekitUrl = discovery.livekitUrl.ifBlank {
            buildLivekitUrl(host, session.livekitPort)
        }
        val room = discovery.defaultRoom.ifBlank { DEFAULT_ROOM }
        return ConnectParams(
            helperUrl = helperUrl,
            livekitUrl = livekitUrl,
            room = room,
            identity = DEFAULT_IDENTITY,
        )
    }

    fun parseQrUrl(raw: String): QrParseResult? {
        val trimmed = raw.trim()
        if (trimmed.isBlank()) {
            return null
        }
        return try {
            val uri = URI(trimmed)
            val scheme = uri.scheme?.lowercase().orEmpty()
            if (scheme !in setOf("http", "https")) {
                return null
            }
            val host = uri.host?.trim().orEmpty()
            if (host.isBlank()) {
                return null
            }
            val port = if (uri.port > 0) uri.port else DEFAULT_HELPER_PORT
            QrParseResult(host = host, helperPort = port)
        } catch (_: Exception) {
            null
        }
    }

    fun extractHostFromUrl(url: String): String? =
        try {
            URI(url.trim()).host?.takeIf { it.isNotBlank() }
        } catch (_: Exception) {
            null
        }

    fun extractPortFromUrl(url: String, defaultPort: Int): Int =
        try {
            val uri = URI(url.trim())
            if (uri.port > 0) uri.port else defaultPort
        } catch (_: Exception) {
            defaultPort
        }

    private fun normalizeHost(host: String): String = host.trim().trimEnd('/')
}

data class QrParseResult(
    val host: String,
    val helperPort: Int,
)
