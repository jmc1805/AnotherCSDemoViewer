"""Unit tests for match_summary.py - the server-side computeSummary twin.
Synthetic fixtures exercise the MR12 side swap, OT
half-swaps, first-half team pinning, suicides, and duration/roster rollups.

Run:  python3 test/match_summary_test.py     (no deps, no browser)

Parity with the historical client-side computeSummary() was additionally verified
against the whole corpus (JS reference vs this module) during implementation.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import match_summary  # noqa: E402

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


def _round(n, winner, start=None, end=None):
    r = {"round_num": n, "winner_side": winner}
    if start is not None:
        r["start"] = start
    if end is not None:
        r["end"] = end
    return r


def _kill(tick, atk, atk_side, vic, vic_side, assist=None):
    return {"tick": tick, "attacker_name": atk, "attacker_side": atk_side,
            "victim_name": vic, "victim_side": vic_side, "assister_name": assist}


def test_regulation_swap():
    # Rounds 1-12: CT-starters win 8, T-starters win 4.
    # Rounds 13-16 (swapped): winner_side 'ct' now credits the T-starters.
    rounds = [_round(i, "ct") for i in range(1, 9)] \
        + [_round(i, "t") for i in range(9, 13)] \
        + [_round(13, "ct"), _round(14, "ct"), _round(15, "t"), _round(16, "t")]
    s = match_summary.compute_summary_stats({"rounds": rounds, "kills": []})
    # team_ct (started CT): 8 first-half + (rounds 15,16 't' wins) 2 = 10
    # team_t  (started T):  4 first-half + (rounds 13,14 'ct' wins) 2 = 6
    eq(s["scoreCT"], 10, "regulation swap scoreCT")
    eq(s["scoreT"], 6, "regulation swap scoreT")
    eq(s["rounds"], 16, "round count")


def test_ot_half_swaps():
    # Build a genuine 12:12 into OT: 1-12 split 6/6 (not swapped),
    # 13-24 split 6/6 (swapped) so identities stay balanced.
    rounds = [_round(i, "ct") for i in range(1, 7)] + [_round(i, "t") for i in range(7, 13)]
    rounds += [_round(i, "t") for i in range(13, 19)] + [_round(i, "ct") for i in range(19, 25)]
    # OT rounds 25-27 are SWAPPED (matches the JS: floor((25-25)/3)%2==0 => true),
    # so 'ct' wins credit the T-starters (teamT).
    rounds += [_round(25, "ct"), _round(26, "ct"), _round(27, "ct")]
    s = match_summary.compute_summary_stats({"rounds": rounds, "kills": []})
    eq(s["scoreCT"], 12, "OT scoreCT (12:12 + swapped ct wins -> teamT)")
    eq(s["scoreT"], 15, "OT scoreT")
    ok(match_summary._is_swapped_at_round(25) is True, "round 25 swapped (per JS parity)")
    ok(match_summary._is_swapped_at_round(28) is False, "round 28 not swapped")


def test_team_pinning_and_suicides():
    rounds = [_round(1, "ct", start=0), _round(13, "t", start=1000)]
    kills = [
        _kill(10, "Ana", "ct", "Bob", "t"),      # first half: Ana=CT, Bob=T
        _kill(20, "Ana", "ct", "Cid", "t"),      # Ana 2 kills
        _kill(30, "Bob", "t", "Ana", "ct"),      # Bob 1 kill
        _kill(1100, "Bob", "ct", "Ana", "t"),    # 2nd half swap - side ignored for pinning
        _kill(1200, "Cid", "ct", "Cid", "ct"),   # suicide - not a kill
    ]
    s = match_summary.compute_summary_stats({"rounds": rounds, "kills": kills})
    t1 = {p["name"]: p["kills"] for p in s["team1"]}   # started CT
    t2 = {p["name"]: p["kills"] for p in s["team2"]}   # started T
    eq(t1.get("Ana"), 2, "Ana pinned CT with 2 kills")
    ok("Bob" in t2, "Bob pinned T (first-half side, despite 2nd-half CT kill)")
    eq(t2.get("Bob"), 2, "Bob 2 real kills (k30 + k1100), suicide by Cid excluded")
    ok("Cid" in t2, "Cid pinned T, 0 kills present")
    eq(t2.get("Cid"), 0, "Cid 0 kills")


def test_duration_and_kills_total():
    rounds = [_round(1, "ct", end=5000), _round(2, "t", end=9000), _round(3, "ct", end=7000)]
    kills = [_kill(1, "A", "ct", "B", "t"), _kill(2, "B", "t", "B", "t")]
    s = match_summary.compute_summary_stats({"rounds": rounds, "kills": kills})
    eq(s["durationTicks"], 9000, "duration = max end tick")
    eq(s["killsTotal"], 1, "killsTotal excludes suicide")


def test_player_rows():
    kills = [
        _kill(1, "A", "ct", "B", "t", assist="C"),
        _kill(2, "A", "ct", "B", "t"),
        _kill(3, "B", "t", "A", "ct"),
        _kill(4, "A", "ct", "A", "ct"),   # suicide: death for A, no kill
    ]
    rows = {r["name"]: r for r in match_summary.compute_player_rows({"kills": kills})}
    eq(rows["A"]["kills"], 2, "A 2 kills")
    eq(rows["A"]["deaths"], 2, "A 2 deaths (incl suicide)")
    eq(rows["B"]["deaths"], 2, "B 2 deaths")
    eq(rows["B"]["kills"], 1, "B 1 kill")
    eq(rows["C"]["assists"], 1, "C 1 assist")
    eq(rows["C"]["kills"], 0, "C 0 kills")


def test_avg_rank():
    # Competitive (rank_type 12): mean of skill groups -> named rank.
    comp = match_summary.avg_rank({"ranks": [
        {"rank_type": 12, "rank": 16}, {"rank_type": 12, "rank": 17},
        {"rank_type": 12, "rank": 0},   # unranked ignored
    ]})
    eq(comp["rankType"], 12, "competitive rank_type")
    eq(comp["avg"], 16.5, "competitive avg")
    eq(comp["n"], 2, "unranked excluded")
    ok(comp["label"] in ("Legendary Eagle Master", "Legendary Eagle"), f"competitive label ({comp['label']})")
    ok(0 < comp["sort"] <= 1, "competitive sort normalized 0-1")

    # Premier (rank_type 11): mean rating -> comma-formatted, 0-1 sort.
    prem = match_summary.avg_rank({"ranks": [
        {"rank_type": 11, "rank": 15000}, {"rank_type": 11, "rank": 17400}]})
    eq(prem["rankType"], 11, "premier rank_type")
    eq(prem["label"], "16,200", "premier label comma-formatted")
    ok(0 < prem["sort"] < 1, "premier sort normalized 0-1")

    # Dominant type wins when mixed; no ranks -> empty.
    eq(match_summary.avg_rank({})["label"], "", "no ranks -> empty label")
    eq(match_summary.avg_rank({"ranks": []})["n"], 0, "empty ranks -> n=0")


def test_mode_label():
    # A Premier rating in the demo outranks the filename's mode token, which is
    # only whatever was picked at upload.
    prem = {"ranks": [{"rank_type": 11, "rank": 21040}]}
    eq(match_summary.mode_label(prem, "mm"), "PREM", "premier rating -> PREM despite an 'mm' token")
    eq(match_summary.mode_label(prem, ""), "PREM", "premier wins even with no token")

    # Anything else keeps the token: nothing in the demo contradicts it.
    comp = {"ranks": [{"rank_type": 12, "rank": 17}]}
    eq(match_summary.mode_label(comp, "mm"), "MM", "competitive keeps the token")
    eq(match_summary.mode_label({}, "mm"), "MM", "no ranks at all keeps the token")
    eq(match_summary.mode_label({}, "wingman"), "WINGMAN", "token is upper-cased as-is")
    eq(match_summary.mode_label({}, ""), "", "no token and no ranks -> empty")

    # ANY premier rating counts, not the dominant type: rank rows are missing
    # for unranked players, so one rating is all a Premier match may surface.
    one_of_five = {"ranks": [
        {"rank_type": 12, "rank": 15}, {"rank_type": 12, "rank": 16},
        {"rank_type": 11, "rank": 10589},
    ]}
    eq(match_summary.mode_label(one_of_five, "mm"), "PREM",
       "a single premier rating is enough, even against a competitive majority")

    # rank 0 is 'unranked', not a rating - it must not promote a match to PREM.
    eq(match_summary.mode_label({"ranks": [{"rank_type": 11, "rank": 0}]}, "mm"), "MM",
       "rank_type 11 with rank 0 is unranked, not a Premier rating")
    ok(not match_summary.premier_ranks_present({"ranks": [{"rank_type": 11}]}),
       "a rank_type with no rank value is not a rating")
    ok(not match_summary.premier_ranks_present({"ranks": ["junk", None, 7]}),
       "malformed rank rows are ignored rather than raising")

    # FACEIT is the demo's own word (cmd/parser reads the server name out of the
    # header), so unlike 'mm' it is not a default for the Premier check to
    # correct - a FACEIT match is not a Valve Premier match whatever rank rows
    # it carries. In practice it carries none: every FACEIT player is
    # rank_type -1.
    eq(match_summary.mode_label({}, "faceit"), "FACEIT", "the faceit token labels FACEIT")
    eq(match_summary.mode_label({"ranks": []}, "faceit"), "FACEIT", "no ranks, still FACEIT")
    eq(match_summary.mode_label(prem, "faceit"), "FACEIT",
       "where the match was played beats a Premier rating in it")
    eq(match_summary.mode_label({}, "FaceIt"), "FACEIT", "the token is matched case-insensitively")
    ok("faceit" in match_summary.DEMO_DECIDED_MODES,
       "faceit is listed as a demo-decided mode (mirrored by matchhero.logic.js)")


def test_empty_match():
    s = match_summary.compute_summary_stats({})
    eq(s["scoreCT"], 0, "empty scoreCT")
    eq(s["scoreT"], 0, "empty scoreT")
    eq(s["team1"], [], "empty team1")
    eq(s["durationTicks"], 0, "empty duration")


for _fn in list(globals().values()):
    if callable(_fn) and getattr(_fn, "__name__", "").startswith("test_"):
        _fn()

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
