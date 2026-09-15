"""desktop.py - the desktop-app entrypoint: the same Flask app serve.py runs, in
a native window (pywebview → WebView2 on Windows) instead of a browser tab.

serve.py stays the browser path (development, LAN, tools/linuxtest); this is
what the frozen build (desktop.spec) runs, and `python desktop.py` runs it from
a checkout too. The order below is load-bearing:

  1. Logging first. A --noconsole build has sys.stdout/stderr = None: print()
     silently no-ops, .flush() raises, and a traceback goes nowhere. Everything
     lands in DATA_DIR/logs/app.log instead, clip_record's netcon diagnostics
     included.
  2. One instance per data dir, by an OS file lock. Two servers would fight over
     clip_record's _WATCH_SESSION, the in-process job dicts and demo_map.json.
     A second launch just shows the running instance.
  3. Bind the socket BEFORE the window exists. WebView2 renders its own error
     page for a refused connection and never retries - a race here is a
     permanently blank app.
  4. The window owns the main thread (webview.start blocks; Cocoa/GTK require
     it). waitress runs on a daemon thread and dies with the window.

If no window can be made - no WebView2 runtime, or no GUI toolkit on Linux - the
app opens the default browser and keeps serving, and says how to stop it.
"""
import logging
import logging.handlers
import os
import socket
import sys
import threading
import webbrowser

import paths

TITLE = 'CS2 Demo Viewer'

# A FIXED port, not an ephemeral one: localStorage is per origin and the origin
# includes the port, so a new port per launch would forget the HUD scale and
# every other per-viewer preference each run. 8765 rather than serve.py's 8000
# so the desktop app and a dev server can run side by side.
DEFAULT_PORT = 8765

LOCK_PATH = os.path.join(paths.DATA_DIR, 'desktop.lock')
PORT_PATH = os.path.join(paths.DATA_DIR, 'desktop.port')

log = logging.getLogger('cs2viewer.desktop')


# ── 1. logging ────────────────────────────────────────────────────────────────

class _LogStream:
    """File-like sink that turns print() output into log records, line by line.
    Stands in for a None sys.stdout/sys.stderr in a --noconsole build."""

    def __init__(self, logger, level):
        self._logger, self._level, self._buf = logger, level, ''
        self._lock = threading.Lock()

    def write(self, s):
        with self._lock:
            self._buf += s
            while '\n' in self._buf:
                line, self._buf = self._buf.split('\n', 1)
                if line.strip():
                    self._logger.log(self._level, line)
        return len(s)

    def flush(self):
        with self._lock:
            if self._buf.strip():
                self._logger.log(self._level, self._buf)
            self._buf = ''

    def isatty(self):
        return False


def setup_logging():
    os.makedirs(paths.LOGS_DIR, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        os.path.join(paths.LOGS_DIR, 'app.log'), maxBytes=2_000_000, backupCount=3,
        encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    if sys.stderr is not None:          # a terminal run also shows it live
        root.addHandler(logging.StreamHandler(sys.stderr))
    if sys.stdout is None:
        sys.stdout = _LogStream(logging.getLogger('stdout'), logging.INFO)
    if sys.stderr is None:
        sys.stderr = _LogStream(logging.getLogger('stderr'), logging.WARNING)


# ── 2. single instance ────────────────────────────────────────────────────────

def acquire_instance_lock(path=LOCK_PATH):
    """Open file with an exclusive OS lock held, or None if another process
    holds it. The OS drops the lock when the holder dies, so a crash never
    leaves a stale lock behind (a PID file would)."""
    f = open(path, 'a+')
    try:
        if os.name == 'nt':
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def read_port(path=PORT_PATH):
    """The running instance's port, or None."""
    try:
        with open(path, encoding='utf-8') as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def write_port(port, path=PORT_PATH):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(str(port))
    os.replace(tmp, path)


# ── 3. server ─────────────────────────────────────────────────────────────────

def bind_socket(port):
    """A listening loopback socket on `port`, or on a free one if that is taken
    (logged: this session's localStorage then lives under another origin)."""
    for p in (port, 0):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name != 'nt':
            # A quick relaunch would otherwise hit TIME_WAIT and land on a new
            # port (new origin, empty localStorage). Not on Windows, where
            # SO_REUSEADDR lets another process take a port already in use.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(('127.0.0.1', p))
        except OSError:
            sock.close()
            log.warning('port %d is in use - falling back to a free port', p)
            continue
        sock.listen(64)
        return sock
    raise OSError('could not bind a loopback port')


def start_server(sock):
    from waitress import serve
    from app import app
    # 8 workers, not waitress's 4: a page load opens up to six connections at
    # once (the webview's per-origin limit), so with 4 every first load queued.
    t = threading.Thread(target=serve, args=(app,),
                         kwargs={'sockets': [sock], 'threads': 8},
                         name='waitress', daemon=True)
    t.start()
    return t


# ── 4. window ─────────────────────────────────────────────────────────────────

def _start_dir(current):
    """Where a picker opens: the folder of whatever the field already holds."""
    current = (current or '').strip().strip('"')
    if os.path.isdir(current):
        return current
    parent = os.path.dirname(current)
    return parent if os.path.isdir(parent) else ''


class DesktopApi:
    """window.pywebview.api in the page - settings.html's Browse… buttons.
    Private attributes are not exposed to JS (pywebview skips leading '_')."""

    def __init__(self):
        self._window = None

    def _pick(self, kind, current, file_types=()):
        import webview
        r = self._window.create_file_dialog(kind, directory=_start_dir(current),
                                            file_types=file_types)
        if not r:
            return None
        return r if isinstance(r, str) else r[0]

    def pick_folder(self, current=''):
        import webview
        return self._pick(webview.FileDialog.FOLDER, current)

    def pick_file(self, current=''):
        import webview
        types = ('Programs (*.exe)', 'All files (*.*)') if os.name == 'nt' else ()
        return self._pick(webview.FileDialog.OPEN, current, types)


def gui_backend_available(platform=sys.platform, find_spec=None):
    """Whether pywebview has a toolkit to draw with. Windows (WebView2) and macOS
    (WKWebView) always do. Linux needs GTK (`gi`) or Qt (`qtpy`) Python bindings,
    which the frozen Linux build does not ship - so it is checked here, and a
    missing toolkit is one log line instead of pywebview's traceback per backend
    it probes. A source run on a desktop that has either still gets a window."""
    if platform.startswith('win') or platform == 'darwin':
        return True
    if find_spec is None:
        import importlib.util
        find_spec = importlib.util.find_spec
    for mod in ('gi', 'qtpy'):
        try:
            if find_spec(mod) is not None:
                return True
        except (ImportError, ValueError):
            pass
    return False


def open_window(url, api=None):
    """Show `url` in a native window; blocks until it closes. Raises when no
    window can be made, for the caller to fall back on."""
    import webview
    webview.settings['ALLOW_DOWNLOADS'] = True   # saving a recorded .mp4
    window = webview.create_window(TITLE, url, js_api=api, width=1440, height=900,
                                   min_size=(640, 520))
    if api is not None:
        api._window = window
    # private_mode defaults to True, which discards localStorage on every exit.
    webview.start(private_mode=False,
                  storage_path=os.path.join(paths.DATA_DIR, 'webview'))


def serve_in_browser(url, server_thread):
    """The no-window fallback: browser tab, and a way to stop that the user can
    see. Windows has no console here, so a message box is the lifetime."""
    webbrowser.open(url)
    if os.name == 'nt':
        import ctypes
        msg = (f'{TITLE} opened in your web browser at {url}\n\n'
               'It could not open its own window - this usually means the '
               'Microsoft Edge WebView2 Runtime is missing.\n\n'
               'The app keeps running until you close this message.')
        MB_ICONINFORMATION, MB_TOPMOST = 0x40, 0x40000
        ctypes.windll.user32.MessageBoxW(None, msg, TITLE, MB_ICONINFORMATION | MB_TOPMOST)
        return
    print(f'\n{TITLE} is running at {url} and has opened in your web browser.\n'
          'Keep this terminal open - closing it or pressing Ctrl+C stops the app.\n',
          flush=True)
    try:
        server_thread.join()
    except KeyboardInterrupt:
        pass


def show_running_instance():
    port = read_port()
    if not port:
        log.error('another instance holds the lock but left no port file')
        return
    url = f'http://127.0.0.1:{port}/'
    log.info('already running at %s - showing it', url)
    if gui_backend_available():
        try:
            open_window(url)
            return
        except Exception:
            log.exception('no native window - using the default browser')
    webbrowser.open(url)


def main():
    os.makedirs(paths.DATA_DIR, exist_ok=True)
    setup_logging()
    log.info('%s starting - data dir %s, frozen=%s', TITLE, paths.DATA_DIR, paths.FROZEN)

    lock = acquire_instance_lock()
    if lock is None:
        show_running_instance()
        return

    try:
        sock = bind_socket(int(os.environ.get('CS2VIEWER_PORT', DEFAULT_PORT)))
        port = sock.getsockname()[1]
        url = f'http://127.0.0.1:{port}/'
        server_thread = start_server(sock)
        write_port(port)
        log.info('serving on %s', url)
        opened = False
        if gui_backend_available():
            try:
                open_window(url, DesktopApi())
                opened = True
            except Exception:
                log.exception('no native window - falling back to the default browser')
        else:
            log.info('no GTK or Qt Python bindings - using the default browser')
        if not opened:
            serve_in_browser(url, server_thread)
    except Exception:
        log.exception('desktop launch failed')
        raise
    finally:
        try:
            os.unlink(PORT_PATH)
        except OSError:
            pass
        lock.close()
        log.info('%s stopped', TITLE)


if __name__ == '__main__':
    main()
