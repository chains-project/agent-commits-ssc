"""Shared offline IO; portable runtime has no project or network dependency."""
import csv
import hashlib
import json
import sqlite3
import zlib
from pathlib import Path


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def pack(value):
    return zlib.compress(dump(value).encode('utf-8'))


def unpack(value):
    return json.loads(zlib.decompress(value))


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def csv_rows(path):
    csv.field_size_limit(100_000_000)
    with Path(path).open(encoding='utf-8-sig', newline='') as handle:
        yield from csv.DictReader(handle)


def read_db(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    return db


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
