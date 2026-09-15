"""Brotli-at-rest JSON I/O helpers (single source of truth, like paths.py).

Large write-once JSON artifacts (processed match JSON, overwatch raw extracts)
are stored brotli-compressed as ``<name>.json.br`` (~17-25x smaller). Small
mutable files (labels, baselines, .overwatch.json caches, demo_map) stay plain
JSON - atomic rewrite of a small plain file beats recompressing, and they
remain hand-inspectable.

Callers keep referring to the logical plain path (``…/foo.json``); these
helpers resolve the on-disk variant transparently, so pre-compression files
keep working unmigrated.
"""

import json
import os

import brotli

# Write-once artifacts: maximum quality is not worth the CPU on 20-30 MB
# inputs; 9 is within a few percent of 11 at a fraction of the time.
BR_QUALITY = 9


def resolve(path):
    """Return the on-disk file for a logical ``….json`` path - the plain file,
    the ``.br`` variant, or None when neither exists."""
    if os.path.isfile(path):
        return path
    br = path + '.br'
    if os.path.isfile(br):
        return br
    return None


def load(path):
    """Load JSON from a logical path, decompressing transparently.
    Raises FileNotFoundError (with the logical path) when neither variant exists."""
    real = resolve(path)
    if real is None:
        raise FileNotFoundError(path)
    with open(real, 'rb') as f:
        data = f.read()
    if real.endswith('.br'):
        data = brotli.decompress(data)
    return json.loads(data)


def dump_br(obj, path_br):
    """Serialize obj to a brotli-compressed JSON file, atomically (tmp+rename)."""
    payload = brotli.compress(json.dumps(obj).encode('utf-8'), quality=BR_QUALITY)
    _atomic_write(payload, path_br)


def compress_file(src_plain, dst_br, delete_src=False):
    """Compress an existing plain JSON file to dst_br atomically."""
    with open(src_plain, 'rb') as f:
        payload = brotli.compress(f.read(), quality=BR_QUALITY)
    _atomic_write(payload, dst_br)
    if delete_src:
        os.unlink(src_plain)


def _atomic_write(payload, path):
    tmp = path + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(payload)
    os.replace(tmp, path)
