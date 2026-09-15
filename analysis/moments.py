"""Moment index: one flat, pre-derived, queryable record per interesting thing
that happened in a match - every kill (from both sides), every piece of
utility, every round.

This is the data layer behind the Multi Match Analyser's query engine
(analysis/query.py). It exists because the interesting questions are never
about raw event fields - nobody asks "kills where wallbang is true", they ask
"opening duels I lost on the A site while we were down a man". Those
predicates need *derived* context (who was alive, was the bomb down, was this
a trade, how far into the round) which the parser doesn't record and which is
far too expensive to recompute per query. So it's computed once per match and
cached.

Sources entirely from the already-parsed match JSON - no 2nd-pass extractor,
no .dem, no tick chunks. Same posture as analysis/utility_rating.py.

Two hard rules, both about size:

1. **Never touch `grenades[]`.** It is ~205k trajectory points, 27.8 MB of a
   ~29 MB decompressed match - 95% of the document - and contains nothing a
   query would ever filter on. Callers that load the match themselves should
   drop the key before handing it over (app.py's /data/slim route does the
   same thing for the browser).
2. **Build one match at a time.** 16 matches held simultaneously is ~450 MB.
   The resulting index is ~150 KB per match, so the *indexes* can all live in
   RAM at once; the source documents cannot.

Perspective: a single kill is materialised as **two** rows - one `kind="kill"`
(subject = the attacker) and one `kind="death"` (subject = the victim). It
doubles the row count, which is nothing at this corpus size (2.3k kills →
~4.7k rows), and it buys a query engine with no perspective-projection logic:
every row is self-describing, `player` always means "whose moment is this",
and every derived attribute is already computed from the subject's point of
view. Asking for both kinds at once therefore returns two rows per kill, which
is the correct answer to "show me every kill and every death".
"""

import bisect
import math

import maps
import match_summary
from . import mapzones
from .clipwindow import clip_window_for_kill, clip_window_for_kill_span

# Bump when the row shape changes - every cached index older than this is
# rebuilt (same contract as app.py's PLAYERS_INDEX_VERSION / SUMMARY_SCHEMA).
# 2: `map`/`map_key` hold canonical map names (maps.canonical).
# 3: teamplay - `attrs.mates`/`attrs.opps` on every row, the trade/support/
#    execute/crossfire kinds, a real `attrs.distance`, and a death row's
#    `trade` finally meaning "my death was avenged".
# 5: `z` alongside `x`/`y` on every positioned row, and `attrs.zone` - Valve's
#    own named callout region for that position, from analysis/mapzones.py -
#    plus the `has_zones` cap.
MOMENTS_SCHEMA = 5

TICKRATE = 64
TRADE_WINDOW_TICKS = 5 * TICKRATE      # a death avenged within 5 s is a trade
DEFAULT_TEAM_SIZE = 5
EARLY_ROUND_SECS = 15                  # opening phase of a live round
LATE_ROUND_SECS = 95                   # last stretch before the round times out

# ── teamplay thresholds ───────────────────────────────────────────────────────
# Every number here was set by measuring what it produces over the whole
# corpus (18 matches, 363 rounds), not by picking a value that sounds right,
# and the rate each one yields is recorded beside it. That is the only way a
# later change that quietly floods or empties the index shows up as wrong: a
# detector that fires ten times a round and one that fires twice a match both
# look perfectly plausible from the outside.
SUPPORT_WINDOW_TICKS = 5 * TICKRATE     # flash -> teammate's kill    0.20/round
CROSSFIRE_WINDOW_TICKS = 3 * TICKRATE   # two teammates, one enemy    1.43/round
EXECUTE_WINDOW_TICKS = 6 * TICKRATE     # span of one utility burst   0.60/round
EXECUTE_MIN_UTIL = 3                    # pieces of utility in the burst
EXECUTE_MIN_PLAYERS = 2                 # distinct throwers
EXECUTE_MAX_SPREAD = 700.0              # world units from the burst's centroid

# Weapon class, keyed on the parser's `weapon_*` strings (verified against the
# whole corpus). Anything unknown falls back to "other" rather than guessing,
# so a new weapon shows up as its own bucket instead of silently joining one.
_WEAPON_CLASS = {
    'ak47': 'rifle', 'm4a4': 'rifle', 'm4a1_silencer': 'rifle', 'galilar': 'rifle',
    'famas': 'rifle', 'sg556': 'rifle', 'aug': 'rifle',
    'awp': 'sniper', 'ssg08': 'sniper', 'scar20': 'sniper', 'g3sg1': 'sniper',
    'mp9': 'smg', 'mac10': 'smg', 'mp7': 'smg', 'mp5sd': 'smg', 'p90': 'smg',
    'ump45': 'smg', 'bizon': 'smg',
    'usps': 'pistol', 'glock': 'pistol', 'deagle': 'pistol', 'p250': 'pistol',
    'tec9': 'pistol', 'fiveseven': 'pistol', 'p2000': 'pistol', 'cz75a': 'pistol',
    'dual_berettas': 'pistol', 'revolver': 'pistol',
    'xm1014': 'shotgun', 'mag7': 'shotgun', 'nova': 'shotgun', 'sawedoff': 'shotgun',
    'negev': 'lmg', 'm249': 'lmg',
    'hegrenade': 'nade', 'molotov': 'nade', 'incgrenade': 'nade', 'inferno': 'nade',
    'knife': 'knife', 'bayonet': 'knife',
    'zeus_x27': 'zeus', 'c4': 'bomb', 'world': 'world',
}

UTIL_KINDS = ('smoke', 'molotov', 'flash', 'he')


def weapon_class(weapon):
    """'weapon_ak47' -> 'rifle'. Unknown weapons become 'other', never a guess."""
    w = (weapon or '').lower()
    if w.startswith('weapon_'):
        w = w[len('weapon_'):]
    if not w:
        return 'other'
    if 'knife' in w or 'bayonet' in w:
        return 'knife'
    return _WEAPON_CLASS.get(w, 'other')


def weapon_label(weapon):
    """Short display name: 'weapon_m4a1_silencer' -> 'M4A1-S'."""
    w = (weapon or '').lower()
    if w.startswith('weapon_'):
        w = w[len('weapon_'):]
    special = {'m4a1_silencer': 'M4A1-S', 'usps': 'USP-S', 'ssg08': 'SSG 08',
               'hegrenade': 'HE', 'incgrenade': 'Incendiary', 'ak47': 'AK-47',
               'dual_berettas': 'Dualies', 'zeus_x27': 'Zeus', 'galilar': 'Galil',
               'world': 'World', 'mp5sd': 'MP5-SD'}
    return special.get(w, w.upper() if w else '')


def _norm_side(s):
    return match_summary._norm_side(s)


def classify_buy(equip, round_num):
    """Coarse buy type from a side's equipment value at freeze-time end.

    Thresholds are the conventional ones (eco < 5k, force < 10k, semi < 20k).
    Rounds 1 and 13 are pistol rounds by definition, so they're labelled as
    such regardless of value - a pistol round is not an "eco", and conflating
    them makes every eco-round query wrong for two rounds a match.
    """
    if round_num in (1, match_summary.REGULATION_HALF + 1):
        return 'pistol'
    if equip is None:
        return None
    if equip < 5000:
        return 'eco'
    if equip < 10000:
        return 'force'
    if equip < 20000:
        return 'semi'
    return 'full'


def round_lookup(rounds):
    """tick -> round_num resolver for the events the parser doesn't stamp with
    one (blind[], shots[], and on older parses the utility/bomb events too).

    Binary search over round spans, so it's O(log n) per event rather than the
    O(n) scan three separate copies of this used to do (multi.js tickToRoundM,
    match.html tickToRound, utility_rating). Ticks before the first round or in
    the gap after a round's end resolve to the nearest preceding round, which
    is what "which round did this happen in" means for a post-round grenade.
    """
    spans = sorted(((r.get('start') or 0), r.get('round_num'))
                   for r in rounds if r.get('round_num') is not None)
    starts = [s for s, _ in spans]
    nums = [n for _, n in spans]

    def lookup(tick):
        if tick is None or not starts:
            return None
        i = bisect.bisect_right(starts, tick) - 1
        return nums[i] if i >= 0 else nums[0]

    return lookup


def _round_num_of(row, lookup, tick_key='tick'):
    """A row's round: the parser's own `round_num` when present (added to the
    utility/bomb/blind/shot events in the 2026-08 re-parse), else resolved from
    the tick. Keeping both paths means old and new data index identically."""
    rn = row.get('round_num')
    return rn if rn is not None else lookup(row.get(tick_key))


def _team_of(first_half_side):
    """Fixed team identity (1 = first-half CT, 2 = first-half T) - stable
    across the halftime swap, unlike the literal side, which is why it takes
    no round number: that's the whole point of the fixed identity."""
    return 1 if first_half_side == 'ct' else 2 if first_half_side == 't' else 0


def roster_side_map(match_data, lookup):
    """name -> first-half side, from EVERY side-carrying event, not just kills.

    `match_summary._first_half_side_map` reads kills[] and damage[] only. That
    is enough for the scoreboard, but the moment index uses the roster to size
    each team, and an undersized team invents clutches: a player the roster
    never saw is indistinguishable from a dead one, so a survivor with two
    living teammates reads as the last man alive.

    That is why blind[] and shots[] are folded in as well - both carry a side,
    and someone who fired a bullet or got flashed is a player who existed even
    if they never traded a kill in the whole match.

    analysis/highlights.py's _round_rosters() takes its roster from here for
    the same reason, so the highlight reel and the moment index count a given
    round the same way rather than each deriving team sizes from whichever
    events they happen to read.
    """
    m = dict(match_summary._first_half_side_map(match_data))

    def note(name, side, rn):
        s = _norm_side(side)
        if not name or not s or rn is None or name in m:
            return
        m[name] = ('t' if s == 'ct' else 'ct') if match_summary._is_swapped_at_round(rn) else s

    for b in (match_data.get('blind') or []):
        rn = _round_num_of(b, lookup)
        note(b.get('attacker_name'), b.get('attacker_side'), rn)
        note(b.get('victim_name'), b.get('victim_side'), rn)
    for s in (match_data.get('shots') or []):
        note(s.get('player_name'), s.get('player_side'), _round_num_of(s, lookup))
    return m


def side_at_round(first_half_side, round_num):
    """Which side a player is on in `round_num`, given their first-half side.

    first_half_side: 'ct' | 't' | None      round_num: int | None
    -> 'ct' | 't' | None (None when either input is unknown)

    Applies the halftime swap, so it is how a fixed team identity is turned
    back into a per-round side. Public because highlights.py needs the same
    translation to build its round rosters.
    """
    if not first_half_side or round_num is None:
        return None
    if match_summary._is_swapped_at_round(round_num):
        return 't' if first_half_side == 'ct' else 'ct'
    return first_half_side


def build_match_index(match_data, meta):
    """Build the full moment index for one match.

    `meta` is app.py's _match_meta() dict - map/mode/date come from there and
    NOT from the match JSON, whose `mode`/`timestamp` fields are declared by
    the parser but empty on every match on disk (they're encoded in the
    filename instead). Getting this from the wrong place is a silent bug: it
    makes every mode filter match nothing.
    """
    rounds_raw = match_data.get('rounds') or []
    kills = sorted((match_data.get('kills') or []), key=lambda k: k.get('tick') or 0)
    damage = match_data.get('damage') or []
    blind = match_data.get('blind') or []

    lookup = round_lookup(rounds_raw)
    side_map = roster_side_map(match_data, lookup)

    # Roster size per side per round, so "5v4" is right even when a player
    # disconnects or the match is a 4v4. Counted from the fixed team identity
    # and translated through the swap, not from who happens to appear in the
    # round's kill events (which would call a quiet round a 0v0).
    team_sizes = {1: 0, 2: 0}
    for name, fh in side_map.items():
        t = _team_of(fh)
        if t:
            team_sizes[t] += 1
    for t in (1, 2):
        if not team_sizes[t]:
            team_sizes[t] = DEFAULT_TEAM_SIZE

    rounds, round_by_num = _build_rounds(rounds_raw, kills, damage, team_sizes)

    # Detected once, consumed twice: the kill rows name the teammate who set
    # them up, and _support_moments turns the same links into moments of their
    # own from the flasher's side.
    supports = _support_links(match_data, kills, side_map)
    util = _util_moments(match_data, lookup, round_by_num, side_map)
    _join_flash_support(util, supports)

    moments = []
    moments.extend(_kill_moments(kills, round_by_num, side_map, team_sizes, supports))
    moments.extend(_support_moments(supports, round_by_num, side_map))
    moments.extend(_crossfire_moments(damage, round_by_num, side_map))
    moments.extend(util)
    moments.extend(_execute_moments(util, kills, round_by_num, side_map,
                                    _site_centroids(match_data)))
    moments.extend(_round_moments(rounds))

    # Every row carries both player lists, `[]` when nobody else was involved.
    # Enforced in one place for the same reason the utility effect counters
    # are: otherwise "nobody helped" has two encodings - absent, and present
    # but empty - and every query has to spell both.
    for m in moments:
        m['attrs'].setdefault('mates', [])
        m['attrs'].setdefault('opps', [])

    moments.sort(key=lambda m: (m['round'] if m['round'] is not None else 0,
                                m['tick'] if m['tick'] is not None else 0))

    ranks = {r.get('name'): r for r in (match_data.get('ranks') or [])}
    players = []
    for name, fh in sorted(side_map.items()):
        rk = ranks.get(name) or {}
        players.append({
            'name': name,
            'steam_id': rk.get('steam_id'),
            'team': _team_of(fh),
            'rank_type': rk.get('rank_type'),
            'rank': rk.get('rank'),
        })

    has_coords = any(k.get('victim_x') is not None for k in kills)
    has_blind = bool(match_data.get('blind'))
    has_util_coords = any(
        u.get('X') is not None
        for key in ('smokes', 'infernos', 'flashes', 'he')
        for u in (match_data.get(key) or []))

    map_canon = maps.canonical(match_data.get('mapName') or meta.get('map'))
    zones_available = mapzones.has_zones(map_canon)
    if zones_available:
        for m in moments:
            if m.get('x') is not None and m.get('y') is not None:
                m['attrs']['zone'] = mapzones.zone_of(map_canon, m['x'], m['y'], m.get('z'))

    return {
        'v': MOMENTS_SCHEMA,
        'match': meta.get('file'),
        'stem': meta.get('stem'),
        # Both names are canonical (maps.canonical), so the analyser's map
        # facet, the scope filter and the heatmap's per-point map comparison
        # all key on the same string as the rest of the app. `map` comes from
        # the match document and `map_key` from the filename, which agree in
        # practice but are kept separate because only the filename is
        # guaranteed present.
        'map': map_canon,
        'map_key': maps.canonical(meta.get('map')),
        'mode': meta.get('mode'),
        'played_epoch': meta.get('played_epoch'),
        'played_label': meta.get('played_label'),
        'demo': meta.get('demo'),
        'tickrate': TICKRATE,
        'team_sizes': team_sizes,
        # Feature gates: which optional filters this match can actually answer.
        # A match parsed before kill coordinates existed must not silently
        # answer "no" to a zone query - the UI hides the filter instead.
        # Feature gates. `blind_pairs` is what makes flash support answerable
        # at all, and `util_coords` the execute geometry - without them the UI
        # hides those filters rather than letting them quietly answer "none".
        # Note `assist` deliberately has no cap and cannot have one:
        # `assister_name` is omitempty, so "nobody assisted" and "parsed before
        # the field existed" are byte-identical. `mates` inherits that.
        # `has_zones` gates the Zone filter itself: it's per-MAP, not per-match
        # (a map with no extracted callout regions yet, not an old parse).
        'caps': {'kill_coords': has_coords,
                 'blind_pairs': has_blind,
                 'util_coords': has_util_coords,
                 'has_zones': zones_available},
        'players': players,
        'rounds': rounds,
        'moments': moments,
    }


def _build_rounds(rounds_raw, kills, damage, team_sizes):
    """Round context rows - the join table every moment attaches to."""
    first_dmg = {}
    for d in damage:
        rn = d.get('round_num')
        t = d.get('tick')
        if rn is None or t is None:
            continue
        if rn not in first_dmg or t < first_dmg[rn]:
            first_dmg[rn] = t
    first_kill = {}
    for k in kills:
        rn = k.get('round_num')
        t = k.get('tick')
        if rn is None or t is None:
            continue
        if rn not in first_kill or t < first_kill[rn]:
            first_kill[rn] = t

    rounds, by_num = [], {}
    for r in rounds_raw:
        rn = r.get('round_num')
        if rn is None:
            continue
        eco = r.get('economy') or {}
        winner = _norm_side(r.get('winner_side') or r.get('winner'))
        swapped = match_summary._is_swapped_at_round(rn)
        # Fixed team identity of the winner: on a swapped round the literal
        # side belongs to the other team.
        if winner == 'ct':
            winner_team = 2 if swapped else 1
        elif winner == 't':
            winner_team = 1 if swapped else 2
        else:
            winner_team = 0
        row = {
            'n': rn,
            'start': r.get('start'),
            'freeze_end': r.get('freeze_end'),
            'end': r.get('end'),
            'winner': winner,
            'winner_team': winner_team,
            'reason': r.get('reason'),
            'bomb_plant': r.get('bomb_plant'),
            'bomb_site': _site_label(r.get('bomb_site')),
            'half': 2 if swapped else 1,
            'ct_equip': eco.get('ct_equip'),
            't_equip': eco.get('t_equip'),
            'buy_ct': classify_buy(eco.get('ct_equip'), rn),
            'buy_t': classify_buy(eco.get('t_equip'), rn),
            'first_contact': first_dmg.get(rn, first_kill.get(rn)),
            'team_sizes': dict(team_sizes),
        }
        rounds.append(row)
        by_num[rn] = row
    return rounds, by_num


def _site_label(site):
    """'bombsite_a' / 'BombsiteA' / 'A' -> 'A'.

    The parser writes the literal 'not_planted' on rounds where the bomb never
    went down (169 of 320 rounds in the current corpus). That is the absence of
    a site, not a third site, and passing it through would put a NOT_PLANTED
    bucket in the bomb-site facet - use `bomb_planted` for that question."""
    s = (site or '').strip().lower()
    if not s or s in ('not_planted', 'none', 'unknown'):
        return None
    if s.endswith('a'):
        return 'A'
    if s.endswith('b'):
        return 'B'
    return s.upper()


def _kill_moments(kills, round_by_num, side_map, team_sizes, supports=None):
    """Every kill, as an attacker-perspective row and a victim-perspective row,
    plus a `trade` row for each kill that avenged a teammate.

    All the derived context is computed here, once, in a single ordered pass
    per round: alive counts, opening/trade links, clutch state, multi-kill
    index. Those are exactly the attributes a useful query needs and the raw
    event has none of.

    `supports` is the flash-support link list from _support_links(), joined in
    so a kill row can name the teammate whose flash set it up. It is passed in
    rather than recomputed because _support_moments() materialises rows from
    the same records - one detection, two consumers.
    """
    by_kill = {}
    for link in (supports or []):
        by_kill.setdefault((link['kill_tick'], link['killer'], link['victim']),
                           []).append(link['flasher'])

    by_round = {}
    for k in kills:
        by_round.setdefault(k.get('round_num'), []).append(k)

    out = []
    for rn, rkills in by_round.items():
        rd = round_by_num.get(rn) or {}
        r_start = rd.get('start')
        r_end = rd.get('end')
        freeze_end = rd.get('freeze_end') if rd.get('freeze_end') is not None else r_start
        plant = rd.get('bomb_plant')
        sizes = rd.get('team_sizes') or team_sizes

        # Alive counts are tracked by fixed team identity, then reported in
        # side terms for the moment - so a 5v4 stays a 5v4 across halftime.
        # Who avenged whom, looking FORWARD. `trade` below answers "did this
        # kill avenge a teammate" - a property of the kill, true from both
        # sides of it. It cannot answer "was MY death avenged", because that
        # answer lies in kills which have not happened yet, and conflating the
        # two is the defect this map exists to fix.
        avenged = _avenged_map(rkills, side_map)

        alive = {1: sizes.get(1, DEFAULT_TEAM_SIZE), 2: sizes.get(2, DEFAULT_TEAM_SIZE)}
        mk_count = {}
        # Deaths still inside the trade window, newest last.
        recent_deaths = []
        opener = rkills[0] if rkills else None

        for i, k in enumerate(rkills):
            tick = k.get('tick')
            atk, vic = k.get('attacker_name'), k.get('victim_name')
            atk_side = _norm_side(k.get('attacker_side'))
            vic_side = _norm_side(k.get('victim_side'))
            atk_team = _team_of(side_map.get(atk)) if atk else 0
            vic_team = _team_of(side_map.get(vic)) if vic else 0
            suicide = bool(k.get('suicide')) or (atk and atk == vic) or not atk

            alive_before = dict(alive)
            # "Was this kill a trade?" - did the attacker avenge a teammate who
            # died to this victim within the window. Checked before the death
            # list is updated with this kill.
            trade_of = None
            trade_partner = None
            for d in reversed(recent_deaths):
                if tick is not None and d['tick'] is not None and tick - d['tick'] > TRADE_WINDOW_TICKS:
                    break
                if d['team'] == atk_team and d['killer'] == vic:
                    trade_of = d['tick']
                    trade_partner = d['name']
                    break

            if not suicide and atk:
                mk_count[atk] = mk_count.get(atk, 0) + 1

            # Clutch: the victim's death leaves exactly one player alive on a
            # side that still faces opponents. Recorded from the surviving
            # side's perspective; who that player is can't be known from
            # counts alone, so it's resolved below from the live set.
            if vic_team in (1, 2):
                alive[vic_team] = max(0, alive[vic_team] - 1)

            ctx = {
                'round': rn, 'tick': tick,
                'weapon': k.get('weapon'),
                'weapon_class': weapon_class(k.get('weapon')),
                'weapon_label': weapon_label(k.get('weapon')),
                'hs': bool(k.get('headshot')),
                'wallbang': bool(k.get('wallbang')),
                'through_smoke': bool(k.get('through_smoke')),
                'noscope': bool(k.get('noscope')),
                'attacker_blind': bool(k.get('attacker_blind')),
                'attacker_air': bool(k.get('attacker_air')),
                'assist': k.get('assister_name') or None,
                'flash_assist': bool(k.get('assist_flash')),
                'suicide': suicide,
                'opening': k is opener and not suicide,
                'trade': trade_of is not None,
                'bomb_planted': plant is not None and tick is not None and tick >= plant,
                'bomb_site': rd.get('bomb_site'),
                't_into_round': round((tick - freeze_end) / TICKRATE, 2)
                                if tick is not None and freeze_end is not None else None,
                't_since_plant': round((tick - plant) / TICKRATE, 2)
                                 if plant is not None and tick is not None and tick >= plant else None,
                'distance': _distance(k),
            }
            ctx['round_phase'] = _round_phase(ctx['t_into_round'], ctx['bomb_planted'])

            start_tick, end_tick = clip_window_for_kill(tick, r_start) if tick is not None else (None, None)

            # Who else this duel involved. A flash that made the kill possible
            # is teamplay on the attacker's own row as much as it is a moment
            # of its own, so the setter lands in `mates` here too.
            setters = by_kill.get((tick, atk, vic), [])
            av = avenged.get(i)
            assist = ctx['assist']

            if atk and not suicide:
                out.append(_kill_row('kill', ctx, rd, subject=atk, subject_side=atk_side,
                                     subject_team=atk_team, other=vic, other_side=vic_side,
                                     other_team=vic_team, alive=alive_before,
                                     mk=mk_count.get(atk, 1),
                                     x=k.get('attacker_x'), y=k.get('attacker_y'), z=k.get('attacker_z'),
                                     start_tick=start_tick, end_tick=end_tick,
                                     extra={
                                         'traded_for': trade_partner,
                                         'trade_latency': round((tick - trade_of) / TICKRATE, 2)
                                         if trade_of is not None and tick is not None else None,
                                         'set_up_by': setters[0] if setters else None,
                                         'mates': _names(assist, trade_partner, *setters),
                                         'opps': _names(vic),
                                     }))
            if vic:
                # `trade` stays exactly what it is on the kill row - a property
                # of the KILL, true from both sides of it ("I died to a trade"
                # is a real question). What was missing is the subject-relative
                # fact, so it gets its own name rather than overloading one:
                # `traded` = my death was avenged. The two are different
                # populations, and query.py ships a headline "Trade %" rate
                # whose denominator spans both kill and death rows - one field
                # meaning two things would blend them invisibly.
                out.append(_kill_row('death', ctx, rd, subject=vic, subject_side=vic_side,
                                     subject_team=vic_team, other=(atk if not suicide else None),
                                     other_side=atk_side, other_team=atk_team,
                                     alive=alive_before, mk=0,
                                     x=k.get('victim_x'), y=k.get('victim_y'), z=k.get('victim_z'),
                                     start_tick=start_tick, end_tick=end_tick,
                                     extra={
                                         'traded': av is not None,
                                         'traded_by': av['by'] if av else None,
                                         'traded_latency': av['latency'] if av else None,
                                         'mates': _names(av['by'] if av else None),
                                         'opps': _names(atk if not suicide else None, assist),
                                     }))

            if trade_partner and atk and not suicide:
                out.append(_trade_row(ctx, rd, atk, atk_side, atk_team, vic, vic_side,
                                      vic_team, trade_partner, trade_of, tick,
                                      k.get('attacker_x'), k.get('attacker_y'), k.get('attacker_z'),
                                      start_tick, end_tick, alive_before))

            recent_deaths.append({'tick': tick, 'team': vic_team, 'killer': atk, 'name': vic})
            if tick is not None:
                recent_deaths = [d for d in recent_deaths
                                 if d['tick'] is None or tick - d['tick'] <= TRADE_WINDOW_TICKS]

        out.extend(_clutch_moments(rn, rkills, rd, side_map, sizes, r_start, r_end))
        out.extend(_multikill_moments(rn, rkills, rd, side_map, r_start, r_end))
    return out


def _round_phase(t_into_round, bomb_planted):
    if t_into_round is None:
        return None
    if t_into_round < 0:
        return 'freeze'
    if bomb_planted:
        return 'postplant'
    if t_into_round < EARLY_ROUND_SECS:
        return 'early'
    if t_into_round < LATE_ROUND_SECS:
        return 'mid'
    return 'late'


def _kill_row(kind, ctx, rd, subject, subject_side, subject_team, other, other_side,
              other_team, alive, mk, x, y, z, start_tick, end_tick, extra=None):
    """One perspective on a kill. Every attribute reads from the subject's
    point of view - `man_adv` is signed for them, `won_round` is their result -
    so no query ever has to ask "whose side am I looking at".

    `extra` is what the two perspectives do NOT share. `ctx` is built once per
    kill and handed to both calls, so anything subject-relative put in there
    silently describes the wrong player on one of the two rows - which is
    exactly how a death row came to claim it had been traded when what had
    happened was that its killer was avenging someone. Subject-relative
    attributes travel here and are merged in below.
    """
    own = alive.get(subject_team, 0) if subject_team else 0
    opp = alive.get(3 - subject_team, 0) if subject_team in (1, 2) else 0
    won = (rd.get('winner_team') == subject_team) if subject_team and rd.get('winner_team') else None
    label = ('Kill (%s)' % ctx['weapon_label']) if kind == 'kill' else ('Death (%s)' % ctx['weapon_label'])
    _attrs = _kill_attrs(ctx, rd, subject_side, own, opp, mk, won, extra)
    return {
        'kind': kind,
        'round': ctx['round'],
        'tick': ctx['tick'],
        'start_tick': start_tick,
        'end_tick': end_tick,
        'player': subject,
        'side': subject_side,
        'team': subject_team,
        'other': other,
        'other_side': other_side,
        'other_team': other_team,
        'x': x, 'y': y, 'z': z,
        'label': label,
        'detail': _kill_detail(kind, _attrs, other),
        'attrs': _attrs,
    }


def _kill_attrs(ctx, rd, subject_side, own, opp, mk, won, extra):
    return dict(
        ctx,
        alive_own=own, alive_opp=opp,
        situation='%dv%d' % (own, opp),
        man_adv=own - opp,
        multikill_n=mk,
        won_round=won,
        buy_self=rd.get('buy_ct') if subject_side == 'ct' else rd.get('buy_t'),
        buy_enemy=rd.get('buy_t') if subject_side == 'ct' else rd.get('buy_ct'),
        half=rd.get('half'),
        round_reason=rd.get('reason'),
        **(extra or {})
    )


def _kill_detail(kind, a, other):
    """The prose half of a row.

    The opponent's NAME is deliberately absent: the row builder renders it as
    its own team-tinted cell (momentrow.logic.js), and a name printed in both
    places is a name that will eventually disagree with itself.
    """
    bits = []
    if a.get('opening'):
        bits.append('opening duel')
    if kind == 'kill' and a.get('trade'):
        bits.append(('trade for %s' % a['traded_for']) if a.get('traded_for') else 'trade')
    if kind == 'death' and a.get('traded'):
        bits.append(('traded by %s' % a['traded_by']) if a.get('traded_by') else 'traded')
    if a.get('set_up_by'):
        bits.append('flashed by %s' % a['set_up_by'])
    if a.get('hs'):
        bits.append('headshot')
    for key, text in (('wallbang', 'wallbang'), ('through_smoke', 'through smoke'),
                      ('noscope', 'noscope'), ('attacker_blind', 'attacker blind')):
        if a.get(key):
            bits.append(text)
    return ', '.join(bits)


# ── teamplay helpers ──────────────────────────────────────────────────────────

def _names(*values):
    """A moment's player list: the names given, blanks dropped, de-duplicated,
    order preserved.

    Every row carries `mates` and `opps`, `[]` when nobody else was involved -
    so "nobody helped" has exactly one encoding rather than two (absent, and
    present-but-empty), the same rule the utility effect counters follow.
    `opps` is a superset of `other`: one field then answers "against B" for a
    kill, a death, a clutch, a multikill and a flash alike, which is the whole
    point of having it.
    """
    out = []
    for v in values:
        if v and v not in out:
            out.append(v)
    return out


def _distance(k):
    """How far apart the two players were, in world units.

    Derived here rather than read off the event: the parser has never emitted
    a `distance` field, so the old `k.get('distance')` was always None while
    the UI offered the filter and aggregate() advertised a mean for it that
    could never populate. Three-dimensional on purpose - "how far was that
    shot" is a 3-D question on Nuke and Vertigo, even though every other
    geometry in this app is flat. None when coordinates are absent (an old
    parse), never 0: 0 is a legal distance.
    """
    ax, ay = k.get('attacker_x'), k.get('attacker_y')
    vx, vy = k.get('victim_x'), k.get('victim_y')
    if ax is None or ay is None or vx is None or vy is None:
        return None
    az, vz = k.get('attacker_z'), k.get('victim_z')
    dz = (az - vz) if az is not None and vz is not None else 0.0
    return round(math.sqrt((ax - vx) ** 2 + (ay - vy) ** 2 + dz ** 2), 1)


def _avenged_map(rkills, side_map):
    """`{kill index: {by, tick, latency}}` - for each kill, whether the VICTIM
    was avenged by one of their own teammates inside the trade window.

    A death is avenged at most once: the first qualifying kill is the trade,
    anything later is just another kill. Suicides are neither traded nor
    trades.

    Note the counts on the two sides do NOT match, and that is correct rather
    than a bug to chase: one kill can clear more than one teammate's death, so
    over the corpus 395 trade kills avenge 421 deaths (25 of those kills
    cleared two, one cleared three). "How often did I trade" and "how often was
    I traded" are different questions about the same events.
    """
    out = {}
    for i, k in enumerate(rkills):
        vic, killer, t = k.get('victim_name'), k.get('attacker_name'), k.get('tick')
        if not vic or not killer or killer == vic or t is None or k.get('suicide'):
            continue
        vic_team = _team_of(side_map.get(vic))
        if not vic_team:
            continue
        for k2 in rkills[i + 1:]:
            t2 = k2.get('tick')
            if t2 is None:
                continue
            if t2 - t > TRADE_WINDOW_TICKS:
                break
            if k2.get('victim_name') == killer and not k2.get('suicide') \
                    and _team_of(side_map.get(k2.get('attacker_name'))) == vic_team:
                out[i] = {'by': k2.get('attacker_name'), 'tick': t2,
                          'latency': round((t2 - t) / TICKRATE, 2)}
                break
    return out


def _round_ctx(rd, tick, team, side):
    """The round-level attributes every moment carries, from the subject's
    point of view.

    Factored out because the teamplay kinds need exactly the block the kill
    and utility rows already build, and four copies of it would be four
    chances for one to drift out of step with the others.
    """
    freeze_end = rd.get('freeze_end') if rd.get('freeze_end') is not None else rd.get('start')
    plant = rd.get('bomb_plant')
    planted = plant is not None and tick is not None and tick >= plant
    t_into = round((tick - freeze_end) / TICKRATE, 2) \
        if tick is not None and freeze_end is not None else None
    return {
        't_into_round': t_into,
        't_since_plant': round((tick - plant) / TICKRATE, 2) if planted else None,
        'round_phase': _round_phase(t_into, planted),
        'bomb_planted': planted,
        'bomb_site': rd.get('bomb_site'),
        'half': rd.get('half'),
        'round_reason': rd.get('reason'),
        'won_round': (rd.get('winner_team') == team) if team and rd.get('winner_team') else None,
        'buy_self': rd.get('buy_ct') if side == 'ct' else rd.get('buy_t'),
        'buy_enemy': rd.get('buy_t') if side == 'ct' else rd.get('buy_ct'),
    }


def _trade_row(ctx, rd, atk, atk_side, atk_team, vic, vic_side, vic_team,
               partner, trade_of, tick, x, y, z, start_tick, end_tick, alive):
    """A trade, as a teamplay moment in its own right.

    Same event as the `kill` row beside it, seen as a thing two players did
    together rather than a thing one player did: the subject is still the
    trader, but `partner` names the teammate whose death was avenged, which is
    the question actually being asked. Deliberately NOT part of the `duel`
    unit, so asking for kills and deaths never counts this event a third time.
    """
    latency = round((tick - trade_of) / TICKRATE, 2) \
        if trade_of is not None and tick is not None else None
    own = alive.get(atk_team, 0) if atk_team else 0
    opp = alive.get(3 - atk_team, 0) if atk_team in (1, 2) else 0
    return {
        'kind': 'trade', 'round': ctx['round'], 'tick': tick,
        'start_tick': start_tick, 'end_tick': end_tick,
        'player': atk, 'side': atk_side, 'team': atk_team,
        'other': vic, 'other_side': vic_side, 'other_team': vic_team,
        'x': x, 'y': y, 'z': z,
        'label': 'Trade',
        'detail': ('traded after %.1fs' % latency) if latency is not None else 'traded',
        'attrs': dict(_round_ctx(rd, tick, atk_team, atk_side),
                      round=ctx['round'], tick=tick,
                      weapon=ctx['weapon'], weapon_class=ctx['weapon_class'],
                      weapon_label=ctx['weapon_label'], hs=ctx['hs'],
                      partner=partner, partner_team=atk_team,
                      trade_latency=latency,
                      alive_own=own, alive_opp=opp,
                      situation='%dv%d' % (own, opp), man_adv=own - opp,
                      mates=_names(partner), opps=_names(vic)),
    }


def _live_sets(rkills, side_map, sizes):
    """Walk a round's kills and yield, after each death, the set of players
    still alive per fixed team. The roster is the match roster restricted to
    players who appear in this round's events plus everyone on their team, so
    a clutch is detected against the real remaining count."""
    dead = set()
    rosters = {1: set(), 2: set()}
    for name, fh in side_map.items():
        t = _team_of(fh)
        if t:
            rosters[t].add(name)
    for k in rkills:
        v = k.get('victim_name')
        if v:
            dead.add(v)
        yield k, {t: rosters[t] - dead for t in (1, 2)}


def _clutch_moments(rn, rkills, rd, side_map, sizes, r_start, r_end):
    """A 1vX: the tick one player became the last alive on their team while
    the other team still had someone. Emitted as an *attempt*, with `won`
    recording how it ended - losing clutches are as interesting as winning
    ones, and highlights.py only ever surfaced the wins."""
    out = []
    seen = set()          # teams that already have their attempt recorded
    for k, live in _live_sets(rkills, side_map, sizes):
        for t in (1, 2):
            other = 3 - t
            if t in seen:
                continue
            if len(live[t]) == 1 and len(live[other]) >= 1:
                seen.add(t)
                who = next(iter(live[t]))
                pivot = k.get('tick')
                last_tick = rkills[-1].get('tick') or pivot
                start_tick, end_tick = clip_window_for_kill_span(pivot, last_tick, r_start, r_end)
                won = (rd.get('winner_team') == t) if rd.get('winner_team') else None
                out.append({
                    'kind': 'clutch', 'round': rn, 'tick': pivot,
                    'start_tick': start_tick, 'end_tick': end_tick,
                    'player': who,
                    'side': side_at_round(side_map.get(who), rn),
                    'team': t, 'other': None, 'other_side': None, 'other_team': other,
                    'x': None, 'y': None, 'z': None,
                    'label': '1v%d clutch' % len(live[other]),
                    'detail': ('won' if won else 'lost' if won is False else '') +
                              (' - last player alive in round %d' % rn),
                    'attrs': {
                        'round': rn, 'tick': pivot,
                        'clutch_n': len(live[other]),
                        'won_round': won, 'won': won,
                        'opening': False, 'trade': False,
                        'bomb_planted': rd.get('bomb_plant') is not None and pivot is not None
                                        and pivot >= rd['bomb_plant'],
                        'bomb_site': rd.get('bomb_site'),
                        'half': rd.get('half'),
                        'round_reason': rd.get('reason'),
                        'situation': '1v%d' % len(live[other]),
                        'man_adv': 1 - len(live[other]),
                        # The names were right here all along - `live[other]`
                        # is who the clutcher actually had to beat, and it was
                        # reduced to a count and thrown away.
                        'opps': sorted(live[other]),
                        'mates': [],
                    },
                })
                # One attempt per team per round - the first moment they were
                # alone is the one that defines it. Both teams can qualify in
                # the same round (a 1v1 is two simultaneous clutch attempts),
                # which is why this keeps walking instead of returning.
    return out


def _multikill_moments(rn, rkills, rd, side_map, r_start, r_end):
    """2K/3K/4K/ACE per player per round, anchored to the whole span of their
    kills rather than the last one - the play is the sequence."""
    by_player = {}
    for k in rkills:
        a, v = k.get('attacker_name'), k.get('victim_name')
        if not a or a == v or k.get('suicide'):
            continue
        by_player.setdefault(a, []).append(k)
    labels = {2: '2K', 3: '3K', 4: '4K', 5: 'ACE'}
    out = []
    for name, ks in by_player.items():
        if len(ks) < 2:
            continue
        n = min(len(ks), 5)
        start_tick, end_tick = clip_window_for_kill_span(
            ks[0].get('tick'), ks[-1].get('tick'), r_start, r_end)
        team = _team_of(side_map.get(name))
        out.append({
            'kind': 'multikill', 'round': rn, 'tick': ks[-1].get('tick'),
            'start_tick': start_tick, 'end_tick': end_tick,
            'player': name, 'side': side_at_round(side_map.get(name), rn),
            'team': team, 'other': None, 'other_side': None, 'other_team': 0,
            'x': None, 'y': None, 'z': None,
            'label': labels.get(n, '%dK' % n),
            'detail': '%d kills in round %d' % (len(ks), rn),
            'attrs': {
                'round': rn, 'tick': ks[-1].get('tick'),
                'multikill_n': len(ks),
                # Same story as the clutch row: `ks` is the sequence, and only
                # its length survived.
                'opps': _names(*[x.get('victim_name') for x in ks]),
                'mates': _names(*[x.get('assister_name') for x in ks]),
                'won_round': (rd.get('winner_team') == team) if team and rd.get('winner_team') else None,
                'half': rd.get('half'), 'bomb_site': rd.get('bomb_site'),
                'round_reason': rd.get('reason'),
            },
        })
    return out


def _util_moments(match_data, lookup, round_by_num, side_map):
    """Every thrown smoke / molotov / flash / HE, with its *effect* joined in
    from blind[] and damage[].

    The effect is the whole point: "flashes that only blinded my own team" and
    "molotovs that dealt zero damage" are the questions worth asking, and
    neither is answerable from the utility event alone.
    """
    out = []
    blind = match_data.get('blind') or []
    damage = match_data.get('damage') or []

    def emit(kind, ev, tick_key, tick):
        rn = _round_num_of(ev, lookup, tick_key)
        rd = round_by_num.get(rn) or {}
        thrower = ev.get('thrower_name')
        fh = side_map.get(thrower)
        side = side_at_round(fh, rn)
        team = _team_of(fh)
        freeze_end = rd.get('freeze_end') if rd.get('freeze_end') is not None else rd.get('start')
        plant = rd.get('bomb_plant')
        attrs = {
            'round': rn, 'tick': tick, 'util_kind': kind,
            't_into_round': round((tick - freeze_end) / TICKRATE, 2)
                            if tick is not None and freeze_end is not None else None,
            'bomb_planted': plant is not None and tick is not None and tick >= plant,
            'bomb_site': rd.get('bomb_site'),
            'half': rd.get('half'),
            'round_reason': rd.get('reason'),
            'won_round': (rd.get('winner_team') == team) if team and rd.get('winner_team') else None,
            'buy_self': rd.get('buy_ct') if side == 'ct' else rd.get('buy_t'),
        }
        return {
            'kind': kind, 'round': rn, 'tick': tick,
            'start_tick': None, 'end_tick': None,
            'player': thrower, 'side': side, 'team': team,
            'other': None, 'other_side': None, 'other_team': 0,
            'x': ev.get('X'), 'y': ev.get('Y'), 'z': ev.get('Z'),
            'label': kind.capitalize(), 'detail': '',
            'attrs': attrs,
        }

    for s in (match_data.get('smokes') or []):
        out.append(emit('smoke', s, 'start_tick', s.get('start_tick')))
    for s in (match_data.get('infernos') or []):
        m = emit('molotov', s, 'start_tick', s.get('start_tick'))
        m['attrs']['weapon'] = s.get('weapon')
        out.append(m)
    for f in (match_data.get('flashes') or []):
        out.append(emit('flash', f, 'tick', f.get('tick')))
    for h in (match_data.get('he') or []):
        out.append(emit('he', h, 'tick', h.get('tick')))

    # Effect counters start at a real zero on every piece of utility rather
    # than appearing only once something was hit. Otherwise "dealt nothing"
    # has two different encodings - absent, and present-as-0 - and a query
    # has to spell both, which is exactly the kind of trap that makes a
    # filter quietly return the wrong set.
    for m in out:
        if m['kind'] == 'flash':
            m['attrs'].setdefault('enemies_flashed', 0)
            m['attrs'].setdefault('friends_flashed', 0)
            m['attrs'].setdefault('blind_time', 0.0)
            m['attrs'].setdefault('friendly_blind_time', 0.0)
        elif m['kind'] in ('he', 'molotov'):
            m['attrs'].setdefault('damage', 0)
            m['attrs'].setdefault('team_damage', 0)
            m['attrs'].setdefault('enemies_hit', 0)

    _join_flash_effect(out, blind, side_map)
    _join_he_effect(out, damage)
    for m in out:
        m['detail'] = _util_detail(m)
    return out


# A flash's blind[] entries land on the same tick as the detonation, give or
# take the couple of frames the game takes to apply them.
_FLASH_JOIN_TICKS = 16
# HE damage is dealt on the detonation frame; a small window absorbs the
# multi-frame application without picking up unrelated gunfire.
_HE_JOIN_TICKS = 12


def _join_flash_effect(moments, blind, side_map):
    flashes = sorted([m for m in moments if m['kind'] == 'flash'],
                     key=lambda m: m['tick'] or 0)
    if not flashes:
        return
    ticks = [m['tick'] or 0 for m in flashes]
    for b in blind:
        t = b.get('tick')
        if t is None:
            continue
        i = bisect.bisect_left(ticks, t - _FLASH_JOIN_TICKS)
        best = None
        while i < len(flashes) and (flashes[i]['tick'] or 0) <= t + _FLASH_JOIN_TICKS:
            if flashes[i]['player'] == b.get('attacker_name'):
                best = flashes[i]
                break
            i += 1
        if best is None:
            continue
        a = best['attrs']
        dur = b.get('blind_duration') or 0
        victim_fh = side_map.get(b.get('victim_name'))
        friendly = _team_of(victim_fh) == best['team'] and best['team'] != 0
        a['enemies_flashed'] = a.get('enemies_flashed', 0) + (0 if friendly else 1)
        a['friends_flashed'] = a.get('friends_flashed', 0) + (1 if friendly else 0)
        who = b.get('victim_name')
        bucket = a.setdefault('mates' if friendly else 'opps', [])
        if who and who not in bucket:
            bucket.append(who)
        if not friendly:
            a['blind_time'] = round(a.get('blind_time', 0.0) + dur, 2)
        else:
            a['friendly_blind_time'] = round(a.get('friendly_blind_time', 0.0) + dur, 2)


def _join_he_effect(moments, damage):
    hes = sorted([m for m in moments if m['kind'] in ('he', 'molotov')],
                 key=lambda m: m['tick'] or 0)
    if not hes:
        return
    ticks = [m['tick'] or 0 for m in hes]
    for d in damage:
        w = (d.get('weapon') or '').lower()
        if 'grenade' not in w and 'molotov' not in w and 'inferno' not in w:
            continue
        t = d.get('tick')
        if t is None:
            continue
        # Molotov damage ticks for seconds after ignition, so its window is the
        # fire's whole lifetime rather than the detonation frame.
        span = _HE_JOIN_TICKS if 'grenade' in w else 8 * TICKRATE
        i = bisect.bisect_left(ticks, t - span)
        best = None
        while i < len(hes) and (hes[i]['tick'] or 0) <= t + _HE_JOIN_TICKS:
            if hes[i]['player'] == d.get('attacker_name'):
                best = hes[i]
            i += 1
        if best is None:
            continue
        a = best['attrs']
        dmg = d.get('dmg_health') or 0
        friendly = _norm_side(d.get('victim_side')) == best['side']
        who = d.get('victim_name')
        bucket = a.setdefault('mates' if friendly else 'opps', [])
        if who and who not in bucket:
            bucket.append(who)
        if friendly:
            a['team_damage'] = a.get('team_damage', 0) + dmg
        else:
            a['damage'] = a.get('damage', 0) + dmg
            a['enemies_hit'] = a.get('enemies_hit', 0) + 1


def _support_links(match_data, kills, side_map):
    """Flashes that directly enabled a TEAMMATE's kill.

    A kill counts as enabled when the victim was still blind at the moment
    they died and the flash came from someone other than their killer. That
    exclusion is not a detail: 31 of the corpus's naive matches are a player
    flashing themselves into their own kill, which is skill, not teamplay.

    Derived from `blind[]` rather than taken from the Kill event's
    `assist_flash`, for two reasons the bool cannot cover. It says only THAT a
    flash assist happened, never whose flash it was - which is the entire
    question here. And the game credits at most one assister per kill, so a
    second teammate equally responsible is invisible to it. Deriving finds 74
    links against the game's 65: close enough to corroborate the method, wide
    enough to be worth doing.
    """
    blind = sorted([b for b in (match_data.get('blind') or [])
                    if b.get('tick') is not None], key=lambda b: b['tick'])
    if not blind or not kills:
        return []
    ticks = [b['tick'] for b in blind]
    out = []
    for k in kills:
        t, atk, vic = k.get('tick'), k.get('attacker_name'), k.get('victim_name')
        if t is None or not atk or not vic or atk == vic or k.get('suicide'):
            continue
        atk_team = _team_of(side_map.get(atk))
        if not atk_team:
            continue
        i = bisect.bisect_left(ticks, t - SUPPORT_WINDOW_TICKS)
        while i < len(blind) and blind[i]['tick'] <= t:
            b = blind[i]
            i += 1
            flasher = b.get('attacker_name')
            if b.get('victim_name') != vic or not flasher or flasher == atk:
                continue
            if _team_of(side_map.get(flasher)) != atk_team:
                continue
            # Still blind when they died. A flash that had already worn off
            # merely happened first; it did not enable anything.
            if t > b['tick'] + (b.get('blind_duration') or 0) * TICKRATE:
                continue
            out.append({'kill': k, 'flasher': flasher, 'killer': atk, 'victim': vic,
                        'tick': b['tick'], 'kill_tick': t,
                        'blind_duration': round(b.get('blind_duration') or 0, 2),
                        'lead': round((t - b['tick']) / TICKRATE, 2)})
            break
    return out


def _support_moments(links, round_by_num, side_map):
    """One row per flash that converted, from the FLASHER's point of view -
    the moment "I set that up for you" becomes a thing you can query, sort and
    watch back.

    Placed at the victim's position rather than the flash's: the question a
    heatmap of these answers is "where does our support actually work", and
    that is where the blinded enemy was standing when it did.
    """
    out = []
    for link in links:
        k = link['kill']
        rn = k.get('round_num')
        rd = round_by_num.get(rn) or {}
        flasher, killer, victim = link['flasher'], link['killer'], link['victim']
        team = _team_of(side_map.get(flasher))
        side = side_at_round(side_map.get(flasher), rn)
        tick = link['kill_tick']
        start_tick, end_tick = clip_window_for_kill(tick, rd.get('start'))
        out.append({
            'kind': 'support', 'round': rn, 'tick': tick,
            'start_tick': start_tick, 'end_tick': end_tick,
            'player': flasher, 'side': side, 'team': team,
            'other': victim,
            'other_side': side_at_round(side_map.get(victim), rn),
            'other_team': _team_of(side_map.get(victim)),
            'x': k.get('victim_x'), 'y': k.get('victim_y'), 'z': k.get('victim_z'),
            'label': 'Flash support',
            'detail': 'converted %.1fs after the flash' % link['lead'],
            'attrs': dict(_round_ctx(rd, tick, team, side),
                          round=rn, tick=tick,
                          partner=killer, partner_team=team,
                          blind_duration=link['blind_duration'],
                          support_lead=link['lead'],
                          weapon=k.get('weapon'),
                          weapon_class=weapon_class(k.get('weapon')),
                          weapon_label=weapon_label(k.get('weapon')),
                          mates=_names(killer), opps=_names(victim)),
        })
    return out


def _join_flash_support(util_moments, supports):
    """Stamp each flash row with how many kills it enabled and who converted.

    This is the number the `support` rows cannot give on their own. 74 support
    links across 18 matches is a thin thing to reason about; 74 out of 3,252
    flashes thrown is a conversion rate with a real interval behind it. One
    detection, read two ways.
    """
    flashes = [m for m in util_moments if m['kind'] == 'flash']
    if not flashes:
        return
    by_thrower = {}
    for m in flashes:
        m['attrs'].setdefault('enabled_kills', 0)
        by_thrower.setdefault(m['player'], []).append(m)
    for link in supports:
        best = None
        for m in by_thrower.get(link['flasher']) or []:
            if m['tick'] is not None and abs(m['tick'] - link['tick']) <= _FLASH_JOIN_TICKS:
                best = m
                break
        if best is None:
            continue
        a = best['attrs']
        a['enabled_kills'] = a.get('enabled_kills', 0) + 1
        mates = a.setdefault('mates', [])
        if link['killer'] not in mates:
            mates.append(link['killer'])
    for m in flashes:
        n = m['attrs'].get('enabled_kills') or 0
        if n:
            m['detail'] = ((m['detail'] + ', ') if m['detail'] else '') \
                + '%d kill%s enabled' % (n, '' if n == 1 else 's')


def _crossfire_moments(damage, round_by_num, side_map):
    """Two teammates putting damage into the same enemy within a few seconds -
    who actually fights alongside whom, independent of who got the kill.

    One row per (round, victim, pair): a duo trading shots into the same enemy
    across a long firefight is one piece of teamplay, not fifteen. The pairing
    is "the first teammate to join you on this enemy", which is why the inner
    scan stops at the first match rather than enumerating every combination -
    a five-man focus-fire would otherwise produce ten rows saying one thing.
    """
    by_victim = {}
    for d in damage:
        a, v, t = d.get('attacker_name'), d.get('victim_name'), d.get('tick')
        if not a or not v or a == v or t is None:
            continue
        by_victim.setdefault((d.get('round_num'), v), []).append(d)

    out = []
    for (rn, victim), rows in by_victim.items():
        rows.sort(key=lambda d: d['tick'])
        vic_team = _team_of(side_map.get(victim))
        rd = round_by_num.get(rn) or {}
        seen = set()
        for i, d1 in enumerate(rows):
            a1 = d1['attacker_name']
            team = _team_of(side_map.get(a1))
            if not team or team == vic_team:
                continue
            for d2 in rows[i + 1:]:
                if d2['tick'] - d1['tick'] > CROSSFIRE_WINDOW_TICKS:
                    break
                a2 = d2['attacker_name']
                if a2 == a1 or _team_of(side_map.get(a2)) != team:
                    continue
                pair = tuple(sorted((a1, a2)))
                if pair in seen:
                    break
                seen.add(pair)
                gap = round((d2['tick'] - d1['tick']) / TICKRATE, 2)
                dmg = sum(x.get('dmg_health') or 0 for x in rows
                          if x['attacker_name'] in pair
                          and d1['tick'] <= x['tick'] <= d2['tick'] + CROSSFIRE_WINDOW_TICKS)
                side = side_at_round(side_map.get(a1), rn)
                start_tick, end_tick = clip_window_for_kill(d1['tick'], rd.get('start'))
                out.append({
                    'kind': 'crossfire', 'round': rn, 'tick': d1['tick'],
                    'start_tick': start_tick, 'end_tick': end_tick,
                    'player': a1, 'side': side, 'team': team,
                    'other': victim,
                    'other_side': side_at_round(side_map.get(victim), rn),
                    'other_team': vic_team,
                    # damage[] carries no coordinates, so a crossfire has no
                    # position. points() counts it as missing rather than
                    # burning a hotspot into the corner of the radar.
                    'x': None, 'y': None, 'z': None,
                    'label': 'Crossfire',
                    'detail': '%d damage together in %.1fs' % (dmg, gap),
                    'attrs': dict(_round_ctx(rd, d1['tick'], team, side),
                                  round=rn, tick=d1['tick'],
                                  partner=a2, partner_team=team,
                                  crossfire_gap=gap, damage=dmg,
                                  mates=_names(a2), opps=_names(victim)),
                })
                break
    return out


def _site_centroids(match_data):
    """Bomb-site positions, measured from this match's OWN observed plants.

    `bomb[]` plant events carry real coordinates and the site letter the game
    assigned, so a site's position is read off the data rather than guessed -
    and a site nobody planted at in this match simply has no centroid, which
    makes an execute toward it unlabelled instead of mislabelled. Guessing map
    geometry is the de_train mistake in another costume (see CLAUDE.md's
    "Multi-level maps"): a plausible-looking wrong answer you cannot see.

    Deliberately per-match. The index is built one match at a time and cached
    against that match's mtime alone (app.py's _moment_index_for), so a
    corpus-wide derivation would let one match's data change another match's
    cached answers with nothing to invalidate them.

    The catchment radius is the furthest observed plant from the centroid,
    floored at EXECUTE_MAX_SPREAD - the same cohesion distance a burst itself
    must satisfy, so a site seen only once still has a sane one.
    """
    plants = {}
    for b in (match_data.get('bomb') or []):
        if b.get('event') != 'plant' or b.get('X') is None or b.get('Y') is None:
            continue
        site = _site_label(b.get('bombsite'))
        if site:
            plants.setdefault(site, []).append((b['X'], b['Y']))
    out = {}
    for site, pts in plants.items():
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        out[site] = (cx, cy,
                     max([math.hypot(p[0] - cx, p[1] - cy) for p in pts] + [EXECUTE_MAX_SPREAD]))
    return out


def _nearest_site(cx, cy, sites):
    """The site a point falls inside, or None. Never "the closest one anyway" -
    an unlabelled execute is honest, a mid-map burst labelled "A" is not."""
    best, best_d = None, None
    for site, (sx, sy, r) in sites.items():
        d = math.hypot(cx - sx, cy - sy)
        if d <= r and (best_d is None or d < best_d):
            best, best_d = site, d
    return best


def _execute_moments(util_moments, kills, round_by_num, side_map, sites):
    """A team's coordinated utility burst: several pieces of utility, from
    several players, landing close together in a few seconds, at least one of
    them a smoke.

    The thresholds are measured, not chosen - 0.60 executes per round over the
    corpus. The two doing the real work are the player count and the spread:
    one player throwing three nades is a setup, not an execute, and utility
    scattered across the map inside the same six seconds is two people doing
    unrelated things. Dropping the smoke requirement takes it to 2.04/round,
    at which point the word stops meaning anything.

    The scan is greedy and jumps past a window it accepts, so one burst
    produces one row - without that, six pieces of utility in one execute
    yield four overlapping rows and the count triples.
    """
    by = {}
    for m in util_moments:
        if m.get('team') and m.get('x') is not None and m.get('tick') is not None:
            by.setdefault((m['round'], m['team']), []).append(m)

    kills_by_round = {}
    for k in kills:
        kills_by_round.setdefault(k.get('round_num'), []).append(k)

    out = []
    for (rn, team), rows in sorted(by.items(), key=lambda kv: (kv[0][0] or 0, kv[0][1])):
        rows.sort(key=lambda m: m['tick'])
        rd = round_by_num.get(rn) or {}
        i = 0
        while i < len(rows):
            j = i
            while j + 1 < len(rows) and rows[j + 1]['tick'] - rows[i]['tick'] <= EXECUTE_WINDOW_TICKS:
                j += 1
            win = rows[i:j + 1]
            throwers = _names(*[m.get('player') for m in win])
            cx = sum(m['x'] for m in win) / len(win)
            cy = sum(m['y'] for m in win) / len(win)
            zs = [m['z'] for m in win if m.get('z') is not None]
            cz = sum(zs) / len(zs) if zs else None
            spread = max(math.hypot(m['x'] - cx, m['y'] - cy) for m in win)
            if (len(win) >= EXECUTE_MIN_UTIL and len(throwers) >= EXECUTE_MIN_PLAYERS
                    and spread <= EXECUTE_MAX_SPREAD
                    and any(m['kind'] == 'smoke' for m in win)):
                out.append(_execute_row(rn, team, win, throwers, cx, cy, cz, spread, rd,
                                        kills_by_round.get(rn) or [], side_map, sites))
                i = j + 1
            else:
                i += 1
    return out


def _execute_row(rn, team, win, throwers, cx, cy, cz, spread, rd, rkills, side_map, sites):
    """One execute. `player` is None because this is a thing a TEAM did - the
    same shape a round row already has - and `mates` names everyone who threw.
    """
    start, end = win[0]['tick'], win[-1]['tick']
    side = win[0].get('side')
    counts = {}
    for m in win:
        counts[m['kind']] = counts.get(m['kind'], 0) + 1

    # The entry is the first kill either way after the utility starts landing:
    # ours means the execute got someone, theirs means it was read.
    entry, entry_lost = None, None
    for k in rkills:
        t = k.get('tick')
        if t is None or t < start or k.get('suicide'):
            continue
        if _team_of(side_map.get(k.get('attacker_name'))) == team:
            entry = k.get('attacker_name')
        else:
            entry_lost = k.get('victim_name')
        break

    site = _nearest_site(cx, cy, sites)
    start_tick, end_tick = clip_window_for_kill_span(start, end, rd.get('start'), rd.get('end'))
    bits = ['%d utility from %d players in %.1fs'
            % (len(win), len(throwers), (end - start) / TICKRATE)]
    if entry:
        bits.append('%s entried' % entry)
    elif entry_lost:
        bits.append('%s died first' % entry_lost)
    return {
        'kind': 'execute', 'round': rn, 'tick': start,
        'start_tick': start_tick, 'end_tick': end_tick,
        'player': None, 'side': side, 'team': team,
        'other': None, 'other_side': None, 'other_team': (3 - team) if team in (1, 2) else 0,
        'x': round(cx, 1), 'y': round(cy, 1), 'z': round(cz, 1) if cz is not None else None,
        'label': ('%s execute' % site) if site else 'Execute',
        'detail': ' - '.join(bits),
        'attrs': dict(_round_ctx(rd, start, team, side),
                      round=rn, tick=start,
                      util_n=len(win), smokes=counts.get('smoke', 0),
                      flashes=counts.get('flash', 0), mollies=counts.get('molotov', 0),
                      hes=counts.get('he', 0),
                      exec_site=site,
                      exec_span=round((end - start) / TICKRATE, 2),
                      exec_spread=round(spread, 1),
                      entry_player=entry, entry_lost=entry_lost,
                      players=throwers, mates=throwers, opps=[]),
    }


def _util_detail(m):
    a = m['attrs']
    if m['kind'] == 'flash':
        e, f = a.get('enemies_flashed', 0), a.get('friends_flashed', 0)
        bits = ['%d enemy blinded' % e if e == 1 else '%d enemies blinded' % e]
        if a.get('blind_time'):
            bits.append('%.1fs total' % a['blind_time'])
        if f:
            bits.append('%d teammate%s flashed' % (f, '' if f == 1 else 's'))
        return ', '.join(bits)
    if m['kind'] in ('he', 'molotov'):
        bits = ['%d damage' % a.get('damage', 0)]
        if a.get('team_damage'):
            bits.append('%d team damage' % a['team_damage'])
        return ', '.join(bits)
    return ''


def _round_moments(rounds):
    """One row per round, so round-level questions ("rounds we lost after
    going a man up") are askable in the same engine as kill-level ones."""
    out = []
    for r in rounds:
        out.append({
            'kind': 'round', 'round': r['n'], 'tick': r.get('freeze_end') or r.get('start'),
            'start_tick': r.get('start'), 'end_tick': r.get('end'),
            'player': None, 'side': r.get('winner'), 'team': r.get('winner_team'),
            'other': None, 'other_side': None, 'other_team': 0,
            'x': None, 'y': None, 'z': None,
            'label': 'Round %d' % r['n'],
            'detail': '%s win - %s' % ((r.get('winner') or '?').upper(),
                                       (r.get('reason') or 'unknown').replace('_', ' ')),
            'attrs': {
                'round': r['n'], 'tick': r.get('freeze_end'),
                'won_round': True,
                'round_reason': r.get('reason'),
                'bomb_planted': r.get('bomb_plant') is not None,
                'bomb_site': r.get('bomb_site'),
                'half': r.get('half'),
                'buy_ct': r.get('buy_ct'), 'buy_t': r.get('buy_t'),
                'ct_equip': r.get('ct_equip'), 't_equip': r.get('t_equip'),
            },
        })
    return out
