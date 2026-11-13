"""整数网格几何：原子边规范化、缓冲带重复识别与片邻接判断。

这里不连接端点、不做任何坐标转换，只回答「两条折线是否描述同一段原子边」
以及「两个分片是否在某个整数坐标上共享主体边线」。
"""


def as_point(value):
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or any(not isinstance(n, int) or isinstance(n, bool) for n in value)):
        raise ValueError('坐标必须是二维整数')
    return value[0], value[1]


def edge_sequence(points):
    """相邻顶点组成的原子边序列，保持点序。"""
    sequence = []
    for left, right in zip(points, points[1:]):
        start, end = as_point(left), as_point(right)
        if start == end:
            raise ValueError('折线包含零长度边')
        sequence.append((start, end))
    if not sequence:
        raise ValueError('折线至少需要一条边')
    return sequence


def canonical_edge(start, end):
    """both 方向下原子边无向；统一成端点有序形式。"""
    return (start, end) if start <= end else (end, start)


def canonical_edges(points):
    return [canonical_edge(start, end) for start, end in edge_sequence(points)]


def is_edge_subchain(short, long):
    """short 的规范化原子边是否连续出现在 long 中（不跨越缺失中间点）。"""
    if not short or len(short) > len(long):
        return False
    return any(long[index:index + len(short)] == short
               for index in range(len(long) - len(short) + 1))


def duplicate_relationship(candidate, existing, allow_reverse):
    """candidate 是否只是 existing 已覆盖原子边上的副本，以及点序关系。

    先规范化相邻顶点边，再判断连续子序列；单条长边 [0,10] 与 [0,5]+[5,10]
    不是同一原子边序列，不能只凭端点合并。返回 'same'、'reverse' 或 None。
    allow_reverse 只在双方 direction 都为 both 时为真：forward 的逆向点序有
    不同业务含义，绝不能当作等价段。
    """
    candidate_edges = canonical_edges(candidate)
    existing_edges = canonical_edges(existing)
    if is_edge_subchain(candidate_edges, existing_edges):
        return 'same'
    if allow_reverse and is_edge_subchain(
            list(reversed(candidate_edges)), existing_edges):
        return 'reverse'
    return None


def tile_coordinates(tile_id):
    try:
        x_text, y_text = tile_id.split('_')
        return int(x_text), int(y_text)
    except (AttributeError, ValueError):
        raise ValueError('分片编号必须是 x_y 整数网格坐标')


def body_bounds(tile_id, size):
    x, y = tile_coordinates(tile_id)
    return x * size, y * size, (x + 1) * size, (y + 1) * size


def inside_body(point, tile_id, size):
    left, bottom, right, top = body_bounds(tile_id, size)
    return left <= point[0] <= right and bottom <= point[1] <= top


def on_body_boundary(point, tile_id, size):
    left, bottom, right, top = body_bounds(tile_id, size)
    if not (left <= point[0] <= right and bottom <= point[1] <= top):
        return False
    return point[0] in (left, right) or point[1] in (bottom, top)


def share_edge_at(point, left_tile, right_tile, size):
    """两个分片主体在该点上是否共享一条边（对角只碰一点不算）。"""
    if left_tile == right_tile:
        return False
    left_x, left_y = tile_coordinates(left_tile)
    right_x, right_y = tile_coordinates(right_tile)
    if abs(left_x - right_x) + abs(left_y - right_y) != 1:
        return False
    return on_body_boundary(point, left_tile, size) and on_body_boundary(point, right_tile, size)
