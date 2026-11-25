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


if __name__ == "__main__":
    unittest.main()
