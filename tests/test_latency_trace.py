import json
import unittest

import latency_trace as lt


class StagesTests(unittest.TestCase):
    def test_compute_stages_example(self):
        s = lt.compute_stages(62, 147, 940)
        self.assertEqual(s.sfu_client_ms, 85)
        self.assertEqual(s.total_ms, (62 + 85) / 2 + 940)

    def test_loop_smaller_than_pub_rtt_clamps_to_zero(self):
        self.assertEqual(lt.compute_stages(100, 80, 10).sfu_client_ms, 0)

    def test_missing_pub_rtt(self):
        s = lt.compute_stages(None, 147, 940)
        self.assertIsNone(s.sfu_client_ms)
        self.assertIsNone(s.total_ms)
        self.assertIn("до сервера н/д", lt.format_line("a", s))

    def test_estimate_ms(self):
        self.assertEqual(lt.estimate_ms(62.0, 50.0, 900.0), 956.0)


class ProtocolTests(unittest.TestCase):
    def test_decode_pong_garbage(self):
        for raw in (b"\xff", b"{}", b'{"v":2,"id":1}', b'{"v":1,"id":"x"}', b"[1]"):
            self.assertIsNone(lt.decode_pong(raw), raw)

    def test_decode_pong_ok(self):
        p = lt.decode_pong(b'{"v":1,"id":7,"sfu_rtt_ms":50,"jitter_buffer_ms":900}')
        self.assertEqual(p, lt.Pong(7, 50.0, 900.0))

    def test_encode_ping_roundtrip(self):
        d = json.loads(lt.encode_ping(3, 12.5, 62.0))
        self.assertEqual(d, {"v": 1, "id": 3, "t": 12.5, "pub_rtt_ms": 62.0})


class FormatTests(unittest.TestCase):
    def test_format_line_example(self):
        line = lt.format_line("android-viewer", lt.compute_stages(62, 147, 940))
        self.assertEqual(
            line,
            "[замер] android-viewer: до сервера 62 мс | сервер↔клиент 85 мс | "
            "буфер клиента 940 мс | ≈ 1,0 с",
        )

    def test_format_line_no_answer(self):
        self.assertIn("нет ответа (обновите клиент)", lt.format_line("a", None))

    def test_trace_stats_summary(self):
        ts = lt.TraceStats()
        ts.add("a", lt.compute_stages(60, 140, 900))
        ts.add("a", lt.compute_stages(80, 180, 1100))
        text = "\n".join(ts.summary_lines())
        self.assertIn("a", text)
        for word in ("min", "avg", "max"):
            self.assertIn(word, text)

    def test_compose_log_keeps_both_and_caps(self):
        lines = [f"l{i}" for i in range(300)]
        out = lt.compose_log("boom", lines).split("\n")
        self.assertEqual(out[0], "boom")
        self.assertEqual(out[1:], lines[-lt.MAX_LOG_LINES:])

    def test_compose_log_no_error(self):
        self.assertEqual(lt.compose_log("", ["x"]), "x")
        self.assertEqual(lt.compose_log("", []), "")


class StatsExtractTests(unittest.TestCase):
    @staticmethod
    def _stats(**kw):
        from livekit.rtc._proto import stats_pb2 as sp

        return sp.RtcStats(**kw)

    def test_extract_pub_rtt_from_real_proto(self):
        st = self._stats(remote_inbound_rtp={"remote_inbound": {"round_trip_time": 0.062}})
        self.assertAlmostEqual(lt.extract_pub_rtt_ms([st]), 62.0)
        self.assertIsNone(lt.extract_pub_rtt_ms([]))

    def test_extract_pub_rtt_falls_back_to_candidate_pair(self):
        st = self._stats(candidate_pair={"candidate_pair": {"current_round_trip_time": 0.07}})
        self.assertAlmostEqual(lt.extract_pub_rtt_ms([st]), 70.0)

    def test_extract_conn_type(self):
        def conn(ctype, proto):
            return [
                self._stats(candidate_pair={"rtc": {"id": "p"}, "candidate_pair": {
                    "nominated": True, "local_candidate_id": "l"}}),
                self._stats(local_candidate={"rtc": {"id": "l"}, "candidate": {
                    "candidate_type": ctype, "protocol": proto}}),
            ]

        self.assertEqual(lt.extract_conn_type(conn("RELAY", "udp")), "relay (TURN)")
        self.assertEqual(lt.extract_conn_type(conn("HOST", "udp")), "UDP")
        self.assertEqual(lt.extract_conn_type(conn("SRFLX", "tcp")), "TCP")
        self.assertEqual(lt.extract_conn_type([]), "н/д")


class PongHandlerTests(unittest.TestCase):
    def _client(self, tracing=True):
        from types import SimpleNamespace
        from livekit_client import LiveKitStreamClient

        lines = []
        c = LiveKitStreamClient(lambda s: None)
        c.trace_callback = lines.append
        c.room = SimpleNamespace(remote_participants={"android-viewer": object()})
        c._tracing = tracing
        c._trace_pending = {5: (0.0, 60.0)}
        return c, lines

    @staticmethod
    def _packet(data, topic=lt.TOPIC_PONG, identity="android-viewer"):
        from types import SimpleNamespace

        who = SimpleNamespace(identity=identity) if identity else None
        return SimpleNamespace(data=data, topic=topic, participant=who)

    PONG5 = b'{"v":1,"id":5,"sfu_rtt_ms":50,"jitter_buffer_ms":900}'

    def test_valid_pong_emits_line(self):
        c, lines = self._client()
        c._on_data_received(self._packet(self.PONG5))
        self.assertEqual(len(lines), 1)
        self.assertIn("android-viewer", lines[0])
        self.assertIn("буфер клиента 900 мс", lines[0])

    def test_ignored_cases(self):
        for name, client_kw, pkt in [
            ("stopped", {"tracing": False}, self._packet(self.PONG5)),
            ("old id", {}, self._packet(b'{"v":1,"id":4}')),
            ("garbage", {}, self._packet(b"\xff\xfe")),
            ("other topic", {}, self._packet(self.PONG5, topic="chat")),
            ("no sender", {}, self._packet(self.PONG5, identity=None)),
            ("not in room", {}, self._packet(self.PONG5, identity="ghost")),
        ]:
            c, lines = self._client(**client_kw)
            c._on_data_received(pkt)
            self.assertEqual(lines, [], name)


class TraceLoopTests(unittest.IsolatedAsyncioTestCase):
    def _client(self):
        from types import SimpleNamespace
        from livekit_client import LiveKitStreamClient

        sent = []

        async def get_rtc_stats():
            return SimpleNamespace(publisher_stats=[], subscriber_stats=[])

        async def publish_data(payload, **kw):
            sent.append((payload, kw))

        c = LiveKitStreamClient(lambda s: None)
        lines = []
        c.trace_callback = lines.append
        c.state.phase = "connected"
        c.room = SimpleNamespace(
            remote_participants={},
            get_rtc_stats=get_rtc_stats,
            local_participant=SimpleNamespace(publish_data=publish_data),
        )
        return c, lines, sent

    async def test_empty_room_pings_and_reports_no_listeners(self):
        import asyncio
        import json

        c, lines, sent = self._client()
        await c.start_trace()
        await asyncio.sleep(0.2)
        await c.stop_trace()
        self.assertFalse(c.tracing)
        self.assertTrue(any("слушателей нет" in s for s in lines), lines)
        self.assertTrue(any("остановлено" in s for s in lines), lines)
        payload, kw = sent[0]
        self.assertEqual(kw, {"reliable": False, "topic": lt.TOPIC_PING})
        self.assertEqual(json.loads(payload)["v"], 1)

    async def test_phase_change_stops_trace(self):
        import asyncio

        c, lines, _ = self._client()
        await c.start_trace()
        c._set_phase("reconnecting")
        await asyncio.sleep(0.1)
        self.assertFalse(c.tracing)

    async def test_stop_during_start_does_not_leave_loop_running(self):
        import asyncio

        c, lines, sent = self._client()
        orig = c.room.get_rtc_stats

        async def slow_stats():
            await asyncio.sleep(0.1)
            return await orig()

        c.room.get_rtc_stats = slow_stats
        starting = asyncio.create_task(c.start_trace())
        await asyncio.sleep(0.02)
        await c.stop_trace()
        await starting
        await asyncio.sleep(0.3)
        self.assertFalse(c.tracing)
        self.assertIsNone(c._trace_task)
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
