"""Player highlight-reel detection: multi-kills/aces, clutch wins, and
high-impact single kills, computed straight from the base match JSON
(kills[]/rounds[]) - no dependency on the Overwatch 2nd-pass extractor, so
this works for every parsed match, not just ones with an overwatch_raw
extract.

Multi-kill tally and 1vX clutch detection here port the exact logic already
proven client-side in templates/match.html (computeStats(): mkRounds ~lines
667-668/816-818, clutch detection ~lines 781-804) so highlight rounds match
what the Rounds & Kills tab already shows, just recomputed server-side so a
cross-match reel can be built without loading match.html's JS per match.
"""

from .clipwindow import clip_window_for_kill, clip_window_for_kill_span
from .moments import round_lookup, roster_side_map, side_at_round

MULTIKILL_LABELS = {2: "2K", 3: "3K", 4: "4K", 5: "ACE"}

# Smallest multi-kill worth a row in the reel.
#
# This was 2, and a 2K is simply not a highlight: it is what an ordinary
# won duel plus its trade looks like. Every 2-kill round by every player
# produced a "MULTI-KILL" row, so the reel filled with them - 446 of the 619
# multi-kill rows in the 17-match corpus were 2Ks, and on the player page
# (which caps at 6 rows per match) a match's entire reel could be nothing but
# 2Ks, which is what "it counts every kill of the round as a multi-kill"
# looks like from the outside. 3K+ is the usual threshold and is also where
# the label stops being a description of a normal round.
#
# Nothing is lost: the per-round 2K tally is still on the match page's
# Combat & Utility "Multikills" column, which is where a count belongs.
MULTIKILL_MIN = 3

# Display priority within one match: the reel is capped per match in the UI
# (player.html's PER_MATCH), so the rows that survive the cap should be the
# ones worth watching, not simply the earliest ones in the round order.
HIGHLIGHT_TYPE_RANK = {"clutch": 0, "multikill": 1, "impact_kill": 2}

# Sub-ranking inside impact_kill, best first. A kill can carry several
# reasons; it ranks by its best one.
IMPACT_REASON_RANK = ["opening kill", "wallbang", "through smoke",
                      "noscope", "while flashed"]


def highlight_sort_key(h):
    """Priority sort key for one highlight (lower sorts first): clutches, then
    multi-kills, then high-impact single kills. Inside a type the bigger
    moment wins - more opponents clutched, more kills in the multi-kill, a
    better reason on an impact kill - and ties break chronologically so a
    match's rows still read in round order at equal priority."""
    kind = h.get("type")
    tick = h.get("tick") or 0
    rank = HIGHLIGHT_TYPE_RANK.get(kind, len(HIGHLIGHT_TYPE_RANK))
    if kind == "clutch":
        return (rank, -(h.get("opponents") or 1), tick)
    if kind == "multikill":
        return (rank, -(h.get("kills") or 2), tick)
    reasons = h.get("reasons") or []
    best = min((IMPACT_REASON_RANK.index(r) for r in reasons
                if r in IMPACT_REASON_RANK), default=len(IMPACT_REASON_RANK))
    return (rank, best, tick)


def _side_norm(side):
    s = (side or "").lower()
    if s == "terrorist":
        return "t"
    return s


def _weapon_label(weapon):
    """'weapon_usps' -> 'USPS' - same weapon_/underscore stripping convention
    match.html/player.html already use for display (see weaponLabel() and
    the Weapons Used table)."""
    return (weapon or "?").replace("weapon_", "").replace("_", " ").upper()


def _round_rosters(side_map, rnum, rkills):
    """(ct_names, t_names) alive at the start of round `rnum`.

    Taken from the MATCH roster translated into this round's sides, not from
    the players who happen to appear in this round's kill events. The
    kill-derived version was a real false-positive source: a player who
    neither killed nor died in a round was counted as absent, which makes
    their team look smaller, so an ordinary survivor with two teammates still
    alive was reported as the last man standing and credited with a 1vX
    clutch they never played (verified on de_ancient_20260712_1353_mm round
    1). analysis/moments.py's index has always counted this correctly; this is
    the same roster source.

    This round's kill participants are still unioned in, so a match whose
    roster can't be derived at all degrades to the old behaviour rather than
    to an empty roster (which would detect no clutches at all).
    """
    ct, t = set(), set()
    for name, first_half in side_map.items():
        side = side_at_round(first_half, rnum)
        if side == "ct":
            ct.add(name)
        elif side == "t":
            t.add(name)
    for k in rkills:
        a, aside = k.get("attacker_name"), _side_norm(k.get("attacker_side"))
        if a and a not in ct and a not in t:
            (ct if aside == "ct" else t).add(a)
        v, vside = k.get("victim_name"), _side_norm(k.get("victim_side"))
        if v and v not in ct and v not in t:
            (ct if vside == "ct" else t).add(v)
    return ct, t


def compute_match_highlights(match_data, player_name):
    """Return a list of {type, round, tick, start_tick, end_tick, label,
    detail} highlight moments for player_name in this one match, ordered by
    highlight_sort_key() - best moment first, not chronologically."""
    rounds = match_data.get("rounds", []) or []
    kills = match_data.get("kills", []) or []

    round_bounds = {}   # round_num -> (start_tick, end_tick or None)
    round_winner = {}   # round_num -> 'ct' | 't' | ''
    for r in rounds:
        rnum = r.get("round_num")
        if rnum is None:
            continue
        round_bounds[rnum] = (r.get("start"), r.get("end"))
        round_winner[rnum] = _side_norm(r.get("winner_side") or r.get("winner"))

    # Who was on which team, from every side-carrying event in the match - not
    # just this round's kills. See _round_rosters().
    side_map = roster_side_map(match_data, round_lookup(rounds))

    kills_by_round = {}
    for k in kills:
        rnum = k.get("round_num")
        kills_by_round.setdefault(rnum, []).append(k)
    for rnum in kills_by_round:
        kills_by_round[rnum].sort(key=lambda k: k.get("tick") or 0)

    highlights = []

    # Multi-kills / aces.
    for rnum, rkills in kills_by_round.items():
        player_kills = [k for k in rkills
                         if k.get("attacker_name") == player_name
                         and k.get("attacker_name") != k.get("victim_name")]
        if len(player_kills) < MULTIKILL_MIN:
            continue
        rstart, rend = round_bounds.get(rnum, (None, None))
        start_tick, end_tick = clip_window_for_kill_span(
            player_kills[0]["tick"], player_kills[-1]["tick"], rstart, rend)
        n = min(len(player_kills), 5)
        highlights.append({
            "type": "multikill",
            "round": rnum,
            "tick": player_kills[-1]["tick"],
            "start_tick": start_tick, "end_tick": end_tick,
            "label": MULTIKILL_LABELS.get(n, f"{n}K"),
            "detail": f"{n} kills in round {rnum}",
            "kills": n,
        })

    # 1vX clutch wins, against the real remaining count - see _round_rosters().
    #
    # Three things all have to hold: the player is the last one alive on their
    # side, their side wins the round, and they are still alive when it ends.
    # That last one is not implied by the other two - a lone T can plant, die,
    # and win on the bomb timer, and crediting that as a clutch WON overstates
    # what happened (they lost the 1vX; the bomb won the round). It is checked
    # by looking for the player in the round's later victims, below.
    for rnum, rkills in kills_by_round.items():
        alive_ct, alive_t = _round_rosters(side_map, rnum, rkills)

        dead_ct, dead_t = set(), set()
        winner = round_winner.get(rnum, "")
        clutch_pivot_tick = None
        clutch_opponents = 1
        for k in rkills:
            v, vside = k.get("victim_name"), _side_norm(k.get("victim_side"))
            if not v:
                continue
            (dead_ct if vside == "ct" else dead_t).add(v)
            live_ct = alive_ct - dead_ct
            live_t = alive_t - dead_t
            # The pivot is the moment the player BECAME the last one alive, so
            # it's recorded once (`is None`) and not re-set by every later kill
            # of the same clutch: it anchors both the clip window (which should
            # span the whole 1vX, not just its final kill) and the opponent
            # count in the label.
            if clutch_pivot_tick is None:
                if len(live_ct) == 1 and len(live_t) >= 1 and winner == "ct" \
                        and next(iter(live_ct)) == player_name:
                    clutch_pivot_tick = k.get("tick")
                    clutch_opponents = len(live_t)
                elif len(live_t) == 1 and len(live_ct) >= 1 and winner == "t" \
                        and next(iter(live_t)) == player_name:
                    clutch_pivot_tick = k.get("tick")
                    clutch_opponents = len(live_ct)
        # Died after becoming the last one alive: the round may still have
        # been won (bomb), but the 1vX was not.
        if clutch_pivot_tick is not None and any(
                k.get("victim_name") == player_name
                and (k.get("tick") or 0) >= clutch_pivot_tick for k in rkills):
            clutch_pivot_tick = None
        if clutch_pivot_tick is not None:
            rstart, rend = round_bounds.get(rnum, (None, None))
            last_tick = rkills[-1].get("tick") or clutch_pivot_tick
            start_tick, end_tick = clip_window_for_kill_span(
                clutch_pivot_tick, last_tick, rstart, rend)
            opponents = clutch_opponents
            highlights.append({
                "type": "clutch",
                "round": rnum,
                "tick": clutch_pivot_tick,
                "start_tick": start_tick, "end_tick": end_tick,
                "label": f"1v{max(opponents, 1)} clutch",
                "detail": f"Won round {rnum} as the last player alive",
                "opponents": max(opponents, 1),
            })

    # High-impact single kills: opening kill of the round, plus wallbang /
    # through-smoke / no-scope / while-blind kills (same flags Overwatch's
    # moment builder uses, see analysis/overwatch.py).
    for rnum, rkills in kills_by_round.items():
        opener = rkills[0] if rkills else None
        for k in rkills:
            if k.get("attacker_name") != player_name \
                    or k.get("attacker_name") == k.get("victim_name"):
                continue
            reasons = []
            if k is opener:
                reasons.append("opening kill")
            if k.get("wallbang"):
                reasons.append("wallbang")
            if k.get("through_smoke"):
                reasons.append("through smoke")
            if k.get("noscope"):
                reasons.append("noscope")
            if k.get("attacker_blind"):
                reasons.append("while flashed")
            if not reasons:
                continue
            rstart, _ = round_bounds.get(rnum, (None, None))
            start_tick, end_tick = clip_window_for_kill(k["tick"], rstart)
            highlights.append({
                "type": "impact_kill",
                "round": rnum,
                "tick": k["tick"],
                "start_tick": start_tick, "end_tick": end_tick,
                "label": f"Kill ({_weapon_label(k.get('weapon'))})",
                "detail": ", ".join(reasons),
                "reasons": reasons,
            })

    highlights.sort(key=highlight_sort_key)
    return highlights


def compute_all_match_highlights(match_data, player_names):
    """Every player's highlights for one match, merged into one
    priority-ranked list (the match page's Highlights tab).

    compute_match_highlights() is per-player by construction and so doesn't
    name the player in its rows; here each row gets a `player` key, which the
    tab needs both to label the row and to look up that moment's recorded clip
    (clip ids are player-specific). Ordering is the same highlight_sort_key
    used within a single player, applied across all of them - so the match's
    clutches lead, then its multi-kills, then its impact kills."""
    highlights = []
    for name in player_names:
        if not name:
            continue
        for h in compute_match_highlights(match_data, name):
            h["player"] = name
            highlights.append(h)
    highlights.sort(key=highlight_sort_key)
    return highlights
