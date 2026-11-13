import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path


class Archive:
    def __init__(self, database, objects):
        self.objects = Path(objects)
        self.objects.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(database)
        self.connection.execute('pragma foreign_keys=on')
        self.last_abandoned = []
        self.connection.executescript("""
            create table if not exists captures (
                id integer primary key, url text not null, status integer not null,
                content_type text not null, digest text not null, size integer not null,
                received_at text not null default current_timestamp
            );
            create index if not exists capture_digest on captures(digest);
            create table if not exists generations (
                id integer primary key, manifest_url text not null,
                manifest_capture_id integer references captures(id),
                manifest_digest text, version text not null, grid_size integer not null,
                declaration text not null, fingerprint text not null,
                status text not null, note text,
                created_at text not null default current_timestamp,
                updated_at text not null default current_timestamp,
                published_at text
            );
            create index if not exists generation_version on generations(version, fingerprint);
            create table if not exists generation_tiles (
                generation_id integer not null references generations(id),
                tile_id text not null, ordinal integer not null, href text not null,
                capture_id integer references captures(id), http_status integer,
                digest text, version text, state text not null, detail text,
                attempt_count integer not null default 0,
                primary key(generation_id, tile_id)
            );
            create table if not exists tile_attempts (
                id integer primary key,
                generation_id integer not null references generations(id),
                tile_id text not null, attempt integer not null,
                capture_id integer references captures(id), http_status integer,
                version text, state text not null, detail text,
                at text not null default current_timestamp
            );
            create table if not exists generation_snapshots (
                generation_id integer primary key references generations(id),
                result text not null,
                updated_at text not null default current_timestamp
            );
            create table if not exists publications (
                generation_id integer primary key references generations(id),
                version text not null, grid_size integer not null,
                result text not null,
                published_at text not null default current_timestamp
            );
        """)

    def save(self, url, status, content_type, body):
        digest = hashlib.sha256(body).hexdigest()
        destination = self.objects / digest
        if destination.exists():
            if destination.read_bytes() != body:
                raise ValueError('原文对象校验失败')
        else:
            descriptor, temporary = tempfile.mkstemp(prefix='.receive-', dir=self.objects)
            try:
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, destination)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        with self.connection:
            cursor = self.connection.execute(
                'insert into captures(url,status,content_type,digest,size) values(?,?,?,?,?)',
                (url, status, content_type, digest, len(body)),
            )
        return cursor.lastrowid

    def read(self, capture_id):
        row = self.connection.execute('select digest from captures where id=?', (capture_id,)).fetchone()
        if row is None:
            raise KeyError(capture_id)
        body = (self.objects / row[0]).read_bytes()
        if hashlib.sha256(body).hexdigest() != row[0]:
            raise ValueError('原文摘要不匹配')
        return body

    def rows(self):
        self.connection.row_factory = sqlite3.Row
        return [dict(row) for row in self.connection.execute('select * from captures order by id')]

    def generation_rows(self):
        self.connection.row_factory = sqlite3.Row
        return [dict(row) for row in self.connection.execute(
            'select * from generations order by id')]

    def tile_rows(self, generation_id):
        self.connection.row_factory = sqlite3.Row
        return [dict(row) for row in self.connection.execute(
            'select * from generation_tiles where generation_id=? order by ordinal',
            (generation_id,))]

    def attempt_rows(self, generation_id):
        self.connection.row_factory = sqlite3.Row
        return [dict(row) for row in self.connection.execute(
            'select * from tile_attempts where generation_id=? order by id',
            (generation_id,))]

    def create_generation(self, manifest_url, manifest_capture_id, manifest_digest, version,
                          grid_size, declaration):
        """按清单声明找同一代次；换版时封存旧代次，绝不复用旧邻片。"""
        fingerprint = hashlib.sha256(
            json.dumps(declaration, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        existing = self.connection.execute(
            'select id from generations where manifest_url=? and version=? and fingerprint=? '
            'and status in (?,?,?) order by id desc limit 1',
            (manifest_url, version, fingerprint, 'receiving', 'complete', 'published')).fetchone()
        self.last_abandoned = self.abandon_receiving(
            manifest_url, '清单换版：%s/%s' % (version, fingerprint[:12]),
            keep=existing[0] if existing else None)
        if existing is not None and self._has_captured_tiles(existing[0]):
            return existing[0], False
        if existing is not None:
            # 同版本同声明的旧代次没有留下任何已取片，删除空壳后重建。
            self.connection.execute('delete from generation_tiles where generation_id=?',
                                    (existing[0],))
            self.connection.execute('delete from generations where id=?', (existing[0],))
        cursor = self.connection.execute(
            'insert into generations(manifest_url,manifest_capture_id,manifest_digest,'
            'version,grid_size,declaration,fingerprint,status,note) '
            'values(?,?,?,?,?,?,?,?,?)',
            (manifest_url, manifest_capture_id, manifest_digest, version, grid_size,
             json.dumps(declaration, ensure_ascii=False, sort_keys=True), fingerprint,
             'receiving', 'new generation'))
        generation_id = cursor.lastrowid
        self.connection.executemany(
            'insert into generation_tiles(generation_id,tile_id,ordinal,href,state) '
            'values(?,?,?,?,?)',
            [(generation_id, tile['id'], index, tile['href'], 'pending')
             for index, tile in enumerate(declaration)])
        self.connection.commit()
        return generation_id, True

    def abandon_receiving(self, manifest_url, reason, keep=None):
        """清单换版时封存同一来源仍在接收中的代次，旧邻片不得与新片拼接。"""
        with self.connection:
            rows = self.connection.execute(
                "select id from generations where manifest_url=? and status='receiving'",
                (manifest_url,)).fetchall()
            abandoned = []
            for (generation_id,) in rows:
                if generation_id == keep:
                    continue
                self.connection.execute(
                    'update generations set status=?, note=?, updated_at=current_timestamp '
                    'where id=?', ('abandoned', reason, generation_id))
                abandoned.append(generation_id)
            return abandoned

    def _has_captured_tiles(self, generation_id):
        row = self.connection.execute(
            'select count(*) from generation_tiles where generation_id=? and state in (?,?,?)',
            (generation_id, 'received', 'redundant', 'rejected')).fetchone()
        return row[0] > 0

    def set_generation_status(self, generation_id, status, note=None):
        with self.connection:
            self.connection.execute(
                'update generations set status=?, note=?, updated_at=current_timestamp '
                'where id=?', (status, note, generation_id))

    def record_attempt(self, generation_id, tile_id, attempt, capture_id, http_status,
                       version, state, detail=None, update_tile=True):
        with self.connection:
            self.connection.execute(
                'insert into tile_attempts(generation_id,tile_id,attempt,capture_id,'
                'http_status,version,state,detail) values(?,?,?,?,?,?,?,?)',
                (generation_id, tile_id, attempt, capture_id, http_status, version,
                 state, detail))
            if not update_tile:
                return
            row = self.connection.execute(
                'select digest, size from captures where id=?', (capture_id,)).fetchone()
            digest = row[0] if row else None
            self.connection.execute(
                'update generation_tiles set attempt_count=coalesce(attempt_count,0)+1 '
                'where generation_id=? and tile_id=?', (generation_id, tile_id))
            self.connection.execute(
                'update generation_tiles set capture_id=?, http_status=?, digest=?, '
                'version=?, state=?, detail=? where generation_id=? and tile_id=?',
                (capture_id, http_status, digest, version, state, detail,
                 generation_id, tile_id))

    def tile_digest(self, generation_id, tile_id):
        row = self.connection.execute(
            'select digest from generation_tiles where generation_id=? and tile_id=?',
            (generation_id, tile_id)).fetchone()
        return row[0] if row else None

    def next_attempt_number(self, generation_id, tile_id):
        row = self.connection.execute(
            'select coalesce(max(attempt),0) from tile_attempts '
            'where generation_id=? and tile_id=?', (generation_id, tile_id)).fetchone()
        return row[0] + 1

    def digest_of_capture(self, capture_id):
        row = self.connection.execute(
            'select digest from captures where id=?', (capture_id,)).fetchone()
        return row[0] if row else None

    def generation(self, generation_id):
        self.connection.row_factory = sqlite3.Row
        row = self.connection.execute(
            'select * from generations where id=?', (generation_id,)).fetchone()
        return dict(row) if row else None

    def save_snapshot(self, generation_id, result):
        with self.connection:
            self.connection.execute(
                'insert into generation_snapshots(generation_id,result,updated_at) '
                'values(?,?,current_timestamp) on conflict(generation_id) do update set '
                'result=excluded.result, updated_at=current_timestamp',
                (generation_id, json.dumps(result, ensure_ascii=False, sort_keys=True)))

    def snapshot(self, generation_id):
        self.connection.row_factory = sqlite3.Row
        row = self.connection.execute(
            'select result from generation_snapshots where generation_id=?',
            (generation_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def publish(self, generation_id, version, grid_size, result):
        """取代表次发布；同一版本若重取失败，调用方不触碰此方法。"""
        payload = json.dumps(result, ensure_ascii=False, sort_keys=True)
        with self.connection:
            self.connection.execute('delete from publications where version=?', (version,))
            self.connection.execute(
                'insert into publications(generation_id,version,grid_size,result) '
                'values(?,?,?,?)', (generation_id, version, grid_size, payload))
            self.connection.execute(
                'update generations set status=?, published_at=current_timestamp, '
                'updated_at=current_timestamp where id=?', ('published', generation_id))

    def rollback(self):
        self.connection.rollback()

    def publication(self, version=None):
        self.connection.row_factory = sqlite3.Row
        if version is None:
            row = self.connection.execute(
                'select * from publications order by published_at desc, generation_id desc '
                'limit 1').fetchone()
        else:
            row = self.connection.execute(
                'select * from publications where version=? order by published_at desc, '
                'generation_id desc limit 1', (version,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        value['result'] = json.loads(value['result'])
        return value

    def publication_for_road(self, road_id):
        """按道路编号查询发布版本中的证据；同版本只认最新发布代次。"""
        self.connection.row_factory = sqlite3.Row
        result = []
        seen_versions = set()
        for row in self.connection.execute(
                'select * from publications order by published_at desc, generation_id desc'):
            if row['version'] in seen_versions:
                continue
            seen_versions.add(row['version'])
            payload = json.loads(row['result'])
            hits = [segment for segment in payload.get('segments', [])
                    if segment.get('road') == road_id]
            if hits:
                result.append({'version': row['version'], 'generation_id': row['generation_id'],
                               'published_at': row['published_at'], 'segments': hits})
        return result

    def close(self):
        self.connection.close()
