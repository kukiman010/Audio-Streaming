package com.audiostreaming.listener

import org.json.JSONObject

/** Протокол трассировки задержки: см. docs/superpowers/specs/2026-11-25-latency-trace-design.md. */
object LatencyTrace {
    const val TOPIC_PING = "lat.ping"
    const val TOPIC_PONG = "lat.pong"
    /** Нет ping дольше этого времени — трассировка считается остановленной. */
    const val QUIET_TIMEOUT_MS = 3000L
    /** Сколько ждём статистику WebRTC, прежде чем ответить с null-полями. */
    const val STATS_TIMEOUT_MS = 800L
}

data class Ping(val id: Long, val pubRttMs: Double?)

/** Любой мусор, чужая версия или пакет без id дают null. */
fun parsePing(data: ByteArray): Ping? = try {
    val o = JSONObject(String(data, Charsets.UTF_8))
    if (o.optInt("v", -1) != 1 || !o.has("id")) {
        null
    } else {
        val rtt = if (o.isNull("pub_rtt_ms")) null else o.optDouble("pub_rtt_ms").takeIf { !it.isNaN() }
        Ping(o.getLong("id"), rtt)
    }
} catch (e: Exception) {
    null
}

fun buildPong(id: Long, sfuRttMs: Double?, jitterBufferMs: Double?): ByteArray =
    JSONObject()
        .put("v", 1)
        .put("id", id)
        .put("sfu_rtt_ms", sfuRttMs?.takeIf { it.isFinite() } ?: JSONObject.NULL)
        .put("jitter_buffer_ms", jitterBufferMs?.takeIf { it.isFinite() } ?: JSONObject.NULL)
        .toString()
        .toByteArray(Charsets.UTF_8)

/** Оценка задержки: (до сервера + сервер↔клиент)/2 + буфер клиента (формула из спеки). */
fun estimateMs(pubRttMs: Double, sfuRttMs: Double, bufferMs: Double): Double =
    (pubRttMs + sfuRttMs) / 2 + bufferMs

/**
 * Средняя задержка jitter buffer в мс. WebRTC отдаёт накопленные суммы, поэтому берём среднее
 * за интервал между замерами; если новых сэмплов нет, среднее за всё время.
 */
class JitterBufferAverager {
    private var prevDelaySec = 0.0
    private var prevCount = 0.0
    private var hasPrev = false

    fun update(delaySec: Double, emittedCount: Double): Double? {
        if (emittedCount <= 0.0) return null
        val dCount = if (hasPrev) emittedCount - prevCount else 0.0
        val dDelay = if (hasPrev) delaySec - prevDelaySec else 0.0
        prevDelaySec = delaySec
        prevCount = emittedCount
        hasPrev = true
        val seconds = if (dCount > 0.0) dDelay / dCount else delaySec / emittedCount
        return seconds * 1000.0
    }
}
