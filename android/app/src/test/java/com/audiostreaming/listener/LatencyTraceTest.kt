package com.audiostreaming.listener

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class LatencyTraceTest {
    @Test
    fun parsePingRejectsGarbage() {
        assertNull(parsePing(byteArrayOf(-1, -2)))
        assertNull(parsePing("{}".toByteArray()))
        assertNull(parsePing("""{"v":2,"id":7}""".toByteArray()))
        assertNull(parsePing("[1]".toByteArray()))
    }

    @Test
    fun parsePingAcceptsValidPacket() {
        val ping = parsePing("""{"v":1,"id":7,"t":1.0,"pub_rtt_ms":62.0}""".toByteArray())
        assertNotNull(ping)
        assertEquals(7L, ping!!.id)
        assertEquals(62.0, ping.pubRttMs!!, 0.0001)
    }

    @Test
    fun parsePingAllowsNullPubRtt() {
        val ping = parsePing("""{"v":1,"id":7,"t":1.0,"pub_rtt_ms":null}""".toByteArray())
        assertNotNull(ping)
        assertNull(ping!!.pubRttMs)
    }

    @Test
    fun buildPongRoundTrips() {
        val json = JSONObject(String(buildPong(7, 50.0, null)))
        assertEquals(1, json.getInt("v"))
        assertEquals(7L, json.getLong("id"))
        assertEquals(50.0, json.getDouble("sfu_rtt_ms"), 0.0001)
        assertTrue(json.isNull("jitter_buffer_ms"))
    }

    @Test
    fun estimateMatchesSpecFormula() {
        assertEquals(956.0, estimateMs(62.0, 50.0, 900.0), 0.0001)
    }

    @Test
    fun jitterBufferUsesRecentIntervalWhenNewSamples() {
        val avg = JitterBufferAverager()
        // первое чтение: за всё время = 0.5 с / 100 сэмплов = 5 мс
        assertEquals(5.0, avg.update(0.5, 100.0)!!, 0.0001)
        // следующее: за интервал (1.5-0.5) с / (200-100) = 10 мс
        assertEquals(10.0, avg.update(1.5, 200.0)!!, 0.0001)
        // новых сэмплов нет: берём накопленное среднее 1.5/200 = 7.5 мс
        assertEquals(7.5, avg.update(1.5, 200.0)!!, 0.0001)
    }

    @Test
    fun jitterBufferNullWithoutSamples() {
        assertNull(JitterBufferAverager().update(0.0, 0.0))
    }
}
