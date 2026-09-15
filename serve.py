"""Production entrypoint.

Runs the app under waitress instead of the Werkzeug dev server used by
`app.py`'s own __main__ block / run.bat's old `flask --debug` invocation -
no auto-reloader, no interactive debugger. Used by the one-click launchers
(run.bat/run.sh). The desktop window is desktop.py; this stays the browser
path for development, LAN use and tools/linuxtest.

    python serve.py           serve on 127.0.0.1:8000
    python serve.py --open    ... and open it in the default browser
"""
import os
import sys
import webbrowser

from waitress import create_server
from app import app, DATA_DIR

if __name__ == '__main__':
    host = os.environ.get('CS2VIEWER_HOST', '127.0.0.1')
    port = int(os.environ.get('CS2VIEWER_PORT', '8000'))
    # create_server binds immediately, so --open can never beat the listener.
    server = create_server(app, host=host, port=port)
    print(f'CS2 Demo Viewer - data dir: {DATA_DIR}')
    print(f'Serving on http://{host}:{port}  (Ctrl+C to stop)')
    sys.stdout.flush()
    if '--open' in sys.argv[1:]:
        browse_host = '127.0.0.1' if host in ('0.0.0.0', '::', '') else host
        webbrowser.open(f'http://{browse_host}:{port}/')
    server.run()
