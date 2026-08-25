package com.audiostreaming.listener

import org.json.JSONObject
import java.io.BufferedReader
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import java.nio.charset.StandardCharsets

data class DiscoveryResponse(
    val livekitUrl: String,
    val helperUrl: String,
    val defaultRoom: String,
)

data class TokenResponse(
    val token: String,
    val room: String,
    val identity: String,
    val role: String,
)

object HelperApi {
    fun fetchDiscovery(helperUrl: String): DiscoveryResponse {
        val json = getJson(normalizeBaseUrl(helperUrl) + "/client/discovery")
        return DiscoveryResponse(
            livekitUrl = json.getString("livekit_url"),
            helperUrl = json.getString("helper_url"),
            defaultRoom = json.getString("default_room"),
        )
    }

    fun fetchViewerToken(helperUrl: String, room: String, identity: String): TokenResponse {
        val encodedRoom = urlEncode(room)
        val encodedIdentity = urlEncode(identity)
        val endpoint =
            "${normalizeBaseUrl(helperUrl)}/livekit/token" +
                "?room=$encodedRoom&identity=$encodedIdentity&role=viewer"
        val json = getJson(endpoint)
        if (!json.has("token")) {
            val error = json.optString("error", "response without token")
            throw IOException(error)
        }
        return TokenResponse(
            token = json.getString("token"),
            room = json.optString("room", room),
            identity = json.optString("identity", identity),
            role = json.optString("role", "viewer"),
        )
    }

    private fun getJson(endpoint: String): JSONObject {
        val connection = (URL(endpoint).openConnection() as HttpURLConnection).apply {
            requestMethod = "GET"
            connectTimeout = 10_000
            readTimeout = 10_000
            setRequestProperty("Accept", "application/json")
            setRequestProperty("Cache-Control", "no-store")
        }

        return try {
            val status = connection.responseCode
            val body = readBody(connection, status in 200..299)
            if (status !in 200..299) {
                throw IOException("HTTP $status: $body")
            }
            JSONObject(body)
        } finally {
            connection.disconnect()
        }
    }

    private fun readBody(connection: HttpURLConnection, success: Boolean): String {
        val stream = if (success) connection.inputStream else connection.errorStream
        return stream?.bufferedReader(StandardCharsets.UTF_8)?.use(BufferedReader::readText).orEmpty()
    }

    private fun normalizeBaseUrl(url: String): String = url.trim().trimEnd('/')

    private fun urlEncode(value: String): String =
        URLEncoder.encode(value, StandardCharsets.UTF_8.name())
}
