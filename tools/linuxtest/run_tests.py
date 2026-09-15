#!/usr/bin/env python3
"""run_tests.py - the Linux acceptance run, executed inside the container.

Driven by tools/linuxtest/linux_test.sh; see that file for the mounts. This is
the half that decides what "the app works on Linux" means and reports it as a
table of PASS / FAIL / SKIP.

Why SKIP is a first-class result here
-------------------------------------
Three of the things worth testing cannot fully run on Linux, and the useful
answer is not to omit them:

  clips        HLAE is a Windows-only injector, so recording is impossible by
               construction. What IS testable - and what actually matters - is
               that the app degrades gracefully: readiness reports false with a
               reason, and /clip/record answers with a clean error instead of a
               500. That is asserted, not skipped.
  watch mode   Launching CS2 needs a GPU, a display and Steam, none of which a
               container has. But the hard part of that feature is the netcon
               console protocol, and that runs against a stand-in console here.
  UI assets    Needs the real game files; runs for real when the driver finds a
               CS2 install to mount, and skips with a reason when it doesn't.

A SKIP never fails the run; a FAIL always does. Anything that reports SKIP says
why, so a run that quietly tested less than you thought is visible.

Nothing here writes to the mounted source: /src is read-only and the first step
stages a writable copy into /app.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

SRC = '/src'                 # repo, read-only
APP = '/app'                 # writable staged copy
SEED_DATA = '/seed/data'     # processed_matches + chunks, read-only
SEED_ASSETS = '/seed/assets'  # static/assets, read-only (optional)
CSGO = '/csgo'               # CS2 game/csgo dir, read-only (optional)

results = []
_t0 = time.time()


def record(status, name, detail=''):
    """Log one result. A failing command's output is kept in full for the body
    of the run but collapsed to one line in the summary - a multi-line tail
    printed into the summary table makes every later row unreadable."""
    results.append((status, name, detail))
    colour = {'PASS': '\033[32m', 'FAIL': '\033[31m', 'SKIP': '\033[33m',
              'INFO': '\033[36m'}.get(status, '')
    print(f'{colour}[{status:4}]\033[0m {name}', flush=True)
    if detail:
        for line in str(detail).strip().splitlines():
            print(f'         {line}', flush=True)


def oneline(detail, limit=150):
    s = ' '.join(str(detail or '').split())
    return s[:limit] + ('…' if len(s) > limit else '')


def run(cmd, cwd=APP, timeout=900, env=None, check_output=True):
    """Run a command, returning (rc, combined output)."""
    e = dict(os.environ)
    if env:
        e.update(env)
    try:
        p = subprocess.run(cmd, cwd=cwd, env=e, timeout=timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, encoding='utf-8', errors='replace')
        return p.returncode, (p.stdout or '')
    except subprocess.TimeoutExpired:
        return 124, f'timed out after {timeout}s'
    except OSError as e2:
        return 127, str(e2)


def last_line(out):
    lines = [l for l in (out or '').strip().splitlines() if l.strip()]
    return lines[-1] if lines else '(no output)'


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


# ── 0. stage a writable copy ────────────────────────────────────────────────
def stage():
    if not os.path.isdir(SRC):
        record('FAIL', 'stage', f'{SRC} not mounted')
        return False
    # Copy the source tree, minus the things that are either huge or host-shaped.
    ignore = shutil.ignore_patterns('.git', 'data', '.venv', '.spike-venv',
                                    'node_modules', '__pycache__', '*.pyc',
                                    'bin', 'notebooks')
    if os.path.isdir(APP):
        shutil.rmtree(APP, ignore_errors=True)
    shutil.copytree(SRC, APP, ignore=ignore)

    # Data: only what the app needs to serve matches. demos/ is gigabytes and
    # is only needed by the optional parse test, which mounts one file instead.
    staged = 0
    for sub in ('processed_matches', 'chunks'):
        s = os.path.join(SEED_DATA, sub)
        if os.path.isdir(s):
            shutil.copytree(s, os.path.join(APP, 'data', sub), dirs_exist_ok=True)
            staged += sum(len(f) for _, _, f in os.walk(s))
    dm = os.path.join(SEED_DATA, 'demo_map.json')
    if os.path.isfile(dm):
        os.makedirs(os.path.join(APP, 'data'), exist_ok=True)
        shutil.copy(dm, os.path.join(APP, 'data', 'demo_map.json'))

    # Radar PNGs ride along inside the assets seed (static/assets/overheadmaps/).
    if os.path.isdir(SEED_ASSETS):
        shutil.copytree(SEED_ASSETS, os.path.join(APP, 'static', 'assets'), dirs_exist_ok=True)

    # Shell scripts checked out on Windows can carry CRLF; the container runs
    # them with bash, where that is a hard error. Report rather than silently
    # fix, because it is a real portability finding about the checkout.
    crlf = []
    for dp, _d, files in os.walk(os.path.join(APP, 'tools')):
        for f in files:
            if f.endswith('.sh'):
                p = os.path.join(dp, f)
                with open(p, 'rb') as fh:
                    if b'\r\n' in fh.read():
                        crlf.append(os.path.relpath(p, APP))
    if crlf:
        record('INFO', 'stage: CRLF shell scripts normalised',
               ', '.join(crlf) + ' (LF in git; only this working copy differs)')
        for rel in crlf:
            p = os.path.join(APP, rel)
            with open(p, 'rb') as fh:
                data = fh.read().replace(b'\r\n', b'\n')
            with open(p, 'wb') as fh:
                fh.write(data)

    radars = os.path.join(APP, 'static', 'assets', 'overheadmaps')
    record('PASS', 'stage source + data', f'{staged} data files, radar images '
           f'{"yes" if os.path.isdir(radars) else "MISSING"}')
    return True


def first_match():
    d = os.path.join(APP, 'data', 'processed_matches')
    try:
        names = sorted(f for f in os.listdir(d)
                       if f.endswith(('.json', '.json.br'))
                       and not any(f.endswith(c + e)
                                   for c in ('.overwatch', '.utility', '.moments', '.slim')
                                   for e in ('.json', '.json.br')))
    except OSError:
        return None
    if not names:
        return None
    n = names[0]
    return n[:-3] if n.endswith('.br') else n


# ── 1. environment ──────────────────────────────────────────────────────────
def test_env():
    rc, out = run(['python3', '--version'])
    py = out.strip()
    rc, out = run(['node', '--version'])
    node = out.strip()
    rc, out = run(['go', 'version'])
    go = out.strip() if rc == 0 else 'not installed'
    with open('/etc/os-release') as fh:
        osname = [l for l in fh if l.startswith('PRETTY_NAME')][0].split('=')[1].strip().strip('"')
    record('INFO', 'environment', f'{osname} · {py} · node {node} · {go}')


# ── 2. the project's own unit suites ────────────────────────────────────────
def test_unit_suites():
    rc, out = run(['node', 'test/run_all.mjs'], timeout=1800)
    record('PASS' if rc == 0 else 'FAIL', 'unit suites (node test/run_all.mjs)',
           last_line(out) if rc == 0 else out[-1500:])


# ── 3. the Go parser ────────────────────────────────────────────────────────
def test_go():
    if shutil.which('go') is None:
        record('SKIP', 'go build + go test', 'no Go toolchain in this image (WITH_GO=0)')
        return
    env = {'GOCACHE': '/tmp/gocache', 'GOPATH': '/tmp/gopath', 'GOFLAGS': '-mod=mod'}
    rc, out = run(['go', 'build', './...'], env=env, timeout=1800)
    if rc != 0:
        record('FAIL', 'go build ./...', out[-1500:])
        return
    record('PASS', 'go build ./...', 'parser + overwatch compile on linux/amd64')
    rc, out = run(['go', 'test', './cmd/...'], env=env, timeout=1800)
    record('PASS' if rc == 0 else 'FAIL', 'go test ./cmd/...',
           last_line(out) if rc == 0 else out[-1500:])


# ── 4. the app boots and every route answers ────────────────────────────────
class Server:
    """serve.py (waitress - the real production entrypoint, not Flask's dev
    server) on a free port, so the routes are exercised the way they ship."""

    def __init__(self):
        self.port = free_port()
        self.proc = None

    def __enter__(self):
        env = {'CS2VIEWER_PORT': str(self.port), 'CS2VIEWER_HOST': '127.0.0.1',
               'PYTHONUNBUFFERED': '1'}
        self.proc = subprocess.Popen(
            ['python3', 'serve.py'], cwd=APP, env={**os.environ, **env},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding='utf-8', errors='replace')
        self.log = []
        threading.Thread(target=self._drain, daemon=True).start()
        deadline = time.time() + 60
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError('server exited: ' + ''.join(self.log[-20:]))
            try:
                urllib.request.urlopen(self.url('/'), timeout=3)
                return self
            except urllib.error.HTTPError:
                return self
            except OSError:
                time.sleep(0.3)
        raise RuntimeError('server did not come up: ' + ''.join(self.log[-20:]))

    def _drain(self):
        for line in self.proc.stdout:
            self.log.append(line)

    def __exit__(self, *a):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def url(self, path):
        return f'http://127.0.0.1:{self.port}{path}'

    def get(self, path, timeout=120):
        try:
            with urllib.request.urlopen(self.url(path), timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def post(self, path, payload=None, timeout=120):
        data = json.dumps(payload or {}).encode()
        req = urllib.request.Request(self.url(path), data=data,
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()


def test_routes(server, match):
    paths = [('dashboard', '/'), ('players', '/players'), ('settings', '/settings'),
             ('overwatch dashboard', '/overwatch/dashboard'),
             ('analyser', '/multi?scope=all'),
             ('analyser fields', '/analyser/fields')]
    if match:
        paths += [('match page', f'/match?match={match}'),
                  ('viewer', f'/viewer?match={match}'),
                  ('multi (match)', f'/multi?match={match}'),
                  ('match json', f'/data/{match}'),
                  ('slim json', f'/data/slim/{match}')]
    bad = []
    for name, p in paths:
        code, body = server.get(p)
        if code != 200 or not body:
            bad.append(f'{name} {p} -> {code}')
    record('PASS' if not bad else 'FAIL', f'route sweep ({len(paths)} routes)',
           'all 200' if not bad else '; '.join(bad))

    # The radar PNG route and a tick chunk: the two things the 2D viewer cannot
    # draw without, and both are served by custom routes rather than Flask's
    # static handler.
    if match:
        try:
            meta = json.loads(server.get(f'/data/slim/{match}')[1])
            mapname = meta.get('mapName') or ''
        except (ValueError, KeyError):
            mapname = ''
        if mapname:
            code, body = server.get(f'/radar/{mapname}.png')
            record('PASS' if code == 200 and len(body) > 1000 else 'FAIL',
                   'radar PNG route', f'/radar/{mapname}.png -> {code}, {len(body)} bytes')
        stem = match[:-5]
        code, body = server.get(f'/chunks/{stem}/ticks_chunk_000.bin.br')
        record('PASS' if code == 200 and body else 'FAIL', 'tick chunk route',
               f'{code}, {len(body)} bytes')

    # A real query through the moment engine - the analyser's actual work.
    # Two things about the spec shape, both easy to get wrong: the moment type
    # is chosen with `unit`, NOT a `where` condition (there is no `kind` field),
    # and a leaf is {f, op, v}. See analysis/query.py's FIELDS/_test_leaf and
    # the shipped PRESETS, which double as grammar documentation.
    code, body = server.post('/analyser/query',
                             {'unit': 'kill',
                              'where': {'all': [{'f': 'hs', 'op': 'is', 'v': True}]}})
    try:
        d = json.loads(body)
        n = len(d.get('rows') or [])
        scanned = d.get('scanned') or 0
        # A 200 with an empty corpus would pass vacuously, so require that the
        # index was actually built and that the filter selected a subset.
        ok = code == 200 and 'aggregates' in d and scanned > 0 and n > 0
    except ValueError:
        ok, n, scanned = False, 0, 0
    record('PASS' if ok else 'FAIL', 'analyser query (headshot kills)',
           f'HTTP {code} · {n} rows from {scanned} moments scanned'
           + ('' if ok else f' · {oneline(body)}'))


# ── 5. real-browser page smoke ──────────────────────────────────────────────
def test_browser():
    try:
        import playwright  # noqa: F401
    except ImportError:
        record('SKIP', 'browser page smoke', 'Playwright not in this image (WITH_BROWSER=0)')
        return
    rc, out = run(['python3', 'tools/smoke_pages.py'], timeout=1800)
    record('PASS' if rc == 0 else 'FAIL', 'browser page smoke (950px + 1920px)',
           last_line(out) if rc == 0 else out[-2000:])


# ── 6. UI asset extraction from the real VPK ────────────────────────────────
def test_asset_extraction():
    vpk = os.path.join(CSGO, 'pak01_dir.vpk')
    if not os.path.isfile(vpk):
        record('SKIP', 'UI asset extraction', 'no CS2 install mounted at /csgo '
               '(driver could not find data/analysis_data/cs2_game_dir)')
        return
    cli = os.environ.get('SOURCE2VIEWER_CLI', '/opt/s2v/Source2Viewer-CLI')
    if not os.path.isfile(cli):
        record('FAIL', 'UI asset extraction', f'Source2Viewer-CLI missing at {cli}')
        return

    # Point the app's own settings at the mounted install and the Linux CLI,
    # then run the real extractor script - not a reimplementation of it.
    ad = os.path.join(APP, 'data', 'analysis_data')
    os.makedirs(ad, exist_ok=True)
    # extract_ui_assets.sh looks for <dir>/game/csgo/pak01_dir.vpk, so hand it
    # the grandparent of the mounted csgo dir.
    with open(os.path.join(ad, 'cs2_game_dir'), 'w') as fh:
        fh.write('/cs2root')
    with open(os.path.join(ad, 'source2viewer_cli'), 'w') as fh:
        fh.write(cli)
    os.makedirs('/cs2root/game', exist_ok=True)
    if not os.path.exists('/cs2root/game/csgo'):
        os.symlink(CSGO, '/cs2root/game/csgo')

    # Wipe any staged manifest so a pass can't be inherited from the host copy.
    shutil.rmtree(os.path.join(APP, 'static', 'assets'), ignore_errors=True)

    rc, out = run(['bash', 'tools/extract_ui_assets.sh'], timeout=3600)
    manifest = os.path.join(APP, 'static', 'assets', 'manifest.json')
    if rc != 0 or not os.path.isfile(manifest):
        record('FAIL', 'UI asset extraction (real VPK)', out[-2000:])
        return
    with open(manifest) as fh:
        kinds = json.load(fh)['kinds']
    counts = ', '.join(f'{k} {len(v)}' for k, v in kinds.items())
    total = sum(len(v) for v in kinds.values())
    record('PASS' if total > 0 else 'FAIL', 'UI asset extraction (real VPK)',
           f'{total} assets · {counts}')


# ── 7. radar calibration extractor ──────────────────────────────────────────
def test_map_calibration():
    if not os.path.isfile(os.path.join(CSGO, 'pak01_dir.vpk')):
        record('SKIP', 'map calibration diff', 'no CS2 install mounted')
        return
    rc, out = run(['node', 'tools/extract_map_calibration.mjs'], timeout=900,
                  env={'CS2_PAK_VPK': os.path.join(CSGO, 'pak01_dir.vpk')})
    # Exit 0 means every MAP_LIBRARY / LAYERS entry matches Valve's own files.
    record('PASS' if rc == 0 else 'FAIL', 'map calibration diff vs VPK',
           last_line(out))


# ── 8. manual asset import ──────────────────────────────────────────────────
def test_manual_import(server):
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('assets/equipment/ak47.svg', b'<svg/>')
        zf.writestr('assets/map_icons/map_icon_de_dust2.svg', b'<svg/>')
        zf.writestr('assets/skillgroups/skillgroup3.svg', b'<svg/>')
        zf.writestr('assets/../../escape.svg', b'<svg/>')   # must not escape
    body, ctype = _multipart({'kind': 'auto'},
                             [('files', 'assets.zip', buf.getvalue())])
    req = urllib.request.Request(server.url('/settings/import-assets'), data=body,
                                 headers={'Content-Type': ctype})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        record('FAIL', 'manual asset import', f'HTTP {e.code}: {e.read()[:300]}')
        return
    escaped = os.path.exists(os.path.join(APP, 'escape.svg')) or os.path.exists('/escape.svg')
    ok = d.get('ok') and d.get('total', 0) >= 3 and d.get('manifest_ok') and not escaped
    record('PASS' if ok else 'FAIL', 'manual asset import (+ zip-slip guard)',
           f"imported {d.get('imported')}, manifest_ok={d.get('manifest_ok')}, "
           f"escaped={escaped}")


def _multipart(fields, files):
    boundary = '----cs2viewerlinuxtest'
    out = b''
    for k, v in fields.items():
        out += (f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n'
                f'{v}\r\n').encode()
    for name, filename, data in files:
        out += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'
                ).encode() + data + b'\r\n'
    out += f'--{boundary}--\r\n'.encode()
    return out, f'multipart/form-data; boundary={boundary}'


# ── 9. clips - cannot record, must degrade cleanly ──────────────────────────
def test_clips(server, match):
    sys.path.insert(0, APP)
    for mod in ('clip_record', 'paths'):
        sys.modules.pop(mod, None)
    os.environ['CS2VIEWER_DATA_DIR'] = os.path.join(APP, 'data')
    import clip_record

    try:
        ready = clip_record.settings_ready()
    except Exception as e:                                   # noqa: BLE001
        record('FAIL', 'clip readiness degrades gracefully', f'raised {e!r}')
        return
    if ready:
        record('FAIL', 'clip readiness degrades gracefully',
               'reports READY on Linux, where HLAE cannot run')
    else:
        record('PASS', 'clip readiness degrades gracefully',
               'settings_ready() is False (HLAE is Windows-only) and did not raise')

    # The cfg builders are pure and must work anywhere - they are what a future
    # Linux/Proton recording path would reuse.
    try:
        lines = clip_record.build_seek_cfg_lines(1000, 2000, 'someone', 'clip', '/tmp/frames')
        clip_record.assert_offline_only(lines)
        record('PASS', 'clip cfg builders + offline assertion',
               f'{len(lines)} cfg lines, no online commands')
    except Exception as e:                                   # noqa: BLE001
        record('FAIL', 'clip cfg builders + offline assertion', repr(e))

    # And the route must refuse politely rather than 500.
    if match:
        code, body = server.post('/clip/record',
                                 {'match': match, 'start_tick': 1000, 'end_tick': 1200,
                                  'focus_player': 'nobody'})
        try:
            d = json.loads(body)
            clean = (not d.get('ok')) and bool(d.get('error'))
        except ValueError:
            clean = False
        record('PASS' if clean and code < 500 else 'FAIL', '/clip/record refuses cleanly',
               f'HTTP {code}, {str(body[:160])}')


# ── 10. watch mode - cannot launch CS2, but the protocol is testable ────────
def test_watch(server, match):
    sys.path.insert(0, APP)
    import clip_record

    try:
        ready = clip_record.watch_ready()
        record('PASS' if not ready else 'FAIL', 'watch readiness degrades gracefully',
               f'watch_ready() -> {ready} (no runnable CS2 in a container)')
    except Exception as e:                                   # noqa: BLE001
        record('FAIL', 'watch readiness degrades gracefully', f'raised {e!r}')

    if match:
        code, body = server.post('/demo/watch',
                                 {'match': match, 'tick': 10000, 'focus_player': 'nobody'})
        try:
            d = json.loads(body)
            clean = (not d.get('ok')) and bool(d.get('error'))
        except ValueError:
            clean = False
        record('PASS' if clean and code < 500 else 'FAIL', '/demo/watch refuses cleanly',
               f'HTTP {code}, {str(body[:160])}')

    # The real functional test: drive netcon_jump against a stand-in console
    # that speaks what CS2 speaks. This is the part of "go in-game" that is not
    # about having a GPU, and it exercises the socket path on Linux for real.
    _netcon_roundtrip()


def _netcon_roundtrip():
    import clip_record
    received = []
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('127.0.0.1', 0))
    port = srv.getsockname()[1]
    srv.listen(1)

    def fake_cs2():
        conn, _ = srv.accept()
        conn.sendall(b'Console initialized.\n' * 50)     # chatter nobody reads
        buf = b''
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                conn.settimeout(5)
                data = conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            buf += data
            for line in buf.split(b'\n')[:-1]:
                received.append(line.decode('utf-8', 'replace').strip())
            buf = buf.split(b'\n')[-1]
            if any('demo_gototick' in r for r in received):
                # What the game prints once a seek actually completes; this is
                # the only thing netcon_jump trusts as proof.
                conn.sendall(b'Demo Skipping finished at tick 19680\n')
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        conn.close()

    t = threading.Thread(target=fake_cs2, daemon=True)
    t.start()
    try:
        clip_record.netcon_jump(port, 19680, 'SomePlayer')
    except Exception as e:                                   # noqa: BLE001
        record('FAIL', 'netcon jump protocol (stand-in console)', repr(e))
        srv.close()
        return
    t.join(timeout=30)
    srv.close()
    joined = ' | '.join(received)
    got_seek = any('demo_gototick' in r for r in received)
    got_pov = any('spec_player' in r for r in received)
    # The POV re-assert must survive the close - the RST-vs-FIN bug this
    # feature was debugged around (see clip_record's _netcon_close docstring).
    pov_after_seek = False
    seek_idx = next((i for i, r in enumerate(received) if 'demo_gototick' in r), None)
    if seek_idx is not None:
        pov_after_seek = any('spec_player' in r for r in received[seek_idx:])
    ok = got_seek and got_pov and pov_after_seek
    record('PASS' if ok else 'FAIL', 'netcon jump protocol (stand-in console)',
           f'seek={got_seek} pov={got_pov} pov_re-asserted_after_seek={pov_after_seek} :: '
           + joined[:200])


# ── 11. parse a real demo end-to-end (optional) ─────────────────────────────
def test_parse_demo():
    dem = '/seed/demo.dem'
    if not os.path.isfile(dem):
        record('SKIP', 'parse a real demo', 'no demo mounted (driver flag --with-demo)')
        return
    if shutil.which('go') is None:
        record('SKIP', 'parse a real demo', 'no Go toolchain to build the parser')
        return
    env = {'GOCACHE': '/tmp/gocache', 'GOPATH': '/tmp/gopath'}
    rc, out = run(['go', 'build', '-o', '/tmp/parser', './cmd/parser'], env=env, timeout=1800)
    if rc != 0:
        record('FAIL', 'parse a real demo', 'parser build failed: ' + out[-800:])
        return
    # Same argv the app builds in _parse_worker: -out is the master JSON FILE,
    # -chunks the directory for the v2 tick chunks. Mirroring it exactly is the
    # point - a parse that only works when invoked differently proves nothing.
    outdir = '/tmp/parseout'
    chunks = os.path.join(outdir, 'chunks')
    os.makedirs(chunks, exist_ok=True)
    master = os.path.join(outdir, 'match.json')
    rc, out = run(['/tmp/parser', '-demo', dem, '-out', master, '-chunks', chunks,
                   '-chunks-v2', '-no-json-chunks'], timeout=3600)
    if rc != 0 or not os.path.isfile(master):
        record('FAIL', 'parse a real demo', out[-1200:])
        return
    with open(master, encoding='utf-8') as fh:
        data = json.load(fh)
    nchunks = len([f for f in os.listdir(chunks) if f.endswith('.bin.br')])
    # Assert the shape the frontend actually depends on, not just "a file
    # appeared": v2 chunk format, a chunk index, and real rounds/kills.
    ok = (data.get('chunkFormat') == 2 and data.get('chunkIndexV2')
          and len(data.get('rounds') or []) > 0 and len(data.get('kills') or []) > 0
          and nchunks > 0)
    record('PASS' if ok else 'FAIL', 'parse a real demo (end to end)',
           f"map {data.get('mapName')} · {len(data.get('rounds') or [])} rounds · "
           f"{len(data.get('kills') or [])} kills · chunkFormat "
           f"{data.get('chunkFormat')} · {nchunks} v2 chunk(s)")


# ── main ────────────────────────────────────────────────────────────────────
def main():
    print('=' * 78)
    print(' CS2 Demo Viewer - Linux acceptance run')
    print('=' * 78, flush=True)

    if not stage():
        return 1
    test_env()
    match = first_match()
    record('INFO', 'corpus', f'using match {match}' if match
           else 'no matches staged - match-scoped checks will be skipped')

    test_unit_suites()
    test_go()
    test_asset_extraction()
    test_map_calibration()

    try:
        with Server() as server:
            record('PASS', 'app boots under waitress', f'serve.py on :{server.port}')
            test_routes(server, match)
            test_manual_import(server)
            test_clips(server, match)
            test_watch(server, match)
    except Exception as e:                                   # noqa: BLE001
        record('FAIL', 'app boots under waitress', repr(e))

    test_browser()
    test_parse_demo()

    print('\n' + '=' * 78)
    npass = sum(1 for s, _, _ in results if s == 'PASS')
    nfail = sum(1 for s, _, _ in results if s == 'FAIL')
    nskip = sum(1 for s, _, _ in results if s == 'SKIP')
    for s, n, d in results:
        if s in ('FAIL', 'SKIP'):
            print(f'  {s}: {n} - {oneline(d)}')
    print(f'\n{npass} passed · {nfail} failed · {nskip} skipped '
          f'· {time.time() - _t0:.0f}s')
    print('=' * 78, flush=True)
    return 1 if nfail else 0


if __name__ == '__main__':
    sys.exit(main())
