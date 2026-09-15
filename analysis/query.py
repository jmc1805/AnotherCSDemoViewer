"""Predicate engine over the moment index (analysis/moments.py).

This is what replaces the Multi Match Analyser's old "type a player name, tick
some boxes, press Apply → add matching rounds" filter. That filter could only
ever union: its round buckets and its event search both pushed into the same
selection list, so "rounds where I got an AWP opening kill AND we lost" was
literally unaskable. Here a query is a boolean tree with real `all` / `any` /
`not` nodes and real operators, evaluated over pre-derived attributes.

Three things make the UI on top of this usable rather than just powerful:

1. **The field registry is the single source of truth.** `FIELDS` describes
   every filterable attribute - type, label, group, allowed values. The client
   builds its whole filter panel from `/analyser/fields`, so adding a filter is
   a one-line change here and nowhere else, and an unknown field is a 400 with
   the list rather than a silently empty result.
2. **Facets come back with every query** - the value counts *within the current
   result set*. Filters stop being blind checkboxes you poke at and become a
   browsable distribution ("AWP 42 · AK-47 31 · Deagle 9"), which is the
   difference between a query language and a tool.
3. **Rates carry their sample size and a Wilson interval.** 16 matches is a
   small corpus; a bare "62% opening-duel win rate" over 13 duels would lie.
4. **A result set can be broken down.** `breakdown()` splits the current rows
   by any facet dimension and runs the same `aggregate()` over each bucket, so
   "every AWP opening kill" turns into who takes them, on which map, in which
   phase of the round - each row carrying its own n and its own intervals.
   Nothing here is a second query: it is the rows already matched, grouped.

The whole corpus is ~40k moment rows, so a query is a plain Python scan taking
single-digit milliseconds. No index, no SQL, no cleverness - and none needed
until the corpus is a hundred times bigger.
"""

import math

import maps
from .stats import wilson_interval

# Result granularity. Each maps to the moment kinds it draws from - `kill` and
# `death` are separate kinds in the index precisely so this is a lookup and
# not a perspective transformation (see moments.py's module docstring).
UNITS = {
    'kill':      ('kill',),
    'death':     ('death',),
    'duel':      ('kill', 'death'),
    'clutch':    ('clutch',),
    'multikill': ('multikill',),
    'utility':   ('smoke', 'molotov', 'flash', 'he'),
    'flash':     ('flash',),
    'round':     ('round',),
    # Teamplay. Each of these is a moment two players made together, which is
    # why they are their own kinds rather than flags on a kill: the subject is
    # the person who did the helping, and `partner` is who they helped. Note
    # `trade` is deliberately NOT in `duel` - it is the same event as the kill
    # row beside it, so folding it in would count that event three times.
    'trade':     ('trade',),
    'support':   ('support',),
    'execute':   ('execute',),
    'crossfire': ('crossfire',),
    'teamplay':  ('trade', 'support', 'execute', 'crossfire'),
}

DEFAULT_LIMIT = 300
MAX_LIMIT = 2000
# Coordinates are returned for every hit, not just the page of rows, when the
# caller asks - a heatmap of the first 300 of 4,000 kills is a different claim
# from a heatmap of all of them, and the map is where that difference is
# invisible. Three numbers per moment, so even the cap is under a megabyte.
POINTS_CAP = 20000

# Rates worth reporting for a result set, as (key, label, predicate, universe).
# `universe` restricts the denominator: an opening-duel win rate over all kills
# would be meaningless, so it only counts rows that were opening duels.
_RATES = [
    ('hs', 'Headshot %', lambda a: a.get('hs'), lambda a: a.get('weapon_class') not in (None, 'nade', 'bomb', 'world')),
    ('opening', 'Opening duel %', lambda a: a.get('opening'), lambda a: True),
    # Two different populations, two different names. `trade` is a property of
    # the KILL and is true on both sides of it ("I died to a trade" is a real
    # question); `traded` is subject-relative and exists only on death rows,
    # where the `key in attrs` gate below confines the rate to them. Making one
    # field mean both would blend the two invisibly on `unit: duel`, whose
    # denominator spans kills and deaths alike.
    ('trade', 'Trade kill %', lambda a: a.get('trade'), lambda a: True),
    ('traded', 'Death traded %', lambda a: a.get('traded'), lambda a: True),
    ('won_round', 'Round won %', lambda a: a.get('won_round'), lambda a: a.get('won_round') is not None),
    ('through_smoke', 'Through smoke %', lambda a: a.get('through_smoke'), lambda a: True),
    ('wallbang', 'Wallbang %', lambda a: a.get('wallbang'), lambda a: True),
    ('bomb_planted', 'Post-plant %', lambda a: a.get('bomb_planted'), lambda a: True),
]

# Breakdown dimensions offered back with every query. Each is (key, label,
# extractor, multi) where the extractor returns a bucket name (or None to
# skip) - or, when `multi` is true, a LIST of bucket names.
#
# A multi dimension breaks the one-row-one-bucket invariant on purpose: a
# moment involving two teammates genuinely belongs under both of them. The
# consequence is that its counts sum past the row count, and the honest
# response to that is to say so rather than to hide it - facet() and
# breakdown() both return the row count alongside the placements so the client
# can state the difference (see analyser.js's breakdown notes).
_DISTS = [
    ('by_player', 'Player', lambda r: r.get('player')),
    ('by_mate', 'With teammate', lambda r: r['attrs'].get('mates') or [], True),
    ('by_opp', 'Against', lambda r: r['attrs'].get('opps') or [], True),
    ('by_team', 'Team', lambda r: r.get('team') or None),
    ('by_weapon', 'Weapon', lambda r: r['attrs'].get('weapon_label')),
    ('by_weapon_class', 'Weapon class', lambda r: r['attrs'].get('weapon_class')),
    ('by_side', 'Side', lambda r: r.get('side')),
    ('by_situation', 'Man count', lambda r: r['attrs'].get('situation')),
    ('by_phase', 'Round phase', lambda r: r['attrs'].get('round_phase')),
    ('by_site', 'Bomb site', lambda r: r['attrs'].get('bomb_site')),
    ('by_zone', 'Zone', lambda r: r['attrs'].get('zone')),
    ('by_buy', 'Buy type', lambda r: r['attrs'].get('buy_self')),
    ('by_map', 'Map', lambda r: r.get('map')),
    ('by_match', 'Match', lambda r: r.get('match_stem')),
    ('by_kind', 'Moment type', lambda r: r.get('kind')),
]

# Normalised to 4-tuples so every consumer can unpack the same shape.
_DISTS = [(d + (False,))[:4] for d in _DISTS]


def _dist_values(get, multi, row):
    """Every bucket one row belongs to, as a list. A single-valued dimension
    yields zero or one; a multi-valued one yields as many as it names."""
    v = get(row)
    if multi:
        return [x for x in (v or []) if x is not None and x != '']
    return [] if v is None or v == '' else [v]


def _f(path, label, type_, group, values=None, requires_cap=None, help=None):
    return {'path': path, 'label': label, 'type': type_, 'group': group,
            'values': values, 'requires_cap': requires_cap, 'help': help}


# The filterable surface. `path` is dotted; a leading "attrs." reads the
# derived block, anything else is a top-level moment field.
FIELDS = {
    # ── who ──────────────────────────────────────────────────────────────────
    'player':        _f('player', 'Player', 'player', 'Who'),
    'other':         _f('other', 'Opponent', 'player', 'Who',
                        help='The other side of the duel: the victim on a kill, the killer on a death.'),
    'side':          _f('side', 'Side', 'enum', 'Who', values=['ct', 't']),
    'team':          _f('team', 'Team', 'enum', 'Who', values=[1, 2],
                        help='Fixed identity: 1 = first-half CT, 2 = first-half T. Survives the halftime swap.'),
    'assist':        _f('attrs.assist', 'Assisted by', 'player', 'Who'),
    'flash_assist':  _f('attrs.flash_assist', 'Flash-assisted', 'bool', 'Who'),

    # ── the duel ─────────────────────────────────────────────────────────────
    'weapon':        _f('attrs.weapon_label', 'Weapon', 'enum', 'Duel'),
    'weapon_class':  _f('attrs.weapon_class', 'Weapon class', 'enum', 'Duel',
                        values=['rifle', 'sniper', 'smg', 'pistol', 'shotgun', 'lmg',
                                'nade', 'knife', 'zeus', 'bomb', 'world', 'other']),
    'hs':            _f('attrs.hs', 'Headshot', 'bool', 'Duel'),
    'wallbang':      _f('attrs.wallbang', 'Wallbang', 'bool', 'Duel'),
    'through_smoke': _f('attrs.through_smoke', 'Through smoke', 'bool', 'Duel'),
    'noscope':       _f('attrs.noscope', 'No-scope', 'bool', 'Duel'),
    'attacker_blind': _f('attrs.attacker_blind', 'Attacker blind', 'bool', 'Duel'),
    'attacker_air':  _f('attrs.attacker_air', 'Mid-air', 'bool', 'Duel'),
    'opening':       _f('attrs.opening', 'Opening duel', 'bool', 'Duel'),
    'trade':         _f('attrs.trade', 'Trade kill', 'bool', 'Duel',
                        help='This kill avenged a teammate within 5 s. A property '
                            'of the kill, so it is true on the attacker\'s row '
                            'and on the victim\'s alike. For "was MY death '
                            'avenged", use Death was traded.'),
    'distance':      _f('attrs.distance', 'Distance', 'number', 'Duel', requires_cap='kill_coords'),

    # ── situation ────────────────────────────────────────────────────────────
    'situation':     _f('attrs.situation', 'Man count', 'enum', 'Situation',
                        help='Alive counts before the moment, from the subject\'s view - "3v5".'),
    'man_adv':       _f('attrs.man_adv', 'Man advantage', 'number', 'Situation',
                        help='Signed for the subject: +1 means they were a player up.'),
    'alive_own':     _f('attrs.alive_own', 'Own team alive', 'number', 'Situation'),
    'alive_opp':     _f('attrs.alive_opp', 'Enemies alive', 'number', 'Situation'),
    'clutch_n':      _f('attrs.clutch_n', 'Clutch opponents', 'number', 'Situation'),
    'multikill_n':   _f('attrs.multikill_n', 'Kills in round', 'number', 'Situation'),
    'won':           _f('attrs.won', 'Clutch won', 'bool', 'Situation'),

    # ── round context ────────────────────────────────────────────────────────
    'round':         _f('round', 'Round number', 'number', 'Round'),
    'round_phase':   _f('attrs.round_phase', 'Round phase', 'enum', 'Round',
                        values=['freeze', 'early', 'mid', 'late', 'postplant']),
    't_into_round':  _f('attrs.t_into_round', 'Seconds into round', 'number', 'Round'),
    't_since_plant': _f('attrs.t_since_plant', 'Seconds since plant', 'number', 'Round'),
    'bomb_planted':  _f('attrs.bomb_planted', 'Bomb planted', 'bool', 'Round'),
    'bomb_site':     _f('attrs.bomb_site', 'Bomb site', 'enum', 'Round', values=['A', 'B']),
    # No fixed `values`: Dust2's callouts aren't Mirage's. The option list is
    # whatever the corpus facet for this dimension actually contains, same as
    # every other enum the client can't hardcode. Gated on `has_zones`
    # (per-map: has this map's env_cs_place data been extracted yet -
    # tools/extract_map_zones.mjs - not per-match, unlike the other caps here).
    'zone':          _f('attrs.zone', 'Zone', 'enum', 'Round', requires_cap='has_zones',
                        help='Valve\'s own named callout region for this position '
                             '(env_cs_place) - finer than bomb_site, and it varies '
                             'per map.'),
    'won_round':     _f('attrs.won_round', 'Won the round', 'bool', 'Round'),
    'round_reason':  _f('attrs.round_reason', 'Round ended by', 'enum', 'Round'),
    'half':          _f('attrs.half', 'Half', 'enum', 'Round', values=[1, 2]),
    'buy_self':      _f('attrs.buy_self', 'Own buy', 'enum', 'Round',
                        values=['pistol', 'eco', 'force', 'semi', 'full']),
    'buy_enemy':     _f('attrs.buy_enemy', 'Enemy buy', 'enum', 'Round',
                        values=['pistol', 'eco', 'force', 'semi', 'full']),

    # ── utility effect ───────────────────────────────────────────────────────
    'util_kind':       _f('attrs.util_kind', 'Utility', 'enum', 'Utility',
                          values=['smoke', 'molotov', 'flash', 'he']),
    # ── teamplay ─────────────────────────────────────────────────────────────
    # `playerlist` is its own type because it takes `has`/`nhas` rather than
    # eq/in, and because the client has to route its clicks to an APPENDING
    # toggle: two `has` leaves on one field mean "both of them were in it",
    # which is a real question, whereas two `eq` leaves on `weapon` mean
    # nothing at all. That distinction is the reason the type exists.
    'with_player':   _f('attrs.mates', 'With teammate', 'playerlist', 'Teamplay',
                        help='A teammate involved in this moment - who assisted, '
                             'traded, flashed for you, or was in the crossfire. '
                             'Every moment carries the list, empty when nobody '
                             'else was in it, so "not with X" means exactly '
                             'that and never "we do not record this here".'),
    'vs_player':     _f('attrs.opps', 'Against', 'playerlist', 'Teamplay',
                        help='An opponent involved in this moment. A superset of '
                             '"Opponent": it also covers the enemies a clutch '
                             'faced, a multi-kill\'s victims and the players a '
                             'flash blinded.'),
    'partner':       _f('attrs.partner', 'Partner', 'player', 'Teamplay',
                        help='The one teammate a trade, flash support or '
                             'crossfire was shared with.'),
    'traded':        _f('attrs.traded', 'Death was traded', 'bool', 'Teamplay',
                        help='My death was avenged within 5 s. Death rows only - '
                             'the kill-side question is "Trade".'),
    'traded_by':     _f('attrs.traded_by', 'Avenged by', 'player', 'Teamplay'),
    'traded_for':    _f('attrs.traded_for', 'Avenged', 'player', 'Teamplay',
                        help='The teammate whose death this kill traded.'),
    'trade_latency': _f('attrs.trade_latency', 'Trade delay (s)', 'number', 'Teamplay'),
    'set_up_by':     _f('attrs.set_up_by', 'Flashed for me by', 'player', 'Teamplay',
                        requires_cap='blind_pairs'),
    'enabled_kills': _f('attrs.enabled_kills', 'Kills this flash enabled', 'number',
                        'Teamplay', requires_cap='blind_pairs',
                        help='On a flash: how many kills a teammate got on someone '
                             'it was still blinding. 0 on a flash that led to '
                             'nothing, absent on anything that is not a flash.'),
    'support_lead':  _f('attrs.support_lead', 'Flash lead time (s)', 'number', 'Teamplay',
                        requires_cap='blind_pairs'),
    'crossfire_gap': _f('attrs.crossfire_gap', 'Crossfire gap (s)', 'number', 'Teamplay'),
    'util_n':        _f('attrs.util_n', 'Utility in the execute', 'number', 'Teamplay',
                        requires_cap='util_coords'),
    'exec_site':     _f('attrs.exec_site', 'Execute site', 'enum', 'Teamplay',
                        values=['A', 'B'], requires_cap='util_coords',
                        help='Read from where this match\'s own bomb plants '
                             'happened, so an execute nowhere near a planted site '
                             'is unlabelled rather than guessed.'),
    'exec_spread':   _f('attrs.exec_spread', 'Execute spread (u)', 'number', 'Teamplay',
                        requires_cap='util_coords'),
    'exec_span':     _f('attrs.exec_span', 'Execute duration (s)', 'number', 'Teamplay',
                        requires_cap='util_coords'),
    'entry_player':  _f('attrs.entry_player', 'Entry frag by', 'player', 'Teamplay'),

    'enemies_flashed': _f('attrs.enemies_flashed', 'Enemies flashed', 'number', 'Utility'),
    'friends_flashed': _f('attrs.friends_flashed', 'Teammates flashed', 'number', 'Utility'),
    'blind_time':      _f('attrs.blind_time', 'Enemy blind time (s)', 'number', 'Utility'),
    'damage':          _f('attrs.damage', 'Damage dealt', 'number', 'Utility'),
    'team_damage':     _f('attrs.team_damage', 'Team damage', 'number', 'Utility'),
    'friendly_blind_time': _f('attrs.friendly_blind_time', 'Teammate blind time (s)',
                              'number', 'Utility'),
    'enemies_hit':     _f('attrs.enemies_hit', 'Enemies hit', 'number', 'Utility'),

    # ── match ────────────────────────────────────────────────────────────────
    'map':          _f('map', 'Map', 'enum', 'Match'),
    'mode':         _f('mode', 'Mode', 'enum', 'Match'),
    'match':        _f('match', 'Match file', 'enum', 'Match'),
    'played_epoch': _f('played_epoch', 'Played', 'number', 'Match'),
}


class QueryError(ValueError):
    """A malformed spec. Carries a message meant to be shown to the user."""


# ── spec evaluation ──────────────────────────────────────────────────────────

_MISSING = object()


def _resolve(row, path):
    """Read a dotted field path off a flattened moment row."""
    if path.startswith('attrs.'):
        return row['attrs'].get(path[6:], _MISSING)
    return row.get(path, _MISSING)


def _cmp_num(a, b, op):
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return False
    return {'gt': a > b, 'gte': a >= b, 'lt': a < b, 'lte': a <= b}[op]


def _test_leaf(row, node):
    name = node.get('f')
    field = FIELDS.get(name)
    if field is None:
        raise QueryError('unknown field %r' % name)
    op = node.get('op', 'eq')
    want = node.get('v')
    got = _resolve(row, field['path'])

    if op == 'exists':
        present = got is not _MISSING and got is not None
        return present if want in (None, True) else not present
    if got is _MISSING:
        got = None

    if op == 'is':
        # Tri-state aware: `is false` must not match a row where the attribute
        # is absent (a smoke has no `hs`), which a plain `not got` would.
        if want is None:
            return got is None
        return bool(got) is bool(want) if got is not None else False
    if op == 'eq':
        return _eqv(got, want)
    if op == 'ne':
        return not _eqv(got, want)
    if op == 'in':
        return any(_eqv(got, w) for w in (want or []))
    if op == 'nin':
        return not any(_eqv(got, w) for w in (want or []))
    if op in ('gt', 'gte', 'lt', 'lte'):
        return got is not None and _cmp_num(got, want, op)
    if op == 'between':
        if got is None or not isinstance(want, (list, tuple)) or len(want) != 2:
            return False
        return _cmp_num(got, want[0], 'gte') and _cmp_num(got, want[1], 'lte')
    if op in ('has', 'nhas'):
        # Tri-state, exactly like `is true` / `is false` above. An absent
        # attribute is not a list, so BOTH answers are False: a `round` row has
        # no teammates recorded, which is not the same claim as "played alone".
        if not isinstance(got, (list, tuple)):
            return False
        hit = any(_eqv(g, want) for g in got)
        return hit if op == 'has' else not hit
    if op == 'contains':
        if got is None:
            return False
        needle = str(want or '').lower()
        # On a list, substring-match the ELEMENTS. Falling through to the
        # scalar branch would test against Python's repr of the list, so
        # `contains "bob"` would match `['Bobby']` through the brackets and
        # quotes as readily as through the name.
        if isinstance(got, (list, tuple)):
            return any(needle in str(g).lower() for g in got)
        return needle in str(got).lower()
    raise QueryError('unknown operator %r' % op)


def _eqv(got, want):
    """Equality that doesn't trip over "1" vs 1 coming back from a URL, and
    that compares player/map names case-insensitively."""
    if got is None or want is None:
        return got is want or got == want
    if isinstance(got, bool) or isinstance(want, bool):
        return bool(got) == bool(want)
    if isinstance(got, (int, float)) and not isinstance(got, bool):
        try:
            return float(got) == float(want)
        except (TypeError, ValueError):
            return False
    return str(got).lower() == str(want).lower()


def matches(row, node):
    """Evaluate one boolean node against one row. An empty/absent node matches
    everything, so a query with no conditions is 'the whole corpus'."""
    if not node:
        return True
    if 'all' in node:
        return all(matches(row, n) for n in node['all'])
    if 'any' in node:
        clauses = node['any']
        return any(matches(row, n) for n in clauses) if clauses else True
    if 'not' in node:
        return not matches(row, node['not'])
    return _test_leaf(row, node)


# ── flattening ───────────────────────────────────────────────────────────────

def flatten(index):
    """Moment rows for one match index, each stamped with its match context so
    a row is self-contained once it leaves here (the result list is
    cross-match; a row that needs a lookup to render is a row that will be
    rendered wrong)."""
    ctx = {
        'match': index.get('match'),
        'match_stem': index.get('stem'),
        'map': index.get('map'),
        'map_key': index.get('map_key'),
        'mode': index.get('mode'),
        'played_epoch': index.get('played_epoch'),
        'played_label': index.get('played_label'),
        'caps': index.get('caps') or {},
    }
    out = []
    for m in index.get('moments') or []:
        row = dict(m)
        row.update(ctx)
        out.append(row)
    return out


def _scope_ok(index, scope):
    if not scope:
        return True
    files = scope.get('files')
    if files and index.get('match') not in files:
        return False
    scope_maps = scope.get('maps')
    if scope_maps:
        # Canonicalise both sides. A spec can arrive from a link shared before
        # a map's name was canonicalised, and it should still select the same
        # matches rather than quietly returning nothing.
        wanted = {maps.canonical(m) for m in scope_maps}
        if maps.canonical(index.get('map')) not in wanted \
                and maps.canonical(index.get('map_key')) not in wanted:
            return False
    modes = scope.get('modes')
    if modes and index.get('mode') not in modes:
        return False
    ep = index.get('played_epoch') or 0
    if scope.get('played_from') and ep < scope['played_from']:
        return False
    if scope.get('played_to') and ep > scope['played_to']:
        return False
    return True


# ── the entry point ──────────────────────────────────────────────────────────

def evaluate(spec, indexes):
    """Run `spec` over an iterable of per-match indexes.

    Returns {rows, total, truncated, aggregates, facets, breakdown, points,
    scanned}. `rows` is capped at `limit`; `total`, the aggregates, the facets,
    the breakdown and the points are computed over the FULL match set, so the
    numbers never silently describe only the first page.
    """
    spec = spec or {}
    unit = spec.get('unit') or 'kill'
    if unit not in UNITS:
        raise QueryError('unknown unit %r (want one of %s)' % (unit, ', '.join(sorted(UNITS))))
    kinds = set(spec.get('kinds') or UNITS[unit])
    where = spec.get('where')
    scope = spec.get('scope') or {}
    limit = min(int(spec.get('limit') or DEFAULT_LIMIT), MAX_LIMIT)

    hits, scanned = [], 0
    for index in indexes:
        if not _scope_ok(index, scope):
            continue
        for row in flatten(index):
            scanned += 1
            if row['kind'] not in kinds:
                continue
            if not matches(row, where):
                continue
            hits.append(row)

    hits.sort(key=_sort_key(spec.get('sort') or 'recent'))
    aggregates = aggregate(hits)
    facets = facet(hits)
    out = {
        'rows': [_public_row(r) for r in hits[:limit]],
        'total': len(hits),
        'truncated': len(hits) > limit,
        'scanned': scanned,
        'aggregates': aggregates,
        'facets': facets,
    }
    group_by = spec.get('group_by')
    if group_by:
        out['breakdown'] = breakdown(hits, group_by)
    if spec.get('points'):
        out['points'] = points(hits)
        out['points_total'] = len(hits)
    return out


def _sort_key(sort):
    if sort == 'chrono':
        return lambda r: (r.get('played_epoch') or 0, r.get('round') or 0, r.get('tick') or 0)
    if sort == 'impact':
        # Biggest moment first: clutches and multi-kills outrank plain kills,
        # then by magnitude. Mirrors highlights.highlight_sort_key's intent.
        rank = {'clutch': 0, 'multikill': 1, 'execute': 2, 'kill': 3, 'death': 3,
                'trade': 3, 'support': 4, 'crossfire': 5}
        return lambda r: (rank.get(r['kind'], 6),
                          -(r['attrs'].get('clutch_n') or r['attrs'].get('multikill_n')
                            or r['attrs'].get('util_n') or 0),
                          -(r.get('played_epoch') or 0), r.get('tick') or 0)
    if sort == 't_into_round':
        return lambda r: (r['attrs'].get('t_into_round') if r['attrs'].get('t_into_round') is not None else 1e9,)
    # 'recent' - newest match first, then chronological within the match.
    return lambda r: (-(r.get('played_epoch') or 0), r.get('round') or 0, r.get('tick') or 0)


def _public_row(r):
    """Trim a row to what a result list actually renders + what it needs to
    preview, clip or jump. Keeps `attrs` - the UI shows derived context and the
    client-side facet chips read from it."""
    return {
        'kind': r['kind'], 'match': r['match'], 'match_stem': r['match_stem'],
        'map': r['map'], 'mode': r['mode'],
        'match_played_label': r.get('played_label'),
        'match_played_epoch': r.get('played_epoch'),
        'round': r['round'], 'tick': r['tick'],
        'start_tick': r['start_tick'], 'end_tick': r['end_tick'],
        'player': r['player'], 'side': r['side'], 'team': r['team'],
        # `other_team` is what lets the client tint the second name by fixed
        # team identity; without it a row could show who the opponent was but
        # not which side they were on, which is most of the information.
        'other': r['other'], 'other_side': r.get('other_side'),
        'other_team': r.get('other_team'),
        'partner': r['attrs'].get('partner'),
        'partner_team': r['attrs'].get('partner_team'),
        'x': r['x'], 'y': r['y'],
        'label': r['label'], 'detail': r['detail'],
        'type': r['kind'],          # momentrow.logic.js renders `type` as the chip
        'attrs': r['attrs'],
    }


def aggregate(rows):
    """Headline numbers for a result set. Every rate carries its raw k/n and a
    Wilson interval - at this corpus size a bare percentage is a lie waiting to
    be quoted, and the interval is the honest version of it."""
    n = len(rows)
    out = {
        'n': n,
        'n_rounds': len({(r['match'], r['round']) for r in rows}),
        'n_matches': len({r['match'] for r in rows}),
        'n_players': len({r['player'] for r in rows if r['player']}),
        'rates': [], 'means': [],
    }
    if not n:
        return out

    for key, label, pred, universe in _RATES:
        pool = [r for r in rows if universe(r['attrs'])]
        # A rate is only meaningful where the attribute exists at all: a smoke
        # has no `hs`, so it must not land in the denominator as a failure.
        pool = [r for r in pool if key in r['attrs']]
        if not pool:
            continue
        k = sum(1 for r in pool if pred(r['attrs']))
        ci = wilson_interval(k, len(pool))
        out['rates'].append({
            'key': key, 'label': label, 'k': k, 'n': len(pool),
            'p': k / len(pool),
            'ci': [round(ci[0], 4), round(ci[1], 4)] if ci else None,
        })

    for key, label in (('t_into_round', 'Seconds into round'),
                       ('man_adv', 'Man advantage'),
                       ('distance', 'Distance')):
        vals = [r['attrs'][key] for r in rows
                if r['attrs'].get(key) is not None]
        if len(vals) < 2:
            continue
        mean = sum(vals) / len(vals)
        sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)) if len(vals) > 1 else 0.0
        out['means'].append({
            'key': key, 'label': label, 'n': len(vals),
            'mean': round(mean, 2), 'sd': round(sd, 2),
            'min': round(min(vals), 2), 'max': round(max(vals), 2),
        })

    # Time-into-round histogram in 5 s buckets - where in a round this pattern
    # actually happens, which is usually the first thing worth seeing.
    times = [r['attrs']['t_into_round'] for r in rows
             if r['attrs'].get('t_into_round') is not None and r['attrs']['t_into_round'] >= 0]
    if times:
        bin_s = 5
        nb = int(max(times) // bin_s) + 1
        bins = [0] * nb
        for t in times:
            bins[min(int(t // bin_s), nb - 1)] += 1
        out['time_hist'] = {'bin_s': bin_s, 'bins': bins}
    return out


def facet(rows, top=12):
    """Value counts per dimension, within the result set. This is what turns
    the filter panel from a wall of blind checkboxes into something you can
    read the shape of the data off."""
    out = {}
    for key, label, get, multi in _DISTS:
        counts = {}
        n_rows = 0
        for r in rows:
            vals = _dist_values(get, multi, r)
            if vals:
                n_rows += 1
            for v in vals:
                counts[v] = counts.get(v, 0) + 1
        if not counts:
            continue
        items = sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))
        block = {
            'label': label,
            'values': [{'value': k, 'n': v} for k, v in items[:top]],
            'other': sum(v for _, v in items[top:]),
            'distinct': len(items),
        }
        if multi:
            # The counts overlap, so the client needs both numbers to describe
            # them truthfully: how many placements there are, and how many
            # distinct moments produced them.
            block['multi'] = True
            block['rows'] = n_rows
        out[key] = block
    return out


def points(rows, cap=POINTS_CAP):
    """Map coordinates for every row that has them, as {x, y, map}.

    Rows without a position are simply absent - a clutch or a multikill
    describes a round, not a spot, and defaulting them to (0,0) would burn a
    permanent hotspot into the corner of every radar. The caller is told how
    many were dropped so it can say so rather than quietly plotting a subset.
    """
    out, missing = [], 0
    for r in rows:
        x, y = r.get('x'), r.get('y')
        if x is None or y is None:
            missing += 1
            continue
        if len(out) >= cap:
            missing += 1
            continue
        out.append({'x': x, 'y': y, 'map': r.get('map')})
    return {'pts': out, 'missing': missing}


BREAKDOWN_TOP = 30


def breakdowns():
    """The dimensions a result set can be split by - the facet dimensions,
    since those are exactly the ones the index has a bucket name for."""
    return [{'key': key, 'label': label, 'multi': multi}
            for key, label, _, multi in _DISTS]


def breakdown(rows, by, top=BREAKDOWN_TOP):
    """Split `rows` by one dimension and aggregate each bucket separately.

    This is what a result list cannot answer on its own. "Every AWP opening
    kill" is a list; "who takes them, and does it actually win the round" is a
    comparison, and a comparison needs each bucket's own n and its own
    interval - a player with 3 opening AWP kills and one with 40 must not read
    as peers because they both show "67%".

    Each bucket reuses aggregate() verbatim, so a rate means the same thing
    here as it does in the headline strip. The time histogram is dropped: it is
    per-bucket noise at this size and would triple the payload.
    """
    dist = next((d for d in _DISTS if d[0] == by), None)
    if dist is None:
        raise QueryError('unknown breakdown %r (want one of %s)'
                         % (by, ', '.join(d[0] for d in _DISTS)))
    _, label, get, multi = dist

    buckets = {}
    skipped = 0
    placements = 0
    for r in rows:
        vals = _dist_values(get, multi, r)
        if not vals:
            # A bucket named "None" would be read as a real group. Rows the
            # dimension doesn't apply to are counted and reported instead.
            skipped += 1
            continue
        for v in vals:
            placements += 1
            buckets.setdefault(v, []).append(r)

    groups = []
    for value, rs in buckets.items():
        agg = aggregate(rs)
        agg.pop('time_hist', None)
        agg['value'] = value
        groups.append(agg)
    groups.sort(key=lambda g: (-g['n'], str(g['value'])))
    out = {
        'by': by, 'label': label,
        'groups': groups[:top],
        'distinct': len(groups),
        'hidden': max(0, len(groups) - top),
        'skipped': skipped,
    }
    if multi:
        # On a single-valued dimension `sum(g.n) + skipped == len(rows)` and
        # the table reads as a partition. Here it does not, because a moment
        # with two teammates belongs under both - so every term of the real
        # identity goes on the wire and the client states it. Each bucket's own
        # n and interval stay exactly as honest as before: "when I'm on with
        # Bob, over n of them" is a legitimate conditional statistic.
        out['multi'] = True
        out['n_rows'] = len(rows)
        out['placements'] = placements
    return out


def field_registry(caps=None):
    """The filter surface, as the client's panel is built from it. Fields
    gated on a capability the corpus doesn't have yet (kill coordinates before
    the re-parse) are dropped rather than offered and silently useless."""
    caps = caps or {}
    out = {}
    for name, f in FIELDS.items():
        if f['requires_cap'] and not caps.get(f['requires_cap']):
            continue
        out[name] = {k: v for k, v in f.items() if k != 'path'}
    return out


# ── presets ──────────────────────────────────────────────────────────────────
# Shipped queries. They double as documentation of the grammar and as the
# page's empty state - a query language nobody can think of a question for is
# a query language nobody uses.
PRESETS = [
    {'id': 'opening_lost', 'name': 'Opening duels lost',
     'spec': {'unit': 'death', 'where': {'all': [{'f': 'opening', 'op': 'is', 'v': True}]},
              'sort': 'recent'}},
    {'id': 'opening_won', 'name': 'Opening duels won',
     'spec': {'unit': 'kill', 'where': {'all': [{'f': 'opening', 'op': 'is', 'v': True}]},
              'sort': 'recent'}},
    {'id': 'awp_kills', 'name': 'AWP kills',
     'spec': {'unit': 'kill', 'where': {'all': [{'f': 'weapon_class', 'op': 'eq', 'v': 'sniper'}]}}},
    {'id': 'smoke_kills', 'name': 'Kills through smoke',
     'spec': {'unit': 'kill', 'where': {'all': [{'f': 'through_smoke', 'op': 'is', 'v': True}]}}},
    {'id': 'clutches', 'name': '1vX clutches won',
     'spec': {'unit': 'clutch', 'where': {'all': [{'f': 'won', 'op': 'is', 'v': True}]},
              'sort': 'impact'}},
    {'id': 'clutch_lost', 'name': '1vX clutches lost',
     'spec': {'unit': 'clutch', 'where': {'all': [{'f': 'won', 'op': 'is', 'v': False}]},
              'sort': 'impact'}},
    {'id': 'early_deaths', 'name': 'Deaths in the first 20 s',
     'spec': {'unit': 'death',
              'where': {'all': [{'f': 't_into_round', 'op': 'between', 'v': [0, 20]}]}}},
    {'id': 'untraded_deaths', 'name': 'Deaths nobody traded',
     'spec': {'unit': 'death', 'where': {'all': [{'f': 'traded', 'op': 'is', 'v': False}]}}},
    {'id': 'dead_molotovs', 'name': 'Molotovs that dealt nothing',
     'spec': {'unit': 'utility',
              'where': {'all': [{'f': 'util_kind', 'op': 'eq', 'v': 'molotov'},
                                {'f': 'damage', 'op': 'eq', 'v': 0}]}}},
    {'id': 'team_flashes', 'name': 'Flashes that only blinded teammates',
     'spec': {'unit': 'flash',
              'where': {'all': [{'f': 'friends_flashed', 'op': 'gte', 'v': 1},
                                {'f': 'enemies_flashed', 'op': 'eq', 'v': 0}]}}},
    {'id': 'lost_man_up', 'name': 'Deaths while a man up',
     'spec': {'unit': 'death', 'where': {'all': [{'f': 'man_adv', 'op': 'gte', 'v': 1}]}}},
    {'id': 'eco_kills', 'name': 'Kills on an eco',
     'spec': {'unit': 'kill', 'where': {'all': [{'f': 'buy_self', 'op': 'in', 'v': ['eco', 'pistol']}]}}},
    {'id': 'postplant_kills', 'name': 'Post-plant kills',
     'spec': {'unit': 'kill', 'where': {'all': [{'f': 'bomb_planted', 'op': 'is', 'v': True}]}}},
    {'id': 'aces', 'name': 'Aces and 4Ks',
     'spec': {'unit': 'multikill',
              'where': {'all': [{'f': 'multikill_n', 'op': 'gte', 'v': 4}]}, 'sort': 'impact'}},

    # ── teamplay ─────────────────────────────────────────────────────────────
    # These carry more weight than the others: they are how someone discovers
    # that `has` and the pair dimensions exist at all. Each pairs a teamplay
    # unit with the breakdown that makes it a question about people rather
    # than a list of events.
    {'id': 'trade_partners', 'name': 'Who I trade for',
     'spec': {'unit': 'trade', 'group_by': 'by_mate', 'sort': 'recent'}},
    {'id': 'untraded_openings', 'name': 'Opening deaths nobody traded',
     'spec': {'unit': 'death',
              'where': {'all': [{'f': 'opening', 'op': 'is', 'v': True},
                                {'f': 'traded', 'op': 'is', 'v': False}]}}},
    {'id': 'flash_support', 'name': 'Flashes a teammate converted',
     'spec': {'unit': 'support', 'group_by': 'by_mate', 'sort': 'recent'}},
    {'id': 'dead_flashes', 'name': 'Flashes nobody converted',
     'spec': {'unit': 'flash',
              'where': {'all': [{'f': 'enemies_flashed', 'op': 'gte', 'v': 1},
                                {'f': 'enabled_kills', 'op': 'eq', 'v': 0}]}}},
    {'id': 'executes', 'name': 'Executes, by site',
     'spec': {'unit': 'execute', 'group_by': 'by_site', 'sort': 'impact'}},
    {'id': 'executes_lost', 'name': 'Executes that lost the round',
     'spec': {'unit': 'execute',
              'where': {'all': [{'f': 'won_round', 'op': 'is', 'v': False}]},
              'sort': 'impact'}},
    {'id': 'duos', 'name': 'Who I fight alongside',
     'spec': {'unit': 'crossfire', 'group_by': 'by_mate', 'sort': 'recent'}},
    {'id': 'nemesis', 'name': 'Who kills me most',
     'spec': {'unit': 'death', 'group_by': 'by_opp', 'sort': 'recent'}},
]
