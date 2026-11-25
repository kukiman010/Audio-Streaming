import unittest

from aiohttp.test_utils import make_mocked_request

import server


class ViewerPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_page_has_latency_handler(self):
        resp = await server.index(make_mocked_request("GET", "/"))
        page = resp.text
        for needle in ('id="latency"', "lat.ping", "lat.pong", "RoomEvent.DataReceived"):
            self.assertIn(needle, page)


if __name__ == "__main__":
    unittest.main()
