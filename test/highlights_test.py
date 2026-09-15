"""Unit tests for analysis/highlights.py (multi-kill/clutch/high-impact-kill
detection) and analysis/clipwindow.py (clip tick-window sizing).

Run:  python3 test/highlights_test.py     (no deps, no browser)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from analysis import clipwindow  # noqa: E402
from analysis import highlights  # noqa: E402

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


# ── clipwindow ───────────────────────────────────────────────────────────────

def test_clip_window_for_kill_unclamped():
    start, end = clipwindow.clip_window_for_kill(1000)
    eq(start, 1000 - clipwindow.KILL_CLIP_BEFORE_TICKS, "start = tick - before")
    eq(end, 1000 + clipwindow.KILL_CLIP_AFTER_TICKS, "end = tick + after")


def test_clip_window_for_kill_clamps_to_round_start():
    start, end = clipwindow.clip_window_for_kill(1000, round_start=900)
    eq(start, 900, "start clamped up to round_start when window would predate it")
    eq(end, 1032, "end unaffected by round_start clamp")


def test_clip_window_for_kill_span():
    start, end = clipwindow.clip_window_for_kill_span(1000, 1500, round_start=200, round_end=2000)
    eq(start, 1000 - clipwindow.KILL_CLIP_BEFORE_TICKS, "span start anchored to first kill")
    eq(end, 1500 + clipwindow.KILL_CLIP_AFTER_TICKS, "span end anchored to last kill")


def test_clip_window_for_kill_span_clamps_round_end():
    start, end = clipwindow.clip_window_for_kill_span(1000, 1500, round_end=2000)
    eq(end, 1500 + clipwindow.KILL_CLIP_AFTER_TICKS,
       "a span ending well inside the round keeps its own after-buffer")
    start, end = clipwindow.clip_window_for_kill_span(1000, 5000, round_end=2000)
    eq(end, 2000 + clipwindow.KILL_CLIP_AFTER_TICKS,
       "a span running past the round is clamped to round_end plus the tail")


def test_clip_window_for_kill_span_keeps_tail_when_round_ends_on_the_kill():
    # A round very often ends ON its last kill. Clamping to round_end then cut
    # the clip at the exact tick of the final frag - the one moment it exists
    # to show. Real case: de_inferno round 15, last kill and round end both
    # at tick 86506.
    start, end = clipwindow.clip_window_for_kill_span(85833, 86506,
                                                      round_start=82914, round_end=86506)
    eq(end, 86506 + clipwindow.KILL_CLIP_AFTER_TICKS,
       "the final frag is followed through, not cut on its own tick")


# ── highlights: multi-kills ───────────────────────────────────────────────────

def _round(num, start, end):
    return {"round_num": num, "start": start, "end": end, "winner_side": ""}


def _kill(tick, rnum, attacker, victim, aside="t", vside="ct", **extra):
    row = {"tick": tick, "round_num": rnum, "attacker_name": attacker,
           "attacker_side": aside, "victim_name": victim, "victim_side": vside}
    row.update(extra)
    return row


def test_multikill_detects_ace():
    match = {
        "rounds": [_round(1, 1000, 3000)],
        "kills": [
            _kill(1100, 1, "Alice", "Bob"),
            _kill(1200, 1, "Alice", "Carl"),
            _kill(1300, 1, "Alice", "Dan"),
            _kill(1400, 1, "Alice", "Eve"),
            _kill(1500, 1, "Alice", "Frank"),
        ],
    }
    hl = highlights.compute_match_highlights(match, "Alice")
    mk = [h for h in hl if h["type"] == "multikill"]
    eq(len(mk), 1, "one multikill highlight for the round")
    eq(mk[0]["label"], "ACE", "5 kills labelled ACE")
    eq(mk[0]["start_tick"], 1000, "window start clamped to the round's start tick")
    eq(mk[0]["end_tick"], 1500 + clipwindow.KILL_CLIP_AFTER_TICKS, "window ends after last kill")


def test_multikill_ignores_single_kill_rounds():
    match = {
        "rounds": [_round(1, 1000, 3000)],
        "kills": [_kill(1100, 1, "Alice", "Bob")],
    }
    hl = highlights.compute_match_highlights(match, "Alice")
    eq([h for h in hl if h["type"] == "multikill"], [], "no multikill highlight for a lone kill")


# ── highlights: clutch ────────────────────────────────────────────────────────

def test_clutch_1v1_win_detected():
    # Round 1: CT is Alice+Bob, T is Carl+Dan. T kills Bob, then Alice (CT,
    # now the lone survivor) kills Carl then Dan to win 1v2->1v1->win.
    match = {
        "rounds": [_round(1, 1000, 5000)],
        "kills": [
            _kill(1100, 1, "Carl", "Bob", aside="t", vside="ct"),
            _kill(1300, 1, "Alice", "Carl", aside="ct", vside="t"),
            _kill(1500, 1, "Alice", "Dan", aside="ct", vside="t"),
        ],
    }
    match["rounds"][0]["winner_side"] = "ct"
    hl = highlights.compute_match_highlights(match, "Alice")
    clutches = [h for h in hl if h["type"] == "clutch"]
    eq(len(clutches), 1, "Alice's 1v2->win round is a clutch highlight")
    eq(clutches[0]["label"], "1v2 clutch", "labelled with opponent count at the clutch pivot")


def test_clutch_not_awarded_to_losing_lone_survivor():
    match = {
        "rounds": [_round(1, 1000, 5000)],
        "kills": [
            _kill(1100, 1, "Carl", "Bob", aside="t", vside="ct"),
            _kill(1300, 1, "Dan", "Alice", aside="t", vside="ct"),
        ],
    }
    match["rounds"][0]["winner_side"] = "t"
    hl = highlights.compute_match_highlights(match, "Alice")
    eq([h for h in hl if h["type"] == "clutch"], [], "no clutch when the lone survivor's team loses")


def test_clutch_opponent_count_is_measured_at_the_pivot():
    # 5v5 round: three CTs and two Ts trade, leaving Alice (CT) alone against
    # three living Ts, whom she then kills. The label must read the enemies
    # ALIVE when she was left alone (1v3), not everyone who played the round.
    match = {
        "rounds": [_round(1, 1000, 9000)],
        "kills": [
            _kill(1100, 1, "Alice", "Zed",  aside="ct", vside="t"),
            _kill(1200, 1, "Yan",   "Bob",  aside="t",  vside="ct"),
            _kill(1300, 1, "Yan",   "Carl", aside="t",  vside="ct"),
            _kill(1400, 1, "Xu",    "Dan",  aside="t",  vside="ct"),
            _kill(1500, 1, "Alice", "Yan",  aside="ct", vside="t"),
            _kill(1600, 1, "Alice", "Xu",   aside="ct", vside="t"),
            _kill(1700, 1, "Alice", "Wim",  aside="ct", vside="t"),
        ],
    }
    match["rounds"][0]["winner_side"] = "ct"
    hl = highlights.compute_match_highlights(match, "Alice")
    clutch = [h for h in hl if h["type"] == "clutch"][0]
    eq(clutch["opponents"], 3, "three Ts were alive when Alice was left alone")
    eq(clutch["label"], "1v3 clutch", "label reflects the count at the pivot")
    eq(clutch["tick"], 1400, "pivot is the kill that left her alone, not a later one")


def test_clutch_clip_window_spans_the_whole_clutch():
    match = {
        "rounds": [_round(1, 1000, 9000)],
        "kills": [
            _kill(1100, 1, "Yan",   "Bob", aside="t",  vside="ct"),
            _kill(1500, 1, "Alice", "Yan", aside="ct", vside="t"),
            _kill(2500, 1, "Alice", "Xu",  aside="ct", vside="t"),
        ],
    }
    match["rounds"][0]["winner_side"] = "ct"
    clutch = [h for h in highlights.compute_match_highlights(match, "Alice")
              if h["type"] == "clutch"][0]
    eq(clutch["start_tick"], 1000,
       "window opens at the kill that started the clutch, clamped to round start")
    eq(clutch["end_tick"], 2500 + clipwindow.KILL_CLIP_AFTER_TICKS,
       "window runs to the last kill of the round")


# ── highlights: high-impact single kills ──────────────────────────────────────

def test_impact_kill_flags_opening_and_wallbang():
    match = {
        "rounds": [_round(1, 1000, 3000)],
        "kills": [
            _kill(1100, 1, "Alice", "Bob", weapon="ak47"),
            _kill(1400, 1, "Carl", "Alice", aside="ct", vside="t", weapon="m4a1"),
            _kill(1700, 1, "Alice", "Dan", weapon="awp", wallbang=True),
        ],
    }
    hl = highlights.compute_match_highlights(match, "Alice")
    impact = [h for h in hl if h["type"] == "impact_kill"]
    eq(len(impact), 2, "opening kill + wallbang kill both flagged for Alice")
    eq(impact[0]["detail"], "opening kill", "first kill of the round is the opener")
    ok("wallbang" in impact[1]["detail"], "wallbang kill carries the wallbang reason")


# ── highlights: priority ordering ─────────────────────────────────────────────

def test_priority_orders_clutch_then_multikill_then_impact():
    # One round where Alice does everything: opens the round, wallbangs, gets
    # a 3K, and clutches it out 1v2. All four moments land in one match.
    match = {
        "rounds": [_round(1, 1000, 5000)],
        "kills": [
            _kill(1100, 1, "Carl", "Bob", aside="t", vside="ct"),
            _kill(1200, 1, "Alice", "Carl", aside="ct", vside="t", weapon="ak47"),
            _kill(1300, 1, "Alice", "Dan", aside="ct", vside="t", weapon="awp",
                  wallbang=True),
            _kill(1400, 1, "Alice", "Eve", aside="ct", vside="t", weapon="awp"),
        ],
    }
    match["rounds"][0]["winner_side"] = "ct"
    hl = highlights.compute_match_highlights(match, "Alice")
    eq([h["type"] for h in hl][:2], ["clutch", "multikill"],
       "clutch outranks the multi-kill, both ahead of single kills")
    ok(all(h["type"] == "impact_kill" for h in hl[2:]),
       "impact kills sort last")


def test_priority_ranks_bigger_moment_first_within_a_type():
    key = highlights.highlight_sort_key
    ok(key({"type": "clutch", "opponents": 3, "tick": 900})
       < key({"type": "clutch", "opponents": 1, "tick": 100}),
       "1v3 clutch outranks an earlier 1v1")
    ok(key({"type": "multikill", "kills": 5, "tick": 900})
       < key({"type": "multikill", "kills": 2, "tick": 100}),
       "ACE outranks an earlier 2K")
    ok(key({"type": "multikill", "kills": 5, "tick": 100})
       > key({"type": "clutch", "opponents": 1, "tick": 900}),
       "any clutch outranks any multi-kill")


def test_priority_ranks_impact_reasons_opening_then_wallbang():
    key = highlights.highlight_sort_key
    ok(key({"type": "impact_kill", "reasons": ["opening kill"], "tick": 900})
       < key({"type": "impact_kill", "reasons": ["wallbang"], "tick": 100}),
       "opening kill outranks an earlier wallbang")
    ok(key({"type": "impact_kill", "reasons": ["through smoke"], "tick": 100})
       < key({"type": "impact_kill", "reasons": ["while flashed"], "tick": 100}),
       "through smoke outranks a flashed kill")
    ok(key({"type": "impact_kill", "reasons": ["while flashed", "opening kill"],
            "tick": 100})
       == key({"type": "impact_kill", "reasons": ["opening kill"], "tick": 100}),
       "a multi-reason kill ranks by its best reason")
    ok(key({"type": "impact_kill", "reasons": [], "tick": 100})
       > key({"type": "impact_kill", "reasons": ["while flashed"], "tick": 100}),
       "an unknown/absent reason sorts behind every known one")


def test_ties_break_chronologically():
    key = highlights.highlight_sort_key
    ok(key({"type": "impact_kill", "reasons": ["wallbang"], "tick": 100})
       < key({"type": "impact_kill", "reasons": ["wallbang"], "tick": 900}),
       "equal-priority rows keep round order")


# ── highlights: match-wide aggregation ────────────────────────────────────────

def test_all_match_highlights_merges_players_into_one_ranked_list():
    # Alice (T) aces round 1; Bob (CT) is left alone in round 2 and wins it.
    # Across players the clutch still outranks the ace - same priority order
    # as within one player. Sides are consistent between the rounds because
    # the clutch roster is the match roster: a player cannot be CT in one
    # round and T in the next without a halftime swap.
    match = {
        "rounds": [_round(1, 1000, 3000), _round(2, 4000, 6000)],
        "kills": [
            _kill(1100, 1, "Alice", "Bob"), _kill(1200, 1, "Alice", "Carl"),
            _kill(1300, 1, "Alice", "Dan"), _kill(1400, 1, "Alice", "Eve"),
            _kill(1500, 1, "Alice", "Frank"),
            _kill(4100, 2, "Alice", "Carl"), _kill(4200, 2, "Alice", "Dan"),
            _kill(4300, 2, "Alice", "Eve"),  _kill(4350, 2, "Alice", "Frank"),
            _kill(4400, 2, "Bob", "Alice", aside="ct", vside="t"),
            _kill(4500, 2, "Bob", "Yan",   aside="ct", vside="t"),
        ],
    }
    match["rounds"][1]["winner_side"] = "ct"
    hl = highlights.compute_all_match_highlights(match, ["Alice", "Bob"])
    eq([h["type"] for h in hl][:2], ["clutch", "multikill"],
       "Bob's clutch outranks Alice's ace across players")
    eq(hl[0]["player"], "Bob", "every row names the player it belongs to")
    ok(all("player" in h for h in hl), "no row is missing its player")
    ok(any(h["player"] == "Alice" and h["label"] == "ACE" for h in hl),
       "Alice's ace is in the merged list")


def test_clutch_not_invented_for_a_quiet_teammate():
    # THE regression: Frank is on Alice's team and does nothing all round -
    # no kill, no death - but he is alive. Alice is not the last one standing
    # and must not be credited with a clutch. Frank is only visible in the
    # match roster through a damage event, exactly as a quiet round looks in
    # real data.
    match = {
        "rounds": [_round(1, 1000, 5000)],
        "kills": [
            _kill(1100, 1, "Carl", "Bob", aside="t", vside="ct"),
            _kill(1300, 1, "Alice", "Carl", aside="ct", vside="t"),
            _kill(1500, 1, "Alice", "Dan", aside="ct", vside="t"),
        ],
        "damage": [
            {"round_num": 1, "tick": 1050, "attacker_name": "Frank",
             "attacker_side": "ct", "victim_name": "Carl", "victim_side": "t"},
        ],
    }
    match["rounds"][0]["winner_side"] = "ct"
    hl = highlights.compute_match_highlights(match, "Alice")
    eq([h for h in hl if h["type"] == "clutch"], [],
       "a living teammate who never appears in a kill still blocks the clutch")


def test_clutch_still_found_when_the_quiet_teammate_is_dead():
    # Same round, but Frank died first - Alice really is alone, so the clutch
    # survives the roster fix rather than being suppressed with it.
    match = {
        "rounds": [_round(1, 1000, 5000)],
        "kills": [
            _kill(1050, 1, "Carl", "Frank", aside="t", vside="ct"),
            _kill(1100, 1, "Carl", "Bob", aside="t", vside="ct"),
            _kill(1300, 1, "Alice", "Carl", aside="ct", vside="t"),
            _kill(1500, 1, "Alice", "Dan", aside="ct", vside="t"),
        ],
    }
    match["rounds"][0]["winner_side"] = "ct"
    clutches = [h for h in highlights.compute_match_highlights(match, "Alice")
                if h["type"] == "clutch"]
    eq(len(clutches), 1, "Alice is genuinely last alive once Frank is dead")
    eq(clutches[0]["label"], "1v2 clutch", "counted against the living Ts")


def test_all_match_highlights_handles_empty_and_unknown_players():
    match = {"rounds": [_round(1, 1000, 3000)],
             "kills": [_kill(1100, 1, "Alice", "Bob")]}
    eq(highlights.compute_all_match_highlights(match, []), [],
       "no players -> empty list")
    eq(highlights.compute_all_match_highlights(match, ["Nobody"]), [],
       "a player with no moments contributes nothing")
    eq(highlights.compute_all_match_highlights(match, [None, ""]), [],
       "blank names are skipped rather than crashing")


def test_all_match_highlights_matches_per_player_output():
    match = {
        "rounds": [_round(1, 1000, 3000)],
        "kills": [
            _kill(1100, 1, "Alice", "Bob", weapon="ak47"),
            _kill(1700, 1, "Alice", "Dan", weapon="awp", wallbang=True),
        ],
    }
    solo = highlights.compute_match_highlights(match, "Alice")
    both = highlights.compute_all_match_highlights(match, ["Alice"])
    eq([h["label"] for h in both], [h["label"] for h in solo],
       "one-player aggregation is the per-player list, in the same order")


def test_multikill_threshold_excludes_a_2k():
    # A 2K is a won duel plus its trade, not a highlight. Before
    # MULTIKILL_MIN this produced a MULTI-KILL row for every such round, and
    # they outnumbered every other kind of moment ~3:1 in the real corpus.
    match = {
        "rounds": [_round(1, 1000, 3000)],
        "kills": [
            _kill(1100, 1, "Alice", "Bob"),
            _kill(1200, 1, "Alice", "Carl"),
        ],
    }
    hl = highlights.compute_match_highlights(match, "Alice")
    eq([h for h in hl if h["type"] == "multikill"], [],
       "a 2K is below MULTIKILL_MIN and is not a multi-kill highlight")


def test_multikill_threshold_admits_a_3k():
    match = {
        "rounds": [_round(1, 1000, 3000)],
        "kills": [
            _kill(1100, 1, "Alice", "Bob"),
            _kill(1200, 1, "Alice", "Carl"),
            _kill(1300, 1, "Alice", "Dan"),
        ],
    }
    mk = [h for h in highlights.compute_match_highlights(match, "Alice")
          if h["type"] == "multikill"]
    eq(len(mk), 1, "a 3K is a multi-kill highlight")
    eq(mk[0]["label"], "3K", "labelled 3K")


def test_multikill_is_one_row_per_round_not_one_per_kill():
    # The reel showed a 2K row for round 1, round 2, round 3 ... for several
    # players at once, which reads as "every kill counts as a multi-kill".
    # It never was one row per kill, and this pins that.
    match = {
        "rounds": [_round(1, 1000, 3000)],
        "kills": [
            _kill(1100, 1, "Alice", "Bob"),
            _kill(1200, 1, "Alice", "Carl"),
            _kill(1300, 1, "Alice", "Dan"),
            _kill(1400, 1, "Alice", "Eve"),
        ],
    }
    mk = [h for h in highlights.compute_match_highlights(match, "Alice")
          if h["type"] == "multikill"]
    eq(len(mk), 1, "four kills in one round are ONE 4K row, not four rows")
    eq(mk[0]["label"], "4K", "labelled by the round's total")


def test_clutch_requires_surviving_the_round():
    # Alice is last CT alive against two Ts, dies, and CT still wins (in a
    # real demo: the defuse completes, or the bomb timer runs out with the
    # site clear). She lost the 1v2, so it is not a clutch WON.
    match = {
        "rounds": [_round(1, 1000, 5000)],
        "kills": [
            _kill(1100, 1, "Carl", "Bob", aside="t", vside="ct"),
            _kill(1300, 1, "Dan", "Alice", aside="t", vside="ct"),
        ],
    }
    match["rounds"][0]["winner_side"] = "ct"
    hl = highlights.compute_match_highlights(match, "Alice")
    eq([h for h in hl if h["type"] == "clutch"], [],
       "the last player alive dying before the round ends is not a clutch win")


def test_clutch_still_awarded_when_the_player_survives():
    # Same shape, but Alice wins the duels - the guard above must not
    # suppress a genuine clutch.
    match = {
        "rounds": [_round(1, 1000, 5000)],
        "kills": [
            _kill(1100, 1, "Carl", "Bob", aside="t", vside="ct"),
            _kill(1300, 1, "Alice", "Carl", aside="ct", vside="t"),
            _kill(1500, 1, "Alice", "Dan", aside="ct", vside="t"),
        ],
    }
    match["rounds"][0]["winner_side"] = "ct"
    cl = [h for h in highlights.compute_match_highlights(match, "Alice")
          if h["type"] == "clutch"]
    eq(len(cl), 1, "surviving the 1v2 is still a clutch")
    eq(cl[0]["label"], "1v2 clutch", "opponent count unchanged")


def test_clutch_is_one_row_per_round():
    # Four kills inside one clutch produce one clutch row, not four.
    match = {
        "rounds": [_round(1, 1000, 6000)],
        "kills": [
            _kill(1100, 1, "Carl", "Bob", aside="t", vside="ct"),
            _kill(1200, 1, "Dan", "Eve", aside="t", vside="ct"),
            _kill(1300, 1, "Alice", "Carl", aside="ct", vside="t"),
            _kill(1400, 1, "Alice", "Dan", aside="ct", vside="t"),
            _kill(1500, 1, "Alice", "Frank", aside="ct", vside="t"),
        ],
    }
    match["rounds"][0]["winner_side"] = "ct"
    cl = [h for h in highlights.compute_match_highlights(match, "Alice")
          if h["type"] == "clutch"]
    eq(len(cl), 1, "one clutch row for the round, whatever it took to win it")


for _fn in list(globals().values()):
    if callable(_fn) and getattr(_fn, "__name__", "").startswith("test_"):
        _fn()

print(f"\n{_passed} passed, {_failed} failed")
sys.exit(1 if _failed else 0)
