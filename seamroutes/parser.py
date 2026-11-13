"""单个分片文档的严格解析：单文档观察与区域接收共用。"""

import json

from .geometry import as_point, edge_sequence, inside_body, tile_coordinates


DIRECTIONS = {'both', 'forward'}
REQUIRED_PROPERTIES = ('sourceRoadId', 'partKey', 'direction', 'cutStart', 'cutEnd')


class VersionMismatch(ValueError):
    """分片自报版本与本次接收代次的清单不一致。"""


def inspect_document(body):
    """单文档观察：保留原有入口，只报告每段的端点概览。"""
    value = json.loads(body)
    if not isinstance(value, dict) or value.get('type') != 'FeatureCollection':
        raise ValueError('需要 FeatureCollection')
    features = value.get('features')
    if not isinstance(features, list):
        raise ValueError('features 必须是数组')
    result = []
    for feature in features:
        geometry, properties = feature.get('geometry', {}), feature.get('properties', {})
        points = geometry.get('coordinates')
        if geometry.get('type') != 'LineString' or not isinstance(points, list) or len(points) < 2:
            raise ValueError('需要至少两点的 LineString')
        for point in points:
            if (not isinstance(point, list) or len(point) != 2
                    or any(not isinstance(coordinate, int) or isinstance(coordinate, bool)
                           for coordinate in point)):
                raise ValueError('坐标必须是二维整数')
        result.append({'road': properties.get('sourceRoadId'), 'part': properties.get('partKey'),
                       'point_count': len(points), 'start': points[0], 'end': points[-1]})
    return {'version': value.get('version'), 'tile': value.get('tile'), 'features': result}


def parse_manifest(body):
    value = _load(body)
    version = value.get('version')
    if not isinstance(version, str) or not version:
        raise ValueError('清单 version 必须是非空字符串')
    grid_size = value.get('grid_size')
    if not isinstance(grid_size, int) or isinstance(grid_size, bool) or grid_size <= 0:
        raise ValueError('清单 grid_size 必须是正整数')
    tiles = value.get('tiles')
    if not isinstance(tiles, list) or not tiles:
        raise ValueError('清单 tiles 必须是非空数组')
    declaration = []
    seen = set()
    for entry in tiles:
        if not isinstance(entry, dict):
            raise ValueError('清单 tile 必须是对象')
        tile_id = entry.get('id')
        if not isinstance(tile_id, str):
            raise ValueError('清单 tile id 必须是字符串')
        tile_coordinates(tile_id)
        href = entry.get('href')
        if not isinstance(href, str) or not href:
            raise ValueError('清单 tile href 必须是非空字符串')
        if tile_id in seen:
            raise ValueError('清单 tile id 重复：' + tile_id)
        seen.add(tile_id)
        declaration.append({'id': tile_id, 'href': href})
    return {'version': version, 'grid_size': grid_size, 'tiles': declaration}


def parse_tile(body, expected_version, expected_tile, grid_size):
    """解析一份已完整取回的分片，返回规范化的要素与来源证据。"""
    value = _load(body)
    if not isinstance(value, dict) or value.get('type') != 'FeatureCollection':
        raise ValueError('需要 FeatureCollection')
    if value.get('version') != expected_version:
        raise VersionMismatch('分片 version 与清单不一致：%r != %r'
                              % (value.get('version'), expected_version))
    if value.get('tile') != expected_tile:
        raise ValueError('分片 tile 与清单不一致：%r != %r'
                         % (value.get('tile'), expected_tile))
    features = value.get('features')
    if not isinstance(features, list):
        raise ValueError('features 必须是数组')
    parsed = []
    for index, feature in enumerate(features):
        parsed.append(_parse_feature(feature, index, expected_tile, grid_size))
    return {'version': value['version'], 'tile': value['tile'], 'features': parsed}


def _load(body):
    try:
        value = json.loads(body)
    except (TypeError, ValueError):
        raise ValueError('响应不是有效 JSON')
    if not isinstance(value, dict):
        raise ValueError('顶层必须是对象')
    return value


def _parse_feature(feature, index, tile_id, grid_size):
    if not isinstance(feature, dict):
        raise ValueError('要素必须是对象')
    if feature.get('type') not in (None, 'Feature'):
        raise ValueError('要素 type 必须是 Feature')
    properties = feature.get('properties')
    if not isinstance(properties, dict):
        raise ValueError('要素 properties 必须是对象')
    missing = [name for name in REQUIRED_PROPERTIES if name not in properties]
    if missing:
        raise ValueError('要素缺少属性：' + ','.join(missing))
    road = properties['sourceRoadId']
    part = properties['partKey']
    direction = properties['direction']
    if not isinstance(road, str) or not road:
        raise ValueError('sourceRoadId 必须是非空字符串')
    if not isinstance(part, str) or not part:
        raise ValueError('partKey 必须是非空字符串')
    if direction not in DIRECTIONS:
        raise ValueError('direction 必须是 both 或 forward')
    for name in ('cutStart', 'cutEnd'):
        if not isinstance(properties[name], bool):
            raise ValueError(name + ' 必须是布尔值')
    geometry = feature.get('geometry')
    if not isinstance(geometry, dict) or geometry.get('type') != 'LineString':
        raise ValueError('需要 LineString 几何')
    coordinates = geometry.get('coordinates')
    if not isinstance(coordinates, list) or len(coordinates) < 2:
        raise ValueError('需要至少两点的 LineString')
    points = [list(as_point(point)) for point in coordinates]
    edge_sequence(points)
    body_points = sum(1 for point in points if inside_body(point, tile_id, grid_size))
    return {
        'road': road,
        'part': part,
        'direction': direction,
        'name': properties.get('name'),
        'cutStart': properties['cutStart'],
        'cutEnd': properties['cutEnd'],
        'points': points,
        'tile': tile_id,
        'feature_index': index,
        'body_point_count': body_points,
    }
