#!/usr/bin/env python3
"""One-time (idempotent) migration of an existing data/ tree to the
brotli-at-rest layout:

  1. processed_matches/<id>.json      → <id>.json.br   (round-trip verified)
  2. overwatch_raw/<id>.json          → <id>.json.br   (round-trip verified)
  3. chunks/<id>/ticks_chunk_*.json   → DELETED, but only for matches whose
     master JSON has a chunkIndexV2 with every referenced .bin.br present on
     disk. JSON tick chunks are unregenerable without the original .dem, so
     any match without full v2 coverage is kept and reported.

The .overwatch.json caches, analysis_data/, demo_map.json and demos/ are
untouched (small / mutable / originals).

Usage:  python3 tools/compress_data.py [--dry-run] [--keep-json-chunks]
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import jsonio   # noqa: E402
import paths    # noqa: E402


def human(n):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(n) < 1024 or unit == 'TB':
            return f'{n:.1f} {unit}' if unit != 'B' else f'{n} B'
        n /= 1024


def compress_tree(folder, label, dry_run, skip=lambda f: False):
    """Compress every *.json in folder to *.json.br with round-trip verify.
    Returns (saved_bytes, count)."""
    import brotli
    saved = count = 0
    if not os.path.isdir(folder):
        return saved, count
    for fname in sorted(os.listdir(folder)):
        if not fname.endswith('.json') or skip(fname):
            continue
        src = os.path.join(folder, fname)
        dst = src + '.br'
        size = os.path.getsize(src)
        if os.path.isfile(dst):
            # Already migrated - a leftover plain twin just gets removed
            # (resolve() prefers the plain file, which would mask the .br).
            print(f'  {label}/{fname}: .br exists, removing plain duplicate')
            if not dry_run:
                os.unlink(src)
            saved += size
            count += 1
            continue
        if dry_run:
            print(f'  {label}/{fname}: would compress ({human(size)})')
            count += 1
            continue
        with open(src, 'rb') as f:
            original = f.read()
        payload = brotli.compress(original, quality=jsonio.BR_QUALITY)
        # Round-trip verify BEFORE deleting the original.
        if brotli.decompress(payload) != original:
            print(f'  ERROR {label}/{fname}: round-trip mismatch - kept plain', file=sys.stderr)
            continue
        jsonio._atomic_write(payload, dst)
        os.unlink(src)
        saved += size - len(payload)
        count += 1
        print(f'  {label}/{fname}: {human(size)} → {human(len(payload))}')
    return saved, count


def delete_json_chunks(dry_run):
    """Delete legacy JSON tick chunks for every match with full v2 coverage."""
    saved = 0
    kept_matches = []
    for fname in sorted(os.listdir(paths.PROCESSED_DIR)):
        stem = None
        if fname.endswith('.json.br'):
            stem = fname[:-len('.json.br')]
        elif fname.endswith('.json') and not fname.endswith('.overwatch.json'):
            stem = fname[:-len('.json')]
        if not stem or stem.endswith('.overwatch'):
            continue

        chunks_dir = os.path.join(paths.CHUNKS_DIR, stem)
        json_chunks = ([f for f in os.listdir(chunks_dir)
                        if f.startswith('ticks_chunk_') and f.endswith('.json')]
                       if os.path.isdir(chunks_dir) else [])
        if not json_chunks:
            continue

        try:
            master = jsonio.load(os.path.join(paths.PROCESSED_DIR, f'{stem}.json'))
        except Exception as e:
            print(f'  {stem}: cannot read master JSON ({e}) - JSON chunks KEPT', file=sys.stderr)
            kept_matches.append(stem)
            continue

        v2 = master.get('chunkIndexV2') or []
        missing = [e['file'] for e in v2
                   if not os.path.isfile(os.path.join(chunks_dir, os.path.basename(e['file'])))]
        if not v2 or missing:
            why = 'no chunkIndexV2' if not v2 else f'{len(missing)} v2 chunk file(s) missing'
            print(f'  {stem}: {why} - JSON chunks KEPT (unregenerable without the .dem)',
                  file=sys.stderr)
            kept_matches.append(stem)
            continue

        size = sum(os.path.getsize(os.path.join(chunks_dir, f)) for f in json_chunks)
        saved += size
        if dry_run:
            print(f'  {stem}: would delete {len(json_chunks)} JSON chunks ({human(size)})')
        else:
            for f in json_chunks:
                os.unlink(os.path.join(chunks_dir, f))
            print(f'  {stem}: deleted {len(json_chunks)} JSON chunks ({human(size)})')
    return saved, kept_matches


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dry-run', action='store_true', help='report only, change nothing')
    ap.add_argument('--keep-json-chunks', action='store_true',
                    help='skip step 3 (JSON tick chunk deletion)')
    args = ap.parse_args()

    total = 0
    print(f'Data dir: {paths.DATA_DIR}{"  (DRY RUN)" if args.dry_run else ""}\n')

    print('1) processed_matches → .json.br')
    s, n = compress_tree(paths.PROCESSED_DIR, 'processed_matches', args.dry_run,
                         skip=lambda f: f.endswith('.overwatch.json'))
    total += s
    print(f'   {n} file(s), saved {human(s)}\n')

    print('2) overwatch_raw → .json.br')
    s, n = compress_tree(paths.OVERWATCH_RAW_DIR, 'overwatch_raw', args.dry_run)
    total += s
    print(f'   {n} file(s), saved {human(s)}\n')

    if args.keep_json_chunks:
        print('3) JSON tick chunks: skipped (--keep-json-chunks)')
    else:
        print('3) legacy JSON tick chunks (v2 coverage verified per match)')
        s, kept = delete_json_chunks(args.dry_run)
        total += s
        print(f'   freed {human(s)}' + (f' - {len(kept)} match(es) KEPT: {", ".join(kept)}'
                                        if kept else ''))

    print(f'\nTotal {"reclaimable" if args.dry_run else "reclaimed"}: {human(total)}')


if __name__ == '__main__':
    main()
