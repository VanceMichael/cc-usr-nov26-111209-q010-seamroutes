"""区域接缝装配：去缓冲重复、按切边配对、输出可追溯道路线记录。

本模块只整理来源已提供的折线：不插值、不补线、不做坐标转换、不按端点
接近或同一 sourceRoadId 把不连通分支连成一条线。真正无法从来源确认的
端点一律写进 unclosed_reasons。
"""

from .geometry import (
    canonical_edges, duplicate_relationship, on_body_boundary, share_edge_at,
)


def assemble(tiles, version, grid_size, tile_states):
    """把一组已核验分片整理成道路线记录。

    tiles: [{'tile', 'features'(parser.parse_tile 结果), 'capture_id', 'digest'}]
    tile_states: {tile_id: state}；声明但未核验的分片为 pending/unavailable/rejected。
    """
    declared = sorted(tile_states)
    verified_states = {tile_id: state for tile_id, state in tile_states.items()
                       if state in {'received', 'redundant'}}
    missing_tiles = sorted(tile_id for tile_id in tile_states
                           if tile_id not in verified_states)
    ordinal = {tile_id: index for index, tile_id in enumerate(declared)}

    features = []
    for payload in tiles:
        tile_id = payload['tile']
        for feature in payload['features']:
            features.append({
                'gid': len(features),
                'tile': tile_id,
                'ordinal': ordinal.get(tile_id, len(ordinal)),
                'feature_index': feature['feature_index'],
                'capture_id': payload.get('capture_id'),
                'digest': payload.get('digest'),
                'road': feature['road'],
                'part': feature['part'],
                'direction': feature['direction'],
                'name': feature['name'],
                'cutStart': feature['cutStart'],
                'cutEnd': feature['cutEnd'],
                'points': feature['points'],
            })

    kept = []
    duplicates = []
    for _, group in _group_features(features):
        group_kept, group_duplicates = _deduplicate(group)
        kept.extend(group_kept)
        duplicates.extend(group_duplicates)

    conflicts, conflict_groups = _attribute_conflicts(features)
    pairing, port_notes = _pair_ports(kept, grid_size)

    segments = []
    for _, group in _group_features(kept):
        for component in _components(group, pairing):
            segments.extend(_component_segments(
                component, features, pairing, port_notes, conflict_groups,
                tile_states, verified_states, declared, grid_size,
            ))
    segments.sort(key=lambda segment: (
        segment['road'], segment['partKey'], segment['direction'],
        segment['sources'][0]['tile'] if segment['sources'] else '',
        segment['sources'][0]['feature_index'] if segment['sources'] else -1,
        segment['points'][0] if segment['points'] else [],
    ))

    duplicates_by_canonical = {}
    for record in duplicates:
        duplicates_by_canonical.setdefault(record['canonical_gid'], []).append(record)
    for segment in segments:
        canonical_gids = segment.pop('_canonical_gids', [])
        for gid in canonical_gids:
            for record in duplicates_by_canonical.get(gid, []):
                segment['duplicates'].append(_duplicate_view(record))
        segment['sources'].sort(key=lambda item: (item['tile'], item['feature_index']))
        segment['duplicates'].sort(key=lambda item: (item['tile'], item['feature_index']))
        segment['seam'].sort(key=lambda item: (
            item['kind'], item.get('tile', ''), item.get('feature_index', -1),
            item.get('point') or []))
        segment['unclosed_reasons'].sort(key=lambda item: (
            item['reason'], item.get('tile', ''), item.get('feature_index', -1),
            item.get('point') or []))
        segment['closed'] = not any(reason['blocking']
                                    for reason in segment['unclosed_reasons'])
        segment['status'] = 'closed' if segment['closed'] else 'unclosed'

    blocking = [dict(reason, road=segment['road'], partKey=segment['partKey'],
                     direction=segment['direction'])
                for segment in segments for reason in segment['unclosed_reasons']
                if reason['blocking']]
    publishable = not missing_tiles and not blocking
    result = {
        'version': version,
        'grid_size': grid_size,
        'tiles': declared,
        'verified_tiles': sorted(verified_states),
        'missing_tiles': missing_tiles,
        'segments': segments,
        'duplicates': [_duplicate_view(record) for record in duplicates],
        'conflicts': conflicts,
        'unclosed_reasons': blocking,
        'publishable': publishable,
    }
    return result


def _group_features(features):
    groups = {}
    for feature in features:
        groups.setdefault((feature['road'], feature['part']), []).append(feature)
    return sorted(groups.items(), key=lambda item: (
        item[0][0], item[0][1],
        min(feature['ordinal'] for feature in item[1])))


def _deduplicate(group):
    """同道路、同 partKey 内，把被别的折线原子边序列完整覆盖的副本降级。

    先按边数、分片序、要素序选出规范代表，再让副本指向它。反向只在双方
    direction 都为 both 时等价；forward 与 both 的同形线段是不同的业务事实，
    跨越不同中间顶点的折线不会被判成重复。
    """
    order = sorted(group, key=lambda feature: (
        -len(canonical_edges(feature['points'])), feature['ordinal'],
        feature['feature_index'], feature['gid']))
    canonical = []
    duplicates = []
    for feature in order:
        matched = None
        for representative in canonical:
            if feature['direction'] != representative['direction']:
                continue
            relation = duplicate_relationship(
                feature['points'], representative['points'],
                allow_reverse=feature['direction'] == representative['direction'] == 'both',
            )
            if relation is not None:
                matched = (representative, relation)
                break
        if matched is None:
            canonical.append(feature)
        else:
            representative, relation = matched
            duplicates.append({
                'gid': feature['gid'],
                'canonical_gid': representative['gid'],
                'relationship': relation,
                'tile': feature['tile'],
                'feature_index': feature['feature_index'],
                'capture_id': feature['capture_id'],
                'digest': feature['digest'],
                'canonical_tile': representative['tile'],
                'canonical_feature_index': representative['feature_index'],
                'points': feature['points'],
            })
    kept = sorted(canonical, key=lambda item: (
        item['ordinal'], item['feature_index'], item['gid']))
    return kept, duplicates


def _attribute_conflicts(features):
    by_branch = {}
    for feature in features:
        by_branch.setdefault((feature['road'], feature['part']), []).append(feature)
    conflicts = []
    conflicted = set()
    for (road, part), group in sorted(by_branch.items()):
        names = sorted({feature['name'] for feature in group
                        if feature['name'] is not None})
        directions = sorted({feature['direction'] for feature in group})
        if len(names) > 1:
            conflicts.append({
                'road': road, 'partKey': part, 'attribute': 'name', 'values': names,
                'sources': [{'tile': feature['tile'],
                             'feature_index': feature['feature_index'],
                             'value': feature['name']} for feature in group],
            })
            conflicted.add((road, part))
        if len(directions) > 1:
            conflicts.append({
                'road': road, 'partKey': part, 'attribute': 'direction',
                'values': directions,
                'sources': [{'tile': feature['tile'],
                             'feature_index': feature['feature_index'],
                             'value': feature['direction']} for feature in group],
            })
            conflicted.add((road, part))
    return conflicts, conflicted


def _port(feature, side):
    return (feature['gid'], side)


def _other(side):
    return 'end' if side == 'start' else 'start'


def _cut(feature, side):
    return feature['cutStart'] if side == 'start' else feature['cutEnd']


def _point(feature, side):
    return tuple(feature['points'][0] if side == 'start' else feature['points'][-1])


def _pair_ports(kept, grid_size):
    """只按精确整数坐标、同道路、同 partKey、同方向、切边标志配对。

    返回配对的端口映射，以及每个歧义位置的说明。配对必须一对一；同一位置
    出现两个以上切边端口记为岔路，不允许任选一个。
    """
    ordered = sorted(kept, key=lambda item: (
        item['ordinal'], item['feature_index'], item['gid']))
    candidates = {}
    ports_at_point = {}
    for left_index, left in enumerate(ordered):
        for right in ordered[left_index + 1:]:
            if left['tile'] == right['tile']:
                continue
            if (left['road'], left['part'], left['direction']) != (
                    right['road'], right['part'], right['direction']):
                continue
            for left_side in ('start', 'end'):
                for right_side in ('start', 'end'):
                    if not _cut(left, left_side) or not _cut(right, right_side):
                        continue
                    if _point(left, left_side) != _point(right, right_side):
                        continue
                    point = _point(left, left_side)
                    if not share_edge_at(point, left['tile'], right['tile'], grid_size):
                        continue
                    if left['direction'] == 'forward' and (
                            (left_side, right_side) not in {('end', 'start'), ('start', 'end')}):
                        continue
                    left_port = _port(left, left_side)
                    right_port = _port(right, right_side)
                    candidates.setdefault(left_port, []).append(right_port)
                    candidates.setdefault(right_port, []).append(left_port)
                    ports_at_point.setdefault(point, set()).update(
                        {left_port, right_port})

    ambiguous = {port for port, options in candidates.items()
                 if len(set(options)) > 1}
    notes = {}
    for point, ports in ports_at_point.items():
        if len(ports) > 2:
            reason = 'junction'
            detail = '同一接缝位置有多于两个切边端口，存在岔路'
        elif any(port in ambiguous for port in ports):
            reason = 'ambiguous_seam'
            detail = '同一位置有多个可配对的互补切边，无法确定唯一接缝'
        else:
            continue
        for port in ports:
            if port in ambiguous or len(ports) > 2:
                notes[port] = {'reason': reason, 'detail': detail,
                               'point': list(point),
                               'candidates': len(set(candidates.get(port, [])))}
    pairing = {}
    for port in sorted(candidates):
        if port in notes:
            continue
        options = sorted(set(candidates[port]))
        if len(options) != 1:
            continue
        partner = options[0]
        if partner in notes or sorted(set(candidates.get(partner, []))) != [port]:
            continue
        pairing[port] = partner
    return pairing, notes


def _components(group, pairing):
    parent = {feature['gid']: feature['gid'] for feature in group}

    def find(gid):
        while parent[gid] != gid:
            parent[gid] = parent[parent[gid]]
            gid = parent[gid]
        return gid

    gids = set(parent)
    for port, partner in pairing.items():
        if port[0] in gids and partner[0] in gids:
            parent[find(port[0])] = find(partner[0])
    grouped = {}
    for feature in group:
        grouped.setdefault(find(feature['gid']), []).append(feature)
    return [sorted(items, key=lambda item: (
        item['ordinal'], item['feature_index'], item['gid']))
        for _, items in sorted(grouped.items())]


def _component_segments(component, features, pairing, port_notes, conflict_groups,
                        tile_states, verified_states, declared, grid_size):
    by_gid = {feature['gid']: feature for feature in component}
    direction = component[0]['direction']
    free_ports = sorted(
        (port for feature in component
         for port in (_port(feature, 'start'), _port(feature, 'end'))
         if pairing.get(port) is None),
        key=lambda port: (by_gid[port[0]]['ordinal'],
                          by_gid[port[0]]['feature_index'],
                          port[1] != 'start', port[0]),
    )
    if not free_ports:
        if direction == 'forward':
            return [_forced_segment(feature, 'junction',
                                    list(feature['points'][0]),
                                    'forward 分支首尾都被配对，无法确定起点',
                                    conflict_groups, component)
                    for feature in component]
        sequence = _cycle_sequence(component, pairing, by_gid)
        if len(sequence) != len(component):
            return [_forced_segment(feature, 'junction',
                                    list(feature['points'][0]),
                                    '环状证据没有覆盖整个连通分支',
                                    conflict_groups, component)
                    for feature in component]
        return [_build_segment(sequence, None, None, component, features, pairing,
                               port_notes, conflict_groups, tile_states,
                               verified_states, declared, grid_size, ring=True)]

    if direction == 'forward':
        starts = [port for port in free_ports if port[1] == 'start']
        if not starts:
            return [_forced_segment(feature, 'forward_reversed',
                                    list(feature['points'][0]),
                                    'forward 分支没有可用的起点，点序不能反转',
                                    conflict_groups, component)
                    for feature in component]
        start_port = starts[0]
    else:
        start_port = free_ports[0]

    sequence, stopped = _walk(start_port, pairing, by_gid, direction)
    if stopped is not None:
        point = _stop_point(sequence, by_gid, start_port)
        return [_forced_segment(feature, stopped, point,
                                '该分支的配对关系无法形成唯一路径',
                                conflict_groups, component)
                for feature in component]
    exit_port = _port(by_gid[sequence[-1][0]], _other(sequence[-1][1]))
    return [_build_segment(sequence, start_port, exit_port, component, features, pairing,
                           port_notes, conflict_groups, tile_states,
                           verified_states, declared, grid_size)]


def _stop_point(sequence, by_gid, start_port):
    if sequence:
        gid, side = sequence[-1]
        return list(_point(by_gid[gid], _other(side)))
    return list(_point(by_gid[start_port[0]], start_port[1]))


def _walk(start_port, pairing, by_gid, direction):
    sequence = []
    seen = set()
    current = start_port
    while True:
        gid, entry_side = current
        if gid in seen:
            return sequence, 'junction'
        if gid not in by_gid:
            return sequence, 'ambiguous_seam'
        seen.add(gid)
        if direction == 'forward' and entry_side != 'start':
            return sequence, 'forward_reversed'
        sequence.append((gid, entry_side))
        partner = pairing.get(_port(by_gid[gid], _other(entry_side)))
        if partner is None:
            return sequence, None
        current = partner


def _cycle_sequence(component, pairing, by_gid):
    start_port = sorted(
        (port for feature in component
         for port in (_port(feature, 'start'), _port(feature, 'end'))),
        key=lambda port: (by_gid[port[0]]['ordinal'],
                          by_gid[port[0]]['feature_index'], port[1]),
    )[0]
    sequence = []
    current = start_port
    for _ in range(len(component) + 1):
        gid, entry_side = current
        sequence.append((gid, entry_side))
        partner = pairing.get(_port(by_gid[gid], _other(entry_side)))
        if partner is None or partner == start_port:
            break
        current = partner
    return sequence


def _forced_segment(feature, reason, point, detail, conflict_groups, component):
    segment = _blank_segment(feature)
    segment['sources'] = [{
        'tile': feature['tile'],
        'feature_index': feature['feature_index'],
        'capture_id': feature['capture_id'],
        'digest': feature['digest'],
        'orientation': 'as_source',
        'point_span': [0, len(feature['points']) - 1],
        'source_points': feature['points'],
    }]
    segment['points'] = [list(item) for item in feature['points']]
    segment['unclosed_reasons'].append({
        'reason': reason, 'blocking': True,
        'tile': feature['tile'], 'feature_index': feature['feature_index'],
        'cut': None, 'point': point, 'detail': detail,
    })
    _append_conflict_reasons(segment, [feature], conflict_groups)
    return segment


def _blank_segment(feature):
    return {
        'road': feature['road'],
        'partKey': feature['part'],
        'direction': feature['direction'],
        'name': feature['name'],
        'points': [],
        'closed': True,
        'status': 'closed',
        'unclosed_reasons': [],
        'sources': [],
        'duplicates': [],
        'seam': [],
        '_canonical_gids': [],
    }


def _merge_points(sequence, by_gid):
    points = []
    spans = []
    for gid, entry_side in sequence:
        feature = by_gid[gid]
        oriented = (feature['points'] if entry_side == 'start'
                    else list(reversed(feature['points'])))
        start_index = len(points)
        if points and points[-1] == oriented[0]:
            oriented = oriented[1:]
            start_index = len(points) - 1
        points.extend([list(point) for point in oriented])
        spans.append((gid, start_index, len(points) - 1))
    return points, spans


def _build_segment(sequence, entry_port, exit_port, component, features, pairing,
                   port_notes, conflict_groups, tile_states, verified_states,
                   declared, grid_size, ring=False):
    by_gid = {feature['gid']: feature for feature in features}
    points, spans = _merge_points(sequence, by_gid)
    first = by_gid[sequence[0][0]]
    segment = _blank_segment(first)
    segment['points'] = points
    segment['_canonical_gids'] = [gid for gid, _ in sequence]
    for (gid, start_index, end_index), (_, entry_side) in zip(spans, sequence):
        feature = by_gid[gid]
        segment['sources'].append({
            'tile': feature['tile'],
            'feature_index': feature['feature_index'],
            'capture_id': feature['capture_id'],
            'digest': feature['digest'],
            'orientation': 'as_source' if entry_side == 'start' else 'reversed',
            'point_span': [start_index, end_index],
            'source_points': feature['points'],
        })
    if ring:
        segment['seam'].append({
            'kind': 'closed_ring', 'blocking': False,
            'detail': '首尾相接的环，没有真实端点',
        })
    if entry_port is not None:
        _classify_port(entry_port, by_gid, segment, port_notes, tile_states,
                       verified_states, declared, grid_size, 'head')
    if exit_port is not None:
        _classify_port(exit_port, by_gid, segment, port_notes, tile_states,
                       verified_states, declared, grid_size, 'tail')
    _append_conflict_reasons(segment, [by_gid[gid] for gid, _ in sequence],
                             conflict_groups)
    return segment


def _classify_port(port, by_gid, segment, port_notes, tile_states, verified_states,
                   declared, grid_size, position):
    feature = by_gid[port[0]]
    side = port[1]
    point = _point(feature, side)
    cut = _cut(feature, side)
    base = {
        'tile': feature['tile'], 'feature_index': feature['feature_index'],
        'cut': side, 'point': list(point),
    }
    if not cut:
        segment['seam'].append(dict(base, kind='true_endpoint', blocking=False,
                                    side=position,
                                    detail='来源未标切边，是真实道路端点'))
        return
    note = port_notes.get(port)
    if note is not None:
        segment['unclosed_reasons'].append(dict(
            base, reason=note['reason'], blocking=True,
            candidates=note['candidates'], detail=note['detail']))
        return
    if not on_body_boundary(point, feature['tile'], grid_size):
        segment['unclosed_reasons'].append(dict(
            base, reason='cut_off_boundary', blocking=True,
            detail='切边端点不在本片主体边界上'))
        return
    neighbors = sorted(tile for tile in declared
                       if tile != feature['tile']
                       and share_edge_at(point, feature['tile'], tile, grid_size))
    if not neighbors:
        segment['seam'].append(dict(base, kind='region_boundary_cut', blocking=False,
                                    side=position,
                                    detail='区域声明边界的切边，声明内没有邻片'))
        return
    missing = [tile for tile in neighbors if tile not in verified_states]
    if missing:
        segment['unclosed_reasons'].append(dict(
            base, reason='missing_neighbor', blocking=True,
            neighbors=neighbors, missing_tiles=missing,
            verified_neighbors=[tile for tile in neighbors if tile in verified_states],
            detail='所在接缝的邻片尚未核验，不能确认闭合'))
        return
    segment['unclosed_reasons'].append(dict(
        base, reason='unpaired_cut', blocking=True, neighbors=neighbors,
        missing_tiles=[],
        detail='邻片已核验，但同一位置没有来源给出的互补切边'))


def _append_conflict_reasons(segment, features, conflict_groups):
    for road, part in sorted(conflict_groups):
        if road != segment['road'] or part != segment['partKey']:
            continue
        for feature in features:
            segment['unclosed_reasons'].append({
                'reason': 'attribute_conflict', 'blocking': True,
                'tile': feature['tile'],
                'feature_index': feature['feature_index'],
                'cut': None, 'point': None,
                'detail': '同一道路分支的属性不一致，不能确定闭合',
            })


def _duplicate_view(record):
    return {
        'tile': record['tile'],
        'feature_index': record['feature_index'],
        'capture_id': record['capture_id'],
        'digest': record['digest'],
        'duplicate_of': {'tile': record['canonical_tile'],
                         'feature_index': record['canonical_feature_index']},
        'relationship': record['relationship'],
        'source_points': record['points'],
    }
