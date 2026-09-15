"""Pure match-summary computation - the server-side twin of preprocessor.html's
`computeSummary()`, moved server-side so the match list loads from one index
instead of re-reading every match document in the browser.

Kept dependency-free and I/O-free so it can be unit-checked for parity against
the historical client-side output (test/match_summary.test.py). app.py wraps it
with file loading + caching into the `/matches/summary` index.
"""

INF = float('inf')

# Score bookkeeping: regulation is MR12 (24 rounds, swap at 12); OT is MR3.
REGULATION_HALF   = 12
REGULATION_ROUNDS = 24
OT_HALF_LEN       = 3


def _is_swapped_at_round(r_num):
    """True when the team that started CT is playing T in round `r_num`
    (mirrors the JS `isSwappedAtRound`)."""
    if r_num <= REGULATION_HALF:
        return False
    if r_num <= REGULATION_ROUNDS:
        return True
    return ((r_num - REGULATION_ROUNDS - 1) // OT_HALF_LEN) % 2 == 0


def compute_summary_stats(data):
    """Return {scoreCT, scoreT, team1, team2, durationTicks, killsTotal, rounds}.

    team1 = the side that started on CT, team2 = started on T - each a list of
    {name, kills} sorted by kills desc. Parity with the old client computeSummary.
    """
    rounds = data.get('rounds') or []

    # Per-team score, side-swap aware.
    team_ct = team_t = 0
    for r in rounds:
        r_num = r.get('round_num')
        r_num = 0 if r_num is None else r_num
        w = (r.get('winner_side') or r.get('winner') or '').lower()
        if not _is_swapped_at_round(r_num):
            if w == 'ct':
                team_ct += 1
            elif w in ('t', 'terrorist'):
                team_t += 1
        else:
            if w in ('t', 'terrorist'):
                team_ct += 1
            elif w == 'ct':
                team_t += 1

    # First-half boundary: pin each player to the identity they had before the
    # regulation side swap so team rosters stay stable.
    half_start = next((r for r in rounds
                       if (r.get('round_num') or 0) == REGULATION_HALF + 1), None)
    half_tick = (half_start.get('start') if half_start and half_start.get('start') is not None
                 else INF)

    player_kills = {}   # name -> total kills
    player_side  = {}   # name -> 'ct' | 't'  (first-half side)

    for k in (data.get('kills') or []):
        tick = k.get('tick')
        in_first_half = (INF if tick is None else tick) < half_tick
        an, vn = k.get('attacker_name'), k.get('victim_name')
        if an and an != vn:
            player_kills[an] = player_kills.get(an, 0) + 1
            if in_first_half and an not in player_side and k.get('attacker_side'):
                player_side[an] = k['attacker_side'].lower()
        if vn:
            player_kills.setdefault(vn, 0)
            if in_first_half and vn not in player_side and k.get('victim_side'):
                player_side[vn] = k['victim_side'].lower()

    team1, team2 = [], []
    for name, kills in player_kills.items():
        (team1 if player_side.get(name) == 'ct' else team2).append(
            {'name': name, 'kills': kills})
    team1.sort(key=lambda p: -p['kills'])
    team2.sort(key=lambda p: -p['kills'])

    duration_ticks = 0
    for r in rounds:
        end = r.get('end')
        if isinstance(end, (int, float)) and end > duration_ticks:
            duration_ticks = int(end)

    return {
        'scoreCT':       team_ct,
        'scoreT':        team_t,
        'team1':         team1,
        'team2':         team2,
        'durationTicks': duration_ticks,
        'killsTotal':    sum(player_kills.values()),
        'rounds':        len(rounds),
    }


# Competitive / Wingman skill-group names (rank_type 12 / 7, ranks 1-18).
#
# Mirrored in the browser by static/matchhero.logic.js's CS_SKILL_GROUPS, which
# is the same list as an array (index 0 is "Unranked", so rank n sits at index
# n). The two are duplicated because the browser cannot import this module;
# test/matchhero.logic.test.mjs asserts they still agree.
_COMP_RANK_NAMES = {
    1: "Silver I", 2: "Silver II", 3: "Silver III", 4: "Silver IV",
    5: "Silver Elite", 6: "Silver Elite Master", 7: "Gold Nova I",
    8: "Gold Nova II", 9: "Gold Nova III", 10: "Gold Nova Master",
    11: "Master Guardian I", 12: "Master Guardian II", 13: "Master Guardian Elite",
    14: "Distinguished Master Guardian", 15: "Legendary Eagle",
    16: "Legendary Eagle Master", 17: "Supreme Master First Class",
    18: "The Global Elite",
}


def rank_label(rank_type, rank):
    """Display label for a single rank: Premier rating ("17,246"), Competitive /
    Wingman skill-group name, or None when unranked. Mirrors formatRank() in the
    match page."""
    if not rank:
        return None
    if rank_type == 11:                 # Premier - CS Rating number
        return f"{rank:,}"
    if rank_type in (12, 7):            # Competitive / Wingman skill group
        name = _COMP_RANK_NAMES.get(rank, f"Rank {rank}")
        return name + (" (WM)" if rank_type == 7 else "")
    return str(rank)


PREMIER_RANK_TYPE = 11   # 12 = Competitive skill group, 7 = Wingman


def premier_ranks_present(data):
    """True when this match carries at least one Premier CS Rating.

    The mode token in a match's filename is whatever was chosen at upload
    ('mm' by default), so it says what the *uploader* called the match. A
    `ranks[]` entry's `rank_type` comes out of the demo itself and is the
    match's own ranking mode, which makes it the better evidence of the two -
    a Premier rating cannot appear in a match that was not Premier.

    Deliberately ANY rather than the dominant type used by avg_rank(): rank
    rows are absent for unranked players, so a Premier match can surface only
    one or two ratings, and one is already proof. Nothing else in a CS2 match
    produces rank_type 11.
    """
    for r in (data.get('ranks') or []):
        if isinstance(r, dict) and r.get('rank_type') == PREMIER_RANK_TYPE and r.get('rank'):
            return True
    return False


# Mode tokens the DEMO decided rather than the uploader: cmd/parser reads the
# server name out of the header, so 'faceit' is already the match's own account
# of where it was played and nothing in ranks[] can outrank it. Everything else
# is a default the Premier check below is there to correct.
DEMO_DECIDED_MODES = ('faceit',)


def mode_label(data, filename_mode):
    """The mode a match should be LABELLED with, uppercased ('FACEIT' / 'PREM' / 'MM').

    Premier wins over the filename token for the reason above; anything else
    keeps the token, since nothing in the demo contradicts it. A FACEIT token
    is the exception: it came from the demo's own server name, and a FACEIT
    match is not a Valve Premier match whatever rank rows it happens to carry
    (in practice it carries none - every FACEIT player is rank_type -1).
    """
    token = (filename_mode or '').strip()
    if token.lower() in DEMO_DECIDED_MODES:
        return token.upper()
    if premier_ranks_present(data):
        return 'PREM'
    return token.upper()


def avg_rank(data):
    """Average match rank across ranked players (dashboard column).

    `ranks` mixes rank types (11=Premier rating, 12=Competitive, 7=Wingman); a
    single match is normally one mode, so we average within the *dominant* rank
    type and format accordingly. Returns
    {rankType, avg, label, sort, n}: `label` is a Premier rating ("16,300") or a
    competitive skill-group name; `sort` is a 0-1 cross-type strength so the
    column sorts sensibly even in a mixed Premier/Competitive corpus.
    """
    by_type = {}
    for r in (data.get('ranks') or []):
        rk = r.get('rank') or 0
        if rk > 0:
            by_type.setdefault(r.get('rank_type') or 0, []).append(rk)
    if not by_type:
        return {'rankType': 0, 'avg': 0, 'label': '', 'sort': 0, 'n': 0}

    rt = max(by_type, key=lambda t: len(by_type[t]))   # dominant mode this match
    vals = by_type[rt]
    avg = sum(vals) / len(vals)

    if rt == 11:                        # Premier rating
        label = f"{round(avg):,}"
        sort = min(avg, 30000) / 30000
    elif rt in (12, 7):                 # Competitive / Wingman skill group (1-18)
        label = _COMP_RANK_NAMES.get(round(avg), f"Rank {round(avg)}")
        if rt == 7:
            label += " (WM)"
        sort = avg / 18
    else:
        label, sort = f"{avg:.0f}", 0

    return {'rankType': rt, 'avg': round(avg, 1), 'label': label,
            'sort': round(sort, 4), 'n': len(vals)}


def compute_player_rows(data):
    """Full per-player roster for one match: [{name, kills, deaths, assists}].
    Feeds the cross-match Players aggregate (dashboard_plan.md Phase 6)."""
    kills = data.get('kills') or []
    rows = {}   # name -> {kills, deaths, assists}

    def row(name):
        return rows.setdefault(name, {'kills': 0, 'deaths': 0, 'assists': 0})

    for k in kills:
        an, vn = k.get('attacker_name'), k.get('victim_name')
        asr = k.get('assister_name')
        if an and an != vn:
            row(an)['kills'] += 1
        if vn:
            row(vn)['deaths'] += 1
        if asr and asr != an:
            row(asr)['assists'] += 1

    return [{'name': n, **v} for n, v in rows.items()]


def _norm_side(s):
    s = (s or '').lower()
    return 'ct' if s == 'ct' else 't' if s in ('t', 'terrorist') else None


def _first_half_side_map(data):
    """name -> 'ct'|'t' in the FIRST-HALF frame (fixed team identity), derived
    from the side-carrying kill/damage events and translated back through the
    halftime swap. Mirrors the JS ensureSideMap() in match.html (§5)."""
    m = {}

    def note(name, side, r_num):
        s = _norm_side(side)
        if not name or not s or r_num is None or name in m:
            return
        m[name] = ('t' if s == 'ct' else 'ct') if _is_swapped_at_round(r_num) else s

    for k in (data.get('kills') or []):
        note(k.get('attacker_name'), k.get('attacker_side'), k.get('round_num'))
        note(k.get('victim_name'), k.get('victim_side'), k.get('round_num'))
    for d in (data.get('damage') or []):
        note(d.get('attacker_name'), d.get('attacker_side'), d.get('round_num'))
        note(d.get('victim_name'), d.get('victim_side'), d.get('round_num'))
    return m


def _winner_team(data):
    """1 = the first-half-CT team won, 2 = first-half-T team won, 0 = draw."""
    team_ct = team_t = 0
    for r in (data.get('rounds') or []):
        r_num = r.get('round_num') or 0
        w = (r.get('winner_side') or r.get('winner') or '').lower()
        if not _is_swapped_at_round(r_num):
            if w == 'ct':
                team_ct += 1
            elif w in ('t', 'terrorist'):
                team_t += 1
        else:
            if w in ('t', 'terrorist'):
                team_ct += 1
            elif w == 'ct':
                team_t += 1
    return 1 if team_ct > team_t else 2 if team_t > team_ct else 0


def compute_player_profile_rows(data):
    """Rich per-player roster for one match, feeding the cross-match player
    profiles (ui_feature_expansion_plan.md §6). Per player:
      name, steam_id, kills, deaths, assists, hs, dmg, kast_rounds,
      team (1|2 fixed identity), win (True/False/None on a draw),
      rank_type, rank, weapons {weapon: [shots, hits, kills]}.
    All derivable from the master JSON (no tick chunks): weapon accuracy uses
    shots (bullets fired) vs damage events (hits); 'weapons used' = ≥1 shot.
    `kast_rounds` mirrors match.html's client-side KAST tally (a round counts
    if the player got a Kill or Assist that round, or Survived it) so the
    cross-match player profile can compute a real KAST% and Rating 2.0
    without re-deriving it from raw events a second time."""
    fh = _first_half_side_map(data)
    winner = _winner_team(data)

    steam_by_name, rank_by_name = {}, {}
    for r in (data.get('ranks') or []):
        n = r.get('name')
        if not n:
            continue
        if r.get('steam_id'):
            steam_by_name[n] = str(r['steam_id'])
        rank_by_name[n] = (r.get('rank_type'), r.get('rank'))

    rows = {}

    def row(name):
        r = rows.get(name)
        if r is None:
            team = 1 if fh.get(name) == 'ct' else 2 if fh.get(name) == 't' else 0
            rt, rk = rank_by_name.get(name, (None, None))
            r = rows[name] = {
                'name': name, 'steam_id': steam_by_name.get(name),
                'kills': 0, 'deaths': 0, 'assists': 0, 'hs': 0, 'dmg': 0,
                'kast_rounds': 0,
                'team': team,
                'win': None if (winner == 0 or team == 0) else (winner == team),
                'rank_type': rt, 'rank': rk,
                'weapons': {},
            }
        return r

    def wpn(r, w):
        return r['weapons'].setdefault((w or 'unknown').lower(), [0, 0, 0])  # [shots, hits, kills]

    for k in (data.get('kills') or []):
        an, vn, asr = k.get('attacker_name'), k.get('victim_name'), k.get('assister_name')
        if an and an != vn:
            r = row(an); r['kills'] += 1
            if k.get('headshot'):
                r['hs'] += 1
            wpn(r, k.get('weapon'))[2] += 1
        if vn:
            row(vn)['deaths'] += 1
        if asr and asr != an:
            row(asr)['assists'] += 1

    # Clamp each hit to the victim's remaining health at that instant (a
    # killing blow's raw dmg_health can exceed the HP actually left) so ADR
    # matches the HLTV/csstats-style "normalized" convention instead of
    # summing the game's raw, sometimes-overkill damage values.
    lives = {}
    for d in (data.get('damage') or []):
        an, vn = d.get('attacker_name'), d.get('victim_name')
        if an and vn and an != vn:
            lives.setdefault((d.get('round_num'), vn), []).append(d)
    for evs in lives.values():
        evs.sort(key=lambda d: d.get('tick', 0) or 0)
        health = 100
        for d in evs:
            raw = d.get('dmg_health', 0) or 0
            actual = max(0, min(raw, health))
            health = max(0, health - raw)
            r = row(d['attacker_name']); r['dmg'] += actual
            wpn(r, d.get('weapon'))[1] += 1

    for s in (data.get('shots') or []):
        pn = s.get('player_name')
        if pn:
            wpn(row(pn), s.get('weapon'))[0] += 1

    # KAST rounds - mirrors match.html's kastRounds Set: a round counts if the
    # player got a Kill or Assist that round, or wasn't a kill victim (i.e.
    # Survived) that round. Only players already known from the loops above
    # are credited (no new rows created here).
    kast_rounds = {name: set() for name in rows}
    dead_by_round = {}
    for k in (data.get('kills') or []):
        rnum = k.get('round_num')
        if rnum is None:
            continue
        an, vn, asr = k.get('attacker_name'), k.get('victim_name'), k.get('assister_name')
        if an and an != vn and an in kast_rounds:
            kast_rounds[an].add(rnum)
        if asr and asr != an and asr in kast_rounds:
            kast_rounds[asr].add(rnum)
        if vn:
            dead_by_round.setdefault(rnum, set()).add(vn)
    for r in (data.get('rounds') or []):
        rnum = r.get('round_num')
        if rnum is None:
            continue
        dead = dead_by_round.get(rnum, set())
        for name in rows:
            if name not in dead:
                kast_rounds[name].add(rnum)
    for name, r in rows.items():
        r['kast_rounds'] = len(kast_rounds[name])

    return list(rows.values())
