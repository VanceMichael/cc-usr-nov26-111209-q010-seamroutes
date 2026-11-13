"""区域接收代次：清单、分片、接缝与发布的同代边界。

一次接收代次绑定一份清单（version + tile 声明）和其中每个分片的原文摘要。
HTTP 成功不证明同代；只有声明片全部核验并完成接缝检查后才写入发布记录。
网络失败、重复下载和持久化中断都不会提前改变发布状态。
"""

import urllib.error
import urllib.parse

from .parser import VersionMismatch, parse_manifest, parse_tile
from .seams import assemble
from .transport import fetch as default_fetch


VERIFIED_STATES = {'received', 'redundant'}


class RegionReceiver:
    def __init__(self, archive, base_url, fetch=None):
        self.archive = archive
        self.base_url = base_url
        self.fetch = fetch or default_fetch

    def receive(self, refetch=False):
        """取一次清单并按需补齐分片，返回本次代次的诊断摘要。"""
        manifest_url = self._manifest_url()
        summary = {
            'manifest_url': manifest_url,
            'generation_id': None,
            'generation_reused': False,
            'version': None,
            'grid_size': None,
            'attempts': [],
            'status': None,
            'published': False,
            'publication_version': None,
            'publishable': False,
            'missing_tiles': [],
            'unclosed_reasons': [],
            'notes': [],
        }
        try:
            status, content_type, body = self.fetch(manifest_url)
        except (urllib.error.URLError, OSError, ValueError) as error:
            return self._fail(summary, 'manifest_unavailable', 'transport_error', str(error))

        manifest_capture = self.archive.save(manifest_url, status, content_type, body)
        summary['manifest_capture_id'] = manifest_capture
        if status != 200:
            return self._fail(summary, 'manifest_unavailable', 'http_status', status)
        try:
            manifest = parse_manifest(body)
        except ValueError as error:
            return self._fail(summary, 'manifest_invalid', 'parse_error', str(error))

        version, grid_size = manifest['version'], manifest['grid_size']
        declaration = sorted(manifest['tiles'], key=lambda tile: tile['id'])
        generation_id, created = self.archive.create_generation(
            manifest_url, manifest_capture,
            self.archive.digest_of_capture(manifest_capture), version, grid_size, declaration)
        summary.update({
            'generation_id': generation_id,
            'generation_reused': not created,
            'version': version,
            'grid_size': grid_size,
            'abandoned_generations': getattr(self.archive, 'last_abandoned', []),
        })

        rows = {row['tile_id']: row for row in self.archive.tile_rows(generation_id)}
        for tile in declaration:
            tile_id = tile['id']
            row = rows[tile_id]
            verified = row['state'] in VERIFIED_STATES
            if verified and not refetch:
                continue
            url = urllib.parse.urljoin(manifest_url, tile['href'])
            attempt_number = self.archive.next_attempt_number(generation_id, tile_id)
            attempt = self._receive_tile(
                generation_id, version, grid_size, tile_id, url, verified, attempt_number)
            attempt['reused_verified_digest'] = verified
            summary['attempts'].append(attempt)
            rows = {item['tile_id']: item for item in self.archive.tile_rows(generation_id)}
            if attempt['state'] == 'version_mismatch':
                self.archive.set_generation_status(
                    generation_id, 'stale', '分片版本与清单不一致，停止拼接')
                summary['status'] = 'stale'
                return self._keep_previous_publication(summary)

        rows = {row['tile_id']: row for row in self.archive.tile_rows(generation_id)}
        tile_states = {tile: rows[tile]['state'] for tile in rows}
        summary['missing_tiles'] = sorted(
            tile for tile, state in tile_states.items() if state not in VERIFIED_STATES)
        try:
            assembled = self._assemble(generation_id, version, grid_size, rows)
        except ValueError as error:
            self.archive.save_snapshot(generation_id, {
                'version': version, 'grid_size': grid_size,
                'missing_tiles': summary['missing_tiles'], 'publishable': False,
                'unclosed_reasons': [], 'segments': [], 'error': str(error)})
            self.archive.set_generation_status(generation_id, 'receiving', '装配原文不可读')
            return self._fail(summary, 'incomplete', 'assemble_error', str(error))
        summary['publishable'] = assembled['publishable']
        summary['unclosed_reasons'] = assembled['unclosed_reasons']
        summary['segments'] = assembled['segments']
        self.archive.save_snapshot(generation_id, assembled)

        if not assembled['publishable']:
            self.archive.set_generation_status(generation_id, 'receiving', '仍有未闭合原因')
            summary['status'] = 'incomplete'
            return summary

        self.archive.set_generation_status(generation_id, 'complete', '接缝检查完成')
        published = self._publish(generation_id, version, grid_size, assembled)
        summary['status'] = 'published' if published else 'complete_unpublished'
        summary['published'] = published
        if published:
            summary['publication_version'] = version
        return summary

    def _receive_tile(self, generation_id, version, grid_size, tile_id, url,
                      previously_verified, attempt_number):
        attempt = {
            'tile_id': tile_id,
            'url': url,
            'attempt': attempt_number,
            'http_status': None,
            'capture_id': None,
            'state': 'unavailable',
            'detail': None,
        }
        try:
            status, content_type, body = self.fetch(url)
        except (urllib.error.URLError, OSError, ValueError) as error:
            attempt['detail'] = 'transport_error: ' + str(error)
            self.archive.record_attempt(
                generation_id, tile_id, attempt_number, None, None, None,
                'transport_error', attempt['detail'], update_tile=not previously_verified)
            return attempt

        capture_id = self.archive.save(url, status, content_type, body)
        attempt['http_status'] = status
        attempt['capture_id'] = capture_id
        if status != 200:
            attempt['detail'] = 'http_status: %d' % status
            self.archive.record_attempt(
                generation_id, tile_id, attempt_number, capture_id, status, None,
                'unavailable', attempt['detail'], update_tile=not previously_verified)
            return attempt
        try:
            parsed = parse_tile(body, version, tile_id, grid_size)
        except VersionMismatch as error:
            attempt['state'] = 'version_mismatch'
            attempt['detail'] = str(error)
            self.archive.record_attempt(
                generation_id, tile_id, attempt_number, capture_id, status, None,
                'version_mismatch', attempt['detail'], update_tile=not previously_verified)
            return attempt
        except ValueError as error:
            attempt['state'] = 'invalid'
            attempt['detail'] = str(error)
            self.archive.record_attempt(
                generation_id, tile_id, attempt_number, capture_id, status, None,
                'invalid', attempt['detail'], update_tile=not previously_verified)
            return attempt

        if previously_verified:
            current = next(row for row in self.archive.tile_rows(generation_id)
                           if row['tile_id'] == tile_id)
            if current['digest'] is not None and (
                    self.archive.digest_of_capture(capture_id) == current['digest']):
                attempt['state'] = 'redundant'
                attempt['detail'] = '重复下载，原文摘要与已核验一致'
                self.archive.record_attempt(
                    generation_id, tile_id, attempt_number, capture_id, status,
                    parsed['version'], 'redundant', attempt['detail'], update_tile=False)
                return attempt
        attempt['state'] = 'received'
        self.archive.record_attempt(
            generation_id, tile_id, attempt_number, capture_id, status,
            parsed['version'], 'received', None)
        return attempt

    def _assemble(self, generation_id, version, grid_size, rows):
        tile_states = {tile: rows[tile]['state'] for tile in rows}
        payloads = []
        for tile_id, row in sorted(rows.items()):
            if row['state'] not in VERIFIED_STATES:
                continue
            body = self.archive.read(row['capture_id'])
            payloads.append({
                'tile': tile_id,
                'capture_id': row['capture_id'],
                'digest': row['digest'],
                'features': parse_tile(body, version, tile_id, grid_size)['features'],
            })
        return assemble(payloads, version, grid_size, tile_states)

    def _publish(self, generation_id, version, grid_size, assembled):
        try:
            self.archive.publish(generation_id, version, grid_size, assembled)
        except Exception:
            self.archive.rollback()
            return False
        return True

    def _fail(self, summary, status, kind, detail):
        summary['status'] = status
        summary['notes'].append({'kind': kind, 'detail': detail})
        return self._keep_previous_publication(summary)

    def _keep_previous_publication(self, summary):
        previous = self.archive.publication()
        if previous is not None:
            summary['previous_publication'] = {
                'version': previous['version'],
                'generation_id': previous['generation_id'],
                'published_at': previous['published_at'],
            }
        return summary

    def _manifest_url(self):
        base = self.base_url.rstrip('/')
        if base.endswith('/manifest'):
            return base
        return base + '/manifest'


def generation_diagnostics(archive, generation_id=None):
    """查看已取片诊断：即使未发布也返回当前代次的接缝结论。"""
    if generation_id is None:
        row = archive.connection.execute(
            'select id from generations order by id desc limit 1').fetchone()
        if row is None:
            return None
        generation_id = row[0]
    generation = archive.generation(generation_id)
    if generation is None:
        raise KeyError(generation_id)
    return {
        'generation': {
            'id': generation['id'],
            'version': generation['version'],
            'grid_size': generation['grid_size'],
            'status': generation['status'],
            'note': generation['note'],
            'manifest_capture_id': generation['manifest_capture_id'],
            'manifest_digest': generation['manifest_digest'],
            'manifest_url': generation['manifest_url'],
            'created_at': generation['created_at'],
            'updated_at': generation['updated_at'],
        },
        'tiles': [{
            'tile_id': row['tile_id'], 'state': row['state'],
            'http_status': row['http_status'], 'capture_id': row['capture_id'],
            'digest': row['digest'], 'attempt_count': row['attempt_count'],
            'detail': row['detail'],
        } for row in archive.tile_rows(generation_id)],
        'attempts': [{
            'tile_id': row['tile_id'], 'attempt': row['attempt'],
            'http_status': row['http_status'], 'state': row['state'],
            'capture_id': row['capture_id'], 'version': row['version'],
            'detail': row['detail'], 'at': row['at'],
        } for row in archive.attempt_rows(generation_id)],
        'snapshot': archive.snapshot(generation_id),
        'publication': archive.publication(generation['version']),
    }


def road_view(archive, road_id, version=None):
    """按道路编号查询来源片、缓冲重复和接缝解释。"""
    publications = archive.publication_for_road(road_id)
    if version is not None:
        publications = [item for item in publications if item['version'] == version]
    return {
        'road': road_id,
        'version': version,
        'publications': publications,
    }


def summarize_receive(summary):
    """给命令行演示用的紧凑视图：保留编号、状态和原因，不展开证据全文。"""
    view = {key: value for key, value in summary.items()
            if key not in {'attempts', 'segments'}}
    view['attempts'] = [{
        'tile_id': attempt['tile_id'], 'attempt': attempt['attempt'],
        'http_status': attempt['http_status'], 'state': attempt['state'],
        'capture_id': attempt['capture_id'], 'detail': attempt['detail'],
        'reused_verified_digest': attempt.get('reused_verified_digest', False),
    } for attempt in summary.get('attempts', [])]
    view['segments'] = [_segment_summary(segment)
                        for segment in summary.get('segments', [])]
    return view


def _segment_summary(segment):
    return {
        'road': segment['road'],
        'partKey': segment['partKey'],
        'direction': segment['direction'],
        'status': segment['status'],
        'points': segment['points'],
        'sources': [{'tile': source['tile'],
                     'feature_index': source['feature_index'],
                     'capture_id': source['capture_id'],
                     'digest': source['digest'][:12],
                     'orientation': source['orientation'],
                     'point_span': source['point_span']}
                    for source in segment['sources']],
        'duplicates': [{'tile': item['tile'],
                        'feature_index': item['feature_index'],
                        'relationship': item['relationship'],
                        'duplicate_of': item['duplicate_of']}
                       for item in segment['duplicates']],
        'seam': segment['seam'],
        'unclosed_reasons': [{'reason': reason['reason'],
                              'blocking': reason['blocking'],
                              'tile': reason.get('tile'),
                              'point': reason.get('point'),
                              'detail': reason['detail']}
                             for reason in segment['unclosed_reasons']],
    }
