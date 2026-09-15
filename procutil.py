"""procutil.py - flags shared by every helper-process launch.

The frozen desktop build runs with no console. On Windows every console child
it starts - the Go parser and extractor, powershell, tasklist, taskkill,
ffmpeg - then opens a console window of its own for as long as it runs;
`capture_output=True` does not prevent that, only CREATE_NO_WINDOW does.

Spread `**NO_WINDOW` into those calls. Never into the CS2 or HLAE launches:
those are windows the user is meant to see. Empty off Windows.
"""
import subprocess

NO_WINDOW = ({'creationflags': subprocess.CREATE_NO_WINDOW}
             if hasattr(subprocess, 'CREATE_NO_WINDOW') else {})
