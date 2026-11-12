import base64
import json
import threading
import unittest
import urllib.parse
import urllib.request

from seamroutes.source_server import FixtureServer
from seamroutes.transport import fetch


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.server = FixtureServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()

    def get(self, path):
        return fetch(self.base + path)

    def phase(self, value):
        self.assertEqual(self.get('/control/phase?value=' + value)[0], 200)

    def test_neighbor_late(self):
        self.assertEqual(self.get('/tiles/1_0')[0], 503)
        self.phase('complete')
        self.assertEqual(self.get('/tiles/1_0')[0], 200)
        roads = json.loads(self.get('/tiles/0_0')[2])['features']
        self.assertNotEqual(roads[0]['properties']['partKey'], roads[1]['properties']['partKey'])

    def test_version_switch(self):
        self.phase('updated')
        self.assertEqual(json.loads(self.get('/manifest')[2])['version'], 'roads2')
        self.assertEqual(json.loads(self.get('/tiles/1_0')[2])['version'], 'roads2')
