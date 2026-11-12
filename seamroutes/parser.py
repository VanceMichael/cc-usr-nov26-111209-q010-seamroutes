import json


def inspect_document(body):
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
            if not isinstance(point, list) or len(point) != 2 or any(not isinstance(coordinate, int) or isinstance(coordinate, bool) for coordinate in point):
                raise ValueError('坐标必须是二维整数')
        result.append({'road': properties.get('sourceRoadId'), 'part': properties.get('partKey'),
                       'point_count': len(points), 'start': points[0], 'end': points[-1]})
    return {'version': value.get('version'), 'tile': value.get('tile'), 'features': result}
