import json
import tempfile
import threading
import unittest
from pathlib import Path

from seamroutes.archive import Archive
from seamroutes.geometry import duplicate_relationship
from seamroutes.parser import parse_manifest, parse_tile
from seamroutes.region import RegionReceiver, generation_diagnostics, road_view
from seamroutes.seams import assemble
from seamroutes.source_server import FixtureServer
from seamroutes.transport import fetch


def feature(road, part, points, direction='both', start_cut=False, end_cut=False,
            name='道路'):
    return {
        'type': 'Feature',
        'properties': {
            'sourceRoadId': road, 'partKey': part, 'direction': direction,
            'name': name, 'cutStart': start_cut, 'cutEnd': end_cut,
        },
        'geometry': {'type': 'LineString', 'coordinates': points},
    }


def collection(tile, version, features):
    return json.dumps({'type': 'FeatureCollection', 'version': version, 'tile': tile,
                       'features': features}).encode()


def parsed(tile, version, features, grid_size=10):
    return parse_tile(collection(tile, version, features), version, tile, grid_size)


def payload(tile, version, features, grid_size=10):
    return {'tile': tile, 'capture_id': tile, 'digest': tile + '-digest',
            'features': parsed(tile, version, features, grid_size)['features']}


def assemble_tiles(specs, states=None, version='v1', grid_size=10):
    tiles = [payload(tile, version, features, grid_size) for tile, features in specs]
    if states is None:
        states = {tile: 'received' for tile, _ in specs}
    return assemble(tiles, version, grid_size, states)


class GeometryTests(unittest.TestCase):
    def test_reverse_sequence_is_same_atomic_edges(self):
        self.assertEqual(
            duplicate_relationship([[10, 2], [5, 2], [0, 2]],
                                   [[0, 2], [5, 2], [10, 2]], True),
            'reverse')
        self.assertIsNone(
            duplicate_relationship([[10, 2], [5, 2], [0, 2]],
                                   [[0, 2], [5, 2], [10, 2]], False))

    def test_different_intermediate_vertex_is_not_duplicate(self):
        self.assertIsNone(
            duplicate_relationship([[0, 2], [3, 9], [10, 2]],
                                   [[0, 2], [5, 2], [10, 2]], True))

    def test_split_long_edge_is_not_same_atomic_sequence(self):
        self.assertIsNone(
            duplicate_relationship([[0, 2], [10, 2]],
                                   [[0, 2], [5, 2], [10, 2]], True))


class ParserStrictnessTests(unittest.TestCase):
    def test_manifest_requires_integer_grid(self):
        with self.assertRaises(ValueError):
            parse_manifest(b'{"version":"v","grid_size":1.5,"tiles":[]}')
        with self.assertRaises(ValueError):
            parse_manifest(b'{"version":"v","grid_size":10,"tiles":'
                           b'[{"id":"a","href":"/a"},{"id":"a","href":"/a"}]}')

    def test_tile_rejects_wrong_version_and_float(self):
        body = collection('0_0', 'v2', [feature('R1', 'main', [[0, 0], [10, 0]])])
        with self.assertRaises(ValueError):
            parse_tile(body, 'v1', '0_0', 10)
        body = collection('0_0', 'v1', [feature('R1', 'main', [[0, 0.5], [10, 0]])])
        with self.assertRaises(ValueError):
            parse_tile(body, 'v1', '0_0', 10)

    def test_tile_rejects_longitude_like_projection(self):
        body = collection('0_0', 'v1', [feature('R1', 'main',
                                                [[120.5, 30.25], [121, 31]])])
        with self.assertRaises(ValueError):
            parse_tile(body, 'v1', '0_0', 10)

    def test_missing_part_key_cannot_be_joined(self):
        body = collection('0_0', 'v1', [feature('R1', None, [[0, 0], [10, 0]])])
        with self.assertRaises(ValueError):
            parse_tile(body, 'v1', '0_0', 10)


class SeamTests(unittest.TestCase):
    def test_same_road_different_parts_are_separate_segments(self):
        result = assemble_tiles([('0_0', [
            feature('R7', 'main', [[0, 2], [10, 2]], end_cut=True),
            feature('R7', 'branch', [[1, 6], [4, 7]]),
        ]), ('1_0', [
            feature('R7', 'main', [[10, 2], [20, 2]], start_cut=True),
        ])])
        parts = {(segment['road'], segment['partKey']): segment
                 for segment in result['segments']}
        self.assertEqual(parts[('R7', 'main')]['points'],
                         [[0, 2], [10, 2], [20, 2]])
        self.assertEqual(parts[('R7', 'branch')]['points'], [[1, 6], [4, 7]])
        self.assertTrue(parts[('R7', 'main')]['closed'])
        self.assertTrue(parts[('R7', 'branch')]['closed'])

    def test_reverse_buffer_duplicate_creates_no_second_road(self):
        result = assemble_tiles([('0_0', [
            feature('R7', 'main', [[0, 2], [5, 2], [10, 2]], end_cut=True),
        ]), ('1_0', [
            feature('R7', 'main', [[10, 2], [15, 2], [20, 2]], start_cut=True),
            feature('R7', 'main', [[10, 2], [5, 2], [0, 2]], start_cut=True),
        ])])
        self.assertEqual(len(result['segments']), 1)
        segment = result['segments'][0]
        self.assertEqual(segment['points'], [[0, 2], [5, 2], [10, 2], [15, 2], [20, 2]])
        self.assertEqual(len(segment['duplicates']), 1)
        self.assertEqual(segment['duplicates'][0]['relationship'], 'reverse')
        self.assertEqual(segment['duplicates'][0]['duplicate_of'],
                         {'tile': '0_0', 'feature_index': 0})

    def test_parallel_branch_with_close_endpoints_is_not_merged(self):
        result = assemble_tiles([('0_0', [
            feature('R7', 'main', [[0, 2], [5, 2], [10, 2]], end_cut=True),
            feature('R7', 'parallel', [[0, 3], [5, 3], [10, 3]], end_cut=True),
        ]), ('1_0', [
            feature('R7', 'main', [[10, 2], [20, 2]], start_cut=True),
            feature('R7', 'parallel', [[10, 3], [20, 3]], start_cut=True),
        ])])
        parts = {segment['partKey']: segment for segment in result['segments']}
        self.assertEqual(set(parts), {'main', 'parallel'})
        self.assertEqual(parts['main']['points'], [[0, 2], [5, 2], [10, 2], [20, 2]])
        self.assertEqual(parts['parallel']['points'], [[0, 3], [5, 3], [10, 3], [20, 3]])
        self.assertEqual(result['duplicates'], [])

    def test_true_endpoint_never_joined_by_equal_coordinates(self):
        result = assemble_tiles([('0_0', [
            feature('R7', 'left', [[0, 2], [10, 2]], end_cut=False),
            feature('R7', 'right', [[10, 2], [20, 2]], start_cut=False, name='道路'),
        ])])
        self.assertEqual(len(result['segments']), 2)
        for segment in result['segments']:
            self.assertTrue(segment['closed'])
            self.assertEqual({item['kind'] for item in segment['seam']},
                             {'true_endpoint'})
        self.assertTrue(result['publishable'])

    def test_direction_and_attribute_conflicts_block_closure(self):
        result = assemble_tiles([('0_0', [
            feature('R7', 'main', [[0, 2], [10, 2]], direction='forward',
                    end_cut=True),
        ]), ('1_0', [
            feature('R7', 'main', [[10, 2], [20, 2]], direction='both',
                    start_cut=True, name='另一名称'),
        ])])
        reasons = {reason['reason'] for reason in result['unclosed_reasons']}
        self.assertIn('attribute_conflict', reasons)
        self.assertIn('unpaired_cut', reasons)
        self.assertFalse(result['publishable'])

    def test_forward_reverse_cut_is_not_paired(self):
        result = assemble_tiles([('0_0', [
            feature('R7', 'main', [[0, 2], [10, 2]], direction='forward',
                    end_cut=True),
        ]), ('1_0', [
            feature('R7', 'main', [[20, 2], [10, 2]], direction='forward',
                    start_cut=False, end_cut=True),
        ])])
        self.assertFalse(result['publishable'])

    def test_junction_at_seam_is_reported(self):
        result = assemble_tiles([('0_0', [
            feature('R7', 'main', [[0, 2], [10, 2]], end_cut=True),
            feature('R7', 'main', [[0, 4], [10, 2]], end_cut=True),
        ]), ('1_0', [
            feature('R7', 'main', [[10, 2], [20, 2]], start_cut=True),
        ])])
        reasons = {reason['reason'] for reason in result['unclosed_reasons']}
        self.assertIn('junction', reasons)
        self.assertFalse(result['publishable'])

    def test_missing_neighbor_is_distinct_from_unpaired_cut(self):
        missing = assemble_tiles([('0_0', [
            feature('R7', 'main', [[0, 2], [10, 2]], end_cut=True),
        ])], states={'0_0': 'received', '1_0': 'pending'})
        self.assertEqual([reason['reason'] for reason in missing['unclosed_reasons']],
                         ['missing_neighbor'])
        self.assertFalse(missing['publishable'])

        unpaired = assemble_tiles([('0_0', [
            feature('R7', 'main', [[0, 2], [10, 2]], end_cut=True),
        ]), ('1_0', [])], states={'0_0': 'received', '1_0': 'received'})
        self.assertEqual([reason['reason'] for reason in unpaired['unclosed_reasons']],
                         ['unpaired_cut'])

    def test_cut_off_boundary_cannot_pair(self):
        result = assemble_tiles([('0_0', [
            feature('R7', 'main', [[0, 2], [7, 2]], end_cut=True),
        ]), ('1_0', [
            feature('R7', 'main', [[7, 2], [20, 2]], start_cut=True),
        ])])
        reasons = {reason['reason'] for reason in result['unclosed_reasons']}
        self.assertIn('cut_off_boundary', reasons)

    def test_region_boundary_cut_is_not_a_missing_neighbor(self):
        result = assemble_tiles([('0_0', [
            feature('R7', 'main', [[0, 2], [10, 2]], end_cut=True),
        ])], states={'0_0': 'received'})
        self.assertTrue(result['publishable'])
        segment = result['segments'][0]
        self.assertTrue(segment['closed'])
        self.assertEqual({item['kind'] for item in segment['seam']},
                         {'region_boundary_cut', 'true_endpoint'})

    def test_evidence_keeps_tile_and_feature_index(self):
        result = assemble_tiles([('0_0', [
            feature('R7', 'main', [[0, 2], [5, 2], [10, 2]], end_cut=True),
        ]), ('1_0', [
            feature('R7', 'main', [[10, 2], [15, 2], [20, 2]], start_cut=True),
        ])])
        sources = result['segments'][0]['sources']
        self.assertEqual([(item['tile'], item['feature_index']) for item in sources],
                         [('0_0', 0), ('1_0', 0)])
        self.assertEqual([item['point_span'] for item in sources], [[0, 2], [2, 4]])
        self.assertEqual(sources[1]['digest'], '1_0-digest')


class RegionFlowTests(unittest.TestCase):
    def setUp(self):
        self.server = FixtureServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_address[1])
        self.temporary = tempfile.TemporaryDirectory()
        self.archive = Archive(Path(self.temporary.name) / 'receive.sqlite3',
                               Path(self.temporary.name) / 'objects')
        self.receiver = RegionReceiver(self.archive, self.base)

    def tearDown(self):
        self.archive.close()
        self.temporary.cleanup()
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()

    def phase(self, value):
        self.assertEqual(fetch(self.base + '/control/phase?value=' + value)[0], 200)

    def test_late_neighbor_moves_from_unclosed_to_published(self):
        first = self.receiver.receive()
        self.assertEqual(first['status'], 'incomplete')
        self.assertFalse(first['published'])
        self.assertEqual(first['missing_tiles'], ['1_0'])
        self.assertEqual({reason['reason'] for reason in first['unclosed_reasons']},
                         {'missing_neighbor'})
        self.assertIsNone(self.archive.publication())
        diagnostics = generation_diagnostics(self.archive, first['generation_id'])
        self.assertEqual(diagnostics['generation']['status'], 'receiving')
        self.assertEqual([row['state'] for row in diagnostics['tiles']],
                         ['received', 'unavailable'])
        self.assertEqual([row['http_status'] for row in diagnostics['attempts']],
                         [200, 503])

        self.phase('complete')
        second = self.receiver.receive()
        self.assertEqual(second['status'], 'published')
        self.assertTrue(second['published'])
        self.assertEqual(second['generation_id'], first['generation_id'])
        self.assertEqual(second['publication_version'], 'roads1')
        parts = {segment['partKey']: segment for segment in second['segments']}
        self.assertEqual(parts['main']['points'],
                         [[0, 2], [5, 2], [10, 2], [15, 2], [20, 2]])
        self.assertEqual(parts['main']['duplicates'][0]['relationship'], 'reverse')
        self.assertEqual(parts['branch']['points'], [[1, 6], [4, 7]])

        publication = self.archive.publication('roads1')
        self.assertEqual(publication['generation_id'], first['generation_id'])
        view = road_view(self.archive, 'R7', 'roads1')
        self.assertEqual(len(view['publications']), 1)
        self.assertEqual({segment['partKey']
                          for segment in view['publications'][0]['segments']},
                         {'main', 'branch'})

    def test_duplicate_download_is_recorded_without_advancing_state(self):
        self.receiver.receive()
        self.phase('complete')
        self.receiver.receive()
        captured = len(self.archive.rows())
        again = self.receiver.receive(refetch=True)
        self.assertEqual(again['status'], 'published')
        self.assertEqual({attempt['state'] for attempt in again['attempts']},
                         {'redundant'})
        self.assertEqual(len(self.archive.rows()), captured + 3)
        attempts = generation_diagnostics(self.archive, again['generation_id'])['attempts']
        self.assertEqual([attempt['state'] for attempt in attempts],
                         ['received', 'unavailable', 'received', 'redundant', 'redundant'])

    def test_version_switch_abandons_old_generation_and_keeps_previous_publication(self):
        self.receiver.receive()
        self.phase('complete')
        self.receiver.receive()
        old = self.archive.publication('roads1')
        self.assertIsNotNone(old)

        self.phase('updated')
        updated = self.receiver.receive()
        self.assertEqual(updated['version'], 'roads2')
        self.assertNotEqual(updated['generation_id'], old['generation_id'])
        self.assertEqual(updated['status'], 'published')
        self.assertEqual(updated['abandoned_generations'], [])
        self.assertEqual(self.archive.generation(old['generation_id'])['status'],
                         'published')
        self.assertEqual(self.archive.publication('roads1')['generation_id'],
                         old['generation_id'])
        self.assertNotEqual(self.archive.publication('roads2')['generation_id'],
                            old['generation_id'])

    def test_version_switch_mid_capture_abandons_partial_generation(self):
        first = self.receiver.receive()
        self.assertEqual(first['status'], 'incomplete')
        self.phase('updated')
        second = self.receiver.receive()
        self.assertEqual(second['abandoned_generations'], [first['generation_id']])
        generation = self.archive.generation(first['generation_id'])
        self.assertEqual(generation['status'], 'abandoned')
        self.assertIn('清单换版', generation['note'])
        self.assertEqual(second['status'], 'published')
        self.assertEqual(second['version'], 'roads2')
        self.assertIsNone(self.archive.publication('roads1'))

    def test_previous_publication_survives_failed_recapture(self):
        self.phase('complete')
        first = self.receiver.receive()
        self.assertEqual(first['status'], 'published')
        before = self.archive.publication('roads1')

        self.phase('updated')
        receiver = RegionReceiver(self.archive, self.base,
                                  fetch=lambda url: (_ for _ in ()).throw(
                                      OSError('network down')))
        failed = receiver.receive()
        self.assertEqual(failed['status'], 'manifest_unavailable')
        self.assertEqual(failed['previous_publication']['generation_id'],
                         before['generation_id'])
        self.assertEqual(self.archive.publication('roads1')['generation_id'],
                         before['generation_id'])
        self.assertIsNone(self.archive.publication('roads2'))

    def test_failed_tile_refetch_keeps_verified_state_and_publication(self):
        self.phase('complete')
        first = self.receiver.receive()
        self.assertEqual(first['status'], 'published')
        before = self.archive.publication('roads1')
        real_fetch = fetch

        def partial(url):
            if '/tiles/' in url:
                raise OSError('tile down')
            return real_fetch(url)
        receiver = RegionReceiver(self.archive, self.base, fetch=partial)
        result = receiver.receive(refetch=True)
        self.assertEqual(result['status'], 'published')
        self.assertEqual({attempt['state'] for attempt in result['attempts']},
                         {'unavailable'})
        self.assertEqual({attempt['reused_verified_digest']
                          for attempt in result['attempts']}, {True})
        tiles = {row['tile_id']: row['state']
                 for row in self.archive.tile_rows(first['generation_id'])}
        self.assertEqual(tiles, {'0_0': 'received', '1_0': 'received'})
        self.assertEqual(self.archive.publication('roads1')['generation_id'],
                         before['generation_id'])

    def test_stale_tile_version_stops_generation(self):
        original = self.server.routes
        routes = dict(original)
        routes['/manifest'] = {'file': 'new-manifest.json'}
        routes['/tiles/0_0'] = {'file': 'left.json'}
        self.server.routes = routes
        try:
            result = self.receiver.receive()
        finally:
            self.server.routes = original
        self.assertEqual(result['status'], 'stale')
        self.assertFalse(result['published'])
        self.assertIsNone(self.archive.publication())
        self.assertEqual(self.archive.generation(result['generation_id'])['status'],
                         'stale')

    def test_publish_failure_leaves_generation_complete_but_unpublished(self):
        self.phase('complete')
        original = self.archive.publish

        def broken(*arguments, **keywords):
            raise sqlite_error('disk full')
        self.archive.publish = broken
        try:
            result = self.receiver.receive()
        finally:
            self.archive.publish = original
        self.assertEqual(result['status'], 'complete_unpublished')
        self.assertFalse(result['published'])
        self.assertIsNone(self.archive.publication())
        self.assertEqual(self.archive.generation(result['generation_id'])['status'],
                         'complete')
        self.assertIsNotNone(self.archive.snapshot(result['generation_id']))


def sqlite_error(message):
    import sqlite3
    return sqlite3.OperationalError(message)


if __name__ == '__main__':
    unittest.main()
