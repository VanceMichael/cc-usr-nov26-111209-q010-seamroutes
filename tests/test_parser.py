import unittest
from pathlib import Path
from seamroutes.parser import inspect_document

class ParserTests(unittest.TestCase):
    def test_two_parts_same_road(self):
        result = inspect_document(Path('seamroutes/fixtures/left.json').read_bytes())
        self.assertEqual([row['road'] for row in result['features']], ['R7', 'R7'])
        self.assertNotEqual(result['features'][0]['part'], result['features'][1]['part'])

    def test_reverse_direction_preserved(self):
        result = inspect_document(Path('seamroutes/fixtures/right.json').read_bytes())
        self.assertEqual(result['features'][1]['start'], [10, 2])
        self.assertEqual(result['features'][1]['end'], [0, 2])

    def test_float_rejected(self):
        with self.assertRaises(ValueError):
            inspect_document(b'{"type":"FeatureCollection","features":[{"geometry":{"type":"LineString","coordinates":[[0.5,1],[2,3]]}}]}')

    def test_wrong_geometry(self):
        with self.assertRaises(ValueError):
            inspect_document(b'{"type":"FeatureCollection","features":[{"geometry":{"type":"Point","coordinates":[0,1]}}]}')
