"""Unit tests for the demo-upload progress the dashboard's bar is drawn from.

Covers app.py's stage weighting, the monotonic guard on a job's fraction, the
ETA, and that /process/status never hands the client a job's private
bookkeeping. The parser half of the protocol (the PROGRESS lines themselves)
belongs to cmd/parser and is exercised by a real parse.

Run:  python3 test/parse_progress_test.py

app.py needs flask, which the rest of the Python suite does not, so an
interpreter without it makes this file report a skip rather than a failure.

The data dir is redirected to a temp directory before app is imported, so the
run never touches a real checkout's data/.
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

_TMP_DATA = tempfile.mkdtemp(prefix="cs2viewer_progress_test_")
os.environ["CS2VIEWER_DATA_DIR"] = _TMP_DATA

try:
    import app  # noqa: E402
except ModuleNotFoundError as exc:   # pragma: no cover - environment, not logic
    # The rest of the Python suite imports nothing outside the stdlib, so this
    # file must not be the one that makes `node test/run_all.mjs` need a venv.
    # It reports a skip and passes; a checkout that can run the app runs it.
    print(f"skipped: {exc.name} is not installed (app.py needs flask)")
    print("0 passed, 0 failed")
    sys.exit(0)

_passed = 0
_failed = 0


def ok(cond, msg):
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print("  x FAIL:", msg)


def eq(a, b, msg):
    ok(a == b, f"{msg} (got {a!r}, want {b!r})")


def close(a, b, msg, tol=1e-9):
    ok(a is not None and abs(a - b) <= tol, f"{msg} (got {a!r}, want ~{b!r})")


def _job(job_id="j", **over):
    with app._JOBS_LOCK:
        app._JOBS[job_id] = {"state": "processing", "match": None, "error": None,
                             "stage": "parse", "progress": 0.0, "eta": None,
                             "_started": app.time.monotonic(), **over}
    return job_id


# ── the weighting ─────────────────────────────────────────────────────────────

def test_weights_cover_the_whole_bar():
    total = sum(w for _, w in app._PARSE_STAGE_WEIGHTS)
    close(total, 1.0, "the stage weights sum to one bar", tol=1e-6)
    ok(all(w > 0 for _, w in app._PARSE_STAGE_WEIGHTS), "no stage is worth nothing")


def test_overall_progress_is_ordered_and_bounded():
    stages = [name for name, _ in app._PARSE_STAGE_WEIGHTS]
    eq(app._overall_progress(stages[0], 0.0), 0.0, "the first stage starts the bar at zero")
    close(app._overall_progress(stages[-1], 1.0), 1.0, "the last stage ends it at one", tol=1e-6)
    starts = [app._overall_progress(s, 0.0) for s in stages]
    ok(starts == sorted(starts), "each stage starts where the previous one ended")
    for s in stages:
        ok(0.0 <= app._overall_progress(s, 5.0) <= 1.0, f"{s} clamps a fraction above 1")
        ok(0.0 <= app._overall_progress(s, -5.0) <= 1.0, f"{s} clamps a negative fraction")


def test_an_unknown_stage_is_not_progress():
    ok(app._overall_progress("teleport", 0.5) is None,
       "a stage this table has not learned yields None rather than a jump to 0")


# ── the job store ─────────────────────────────────────────────────────────────

def test_progress_never_walks_backwards():
    jid = _job("mono")
    app._set_job_progress(jid, "chunks", 1.0)
    high = app._JOBS[jid]["progress"]
    app._set_job_progress(jid, "parse", 0.1)
    eq(app._JOBS[jid]["progress"], high,
       "a lower fraction leaves the bar where it was - going back reads as a restart")
    eq(app._JOBS[jid]["stage"], "parse", "the stage still follows the parser")


def test_an_unknown_stage_changes_nothing():
    jid = _job("unknown-stage")
    app._set_job_progress(jid, "chunks", 0.5)
    before = dict(app._JOBS[jid])
    app._set_job_progress(jid, "teleport", 0.9)
    eq(app._JOBS[jid]["progress"], before["progress"], "an unknown stage moves no bar")
    eq(app._JOBS[jid]["stage"], before["stage"], "an unknown stage renames no stage")


def test_a_missing_job_is_not_an_error():
    app._set_job_progress("no-such-job", "parse", 0.5)   # must not raise
    ok("no-such-job" not in app._JOBS, "a finished or deleted job is not resurrected")


def test_no_eta_before_there_is_enough_run_to_extrapolate_from():
    jid = _job("early")
    app._set_job_progress(jid, "parse", 0.0)
    ok(app._JOBS[jid]["eta"] is None,
       "0% gives no ETA rather than an infinite one")
    ok(app._ETA_MIN_FRACTION > 0, "the floor exists")


def test_eta_is_extrapolated_from_elapsed_time():
    jid = _job("eta")
    # Half a bar in ten seconds -> about ten left, before smoothing.
    with app._JOBS_LOCK:
        app._JOBS[jid]["_started"] = app.time.monotonic() - 10.0
    stage, _ = app._PARSE_STAGE_WEIGHTS[0]
    app._set_job_progress(jid, stage, 1.0)
    overall = app._overall_progress(stage, 1.0)
    eta = app._JOBS[jid]["eta"]
    close(eta, 10.0 * (1 - overall) / overall, "the first ETA is the plain extrapolation", tol=0.5)
    ok(eta > 0, "an unfinished job has time left")


def test_eta_is_smoothed_rather_than_jumping():
    jid = _job("smooth")
    with app._JOBS_LOCK:
        app._JOBS[jid]["_started"] = app.time.monotonic() - 10.0
        app._JOBS[jid]["eta"] = 100.0
    app._set_job_progress(jid, "chunks", 0.5)
    eta = app._JOBS[jid]["eta"]
    ok(eta < 100.0, "a new reading moves the ETA")
    ok(eta > 50.0, "one reading does not replace the ETA outright")


# ── the wire shape ────────────────────────────────────────────────────────────

def test_status_reports_the_progress_keys():
    jid = _job("wire")
    app._set_job_progress(jid, "chunks", 0.5)
    with app.app.test_client() as c:
        body = c.get(f"/process/status/{jid}").get_json()
    ok(body["ok"], "a known job is ok")
    for key in ("state", "match", "error", "stage", "progress", "eta"):
        ok(key in body, f"the client is given {key}")
    eq(body["stage"], "chunks", "the stage is the one the parser last reported")


def test_status_never_leaks_private_bookkeeping():
    jid = _job("private")
    with app.app.test_client() as c:
        body = c.get(f"/process/status/{jid}").get_json()
    ok(not any(k.startswith("_") for k in body),
       "_started is the job's own clock and is not part of the wire shape")


def test_unknown_job_is_404():
    with app.app.test_client() as c:
        resp = c.get("/process/status/not-a-job")
    eq(resp.status_code, 404, "an unknown job id is a 404, not an empty bar")


for _fn in list(globals().values()):
    if callable(_fn) and getattr(_fn, "__name__", "").startswith("test_"):
        _fn()

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
