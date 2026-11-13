DESCRIPTION = '配送道路分片 GeoJSON 的要素检查、HTTP 原文接收与区域接缝整理。'
DEMO_PATH = '/tiles/0_0'

import argparse
import json
import tempfile
import threading
from pathlib import Path

from .archive import Archive
from .parser import inspect_document
from .region import (
    RegionReceiver, generation_diagnostics, road_view, summarize_receive,
)
from .source_server import FixtureServer
from .transport import fetch


def main():
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    commands = parser.add_subparsers(dest='command', required=True)
    inspect = commands.add_parser('inspect')
    inspect.add_argument('path')
    capture = commands.add_parser('capture')
    capture.add_argument('url')
    capture.add_argument('--db', default='captures.sqlite3')
    capture.add_argument('--objects', default='objects')
    show = commands.add_parser('show')
    show.add_argument('--db', default='captures.sqlite3')
    show.add_argument('--objects', default='objects')
    receive = commands.add_parser('receive')
    receive.add_argument('base_url')
    receive.add_argument('--db', default='captures.sqlite3')
    receive.add_argument('--objects', default='objects')
    receive.add_argument('--refetch', action='store_true')
    diagnostics = commands.add_parser('diagnostics')
    diagnostics.add_argument('--db', default='captures.sqlite3')
    diagnostics.add_argument('--objects', default='objects')
    diagnostics.add_argument('--generation', type=int)
    road = commands.add_parser('road')
    road.add_argument('road_id')
    road.add_argument('--db', default='captures.sqlite3')
    road.add_argument('--objects', default='objects')
    road.add_argument('--version')
    commands.add_parser('demo')
    commands.add_parser('demo-region')
    arguments = parser.parse_args()
    if arguments.command == 'inspect':
        result = inspect_document(Path(arguments.path).read_bytes())
    elif arguments.command in {'capture', 'show'}:
        archive = Archive(arguments.db, arguments.objects)
        try:
            if arguments.command == 'capture':
                status, content_type, body = fetch(arguments.url)
                result = {'capture_id': archive.save(arguments.url, status, content_type, body),
                          'http_status': status}
            else:
                result = archive.rows()
        finally:
            archive.close()
    elif arguments.command in {'receive', 'diagnostics', 'road'}:
        archive = Archive(arguments.db, arguments.objects)
        try:
            if arguments.command == 'receive':
                result = summarize_receive(RegionReceiver(
                    archive, arguments.base_url).receive(refetch=arguments.refetch))
            elif arguments.command == 'diagnostics':
                result = generation_diagnostics(archive, arguments.generation)
            else:
                result = road_view(archive, arguments.road_id, arguments.version)
        finally:
            archive.close()
    else:
        result = demo_region() if arguments.command == 'demo-region' else demo()
    print(json.dumps(result, ensure_ascii=False, indent=2))


def demo():
    with FixtureServer() as server, tempfile.TemporaryDirectory() as directory:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = 'http://127.0.0.1:' + str(server.server_address[1])
            url = base + DEMO_PATH
            status, content_type, body = fetch(url)
            archive = Archive(Path(directory) / 'demo.sqlite3', Path(directory) / 'objects')
            try:
                capture_id = archive.save(url, status, content_type, body)
                return {'capture_id': capture_id, 'http_status': status,
                        'observation': inspect_document(archive.read(capture_id))}
            finally:
                archive.close()
        finally:
            server.shutdown()
            thread.join(timeout=2)


def demo_region():
    """迟到邻片演练：先看诊断，再核验接缝，最后发布区域结果。"""
    with FixtureServer() as server, tempfile.TemporaryDirectory() as directory:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = 'http://127.0.0.1:' + str(server.server_address[1])
            archive = Archive(Path(directory) / 'demo.sqlite3',
                              Path(directory) / 'objects')
            try:
                receiver = RegionReceiver(archive, base)
                first = summarize_receive(receiver.receive())
                fetch(base + '/control/phase?value=complete')
                second = summarize_receive(receiver.receive())
                return {
                    'before_neighbor': first,
                    'after_neighbor': second,
                    'road_query': road_view(archive, 'R7', 'roads1'),
                }
            finally:
                archive.close()
        finally:
            server.shutdown()
            thread.join(timeout=2)


if __name__ == '__main__':
    main()
