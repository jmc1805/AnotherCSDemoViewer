"""Unit tests for analysis/moments.py (the moment index) and analysis/query.py
(the predicate engine behind the Multi Match Analyser).

Run: python test/moments_test.py     (no deps, no fixtures on disk)

Everything runs against a hand-built match dict, so the expected alive counts,
trade links and clutch pivots can be reasoned about by hand rather than
asserted against whatever the corpus happens to contain today.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis import moments, query

_passed = 0
_failed = 0


def ok(cond, msg):
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print('  x FAIL:', msg)


def eq(a, b, msg):
    ok(a == b, '%s (got %r, want %r)' % (msg, a, b))


# ── fixture ───────────────────────────────────────────────────────────────────
# Two teams of 5. Team 1 (first-half CT): a1..a5. Team 2 (first-half T): b1..b5.
CT = ['a1', 'a2', 'a3', 'a4', 'a5']
T = ['b1', 'b2', 'b3', 'b4', 'b5']
TR = 64


def kill(tick, rnd, atk, vic, **kw):
    """One kill. Sides are the LITERAL sides for that round, which is what the
    parser records - the fixture uses rounds 1-12 so they equal the fixed
    identity and the halftime swap isn't in play unless a test wants it."""
    atk_side = 'ct' if atk in CT else 't'
    vic_side = 'ct' if vic in CT else 't'
    row = {'tick': tick, 'round_num': rnd, 'attacker_name': atk, 'attacker_side': atk_side,
           'victim_name': vic, 'victim_side': vic_side, 'weapon': 'weapon_ak47',
           'headshot': False}
    row.update(kw)
    return row


def make_match(kills, rounds=None, **extra):
    """A complete 5v5 match. Every player is present in shots[] whether or not
    they ever traded a kill, because that is what a real match looks like - and
    because a roster reconstructed from kills alone is precisely the bug that
    invents clutches (see roster_side_map)."""
    rounds = rounds or [{'round_num': 1, 'start': 0, 'freeze_end': 20 * TR,
                         'end': 100 * TR, 'winner': 'ct', 'reason': 'ct_killed',
                         'bomb_plant': None, 'bomb_site': None,
                         'economy': {'ct_equip': 25000, 't_equip': 22000}}]
    r0 = rounds[0]['round_num']
    shots = [{'tick': (rounds[0].get('freeze_end') or 0) + 1, 'round_num': r0,
              'player_name': n, 'player_side': ('ct' if n in CT else 't'),
              'weapon': 'weapon_ak47'} for n in CT + T]
    data = {'mapName': 'de_dust2', 'rounds': rounds, 'kills': kills,
            'damage': [], 'blind': [], 'shots': shots, 'smokes': [], 'infernos': [],
            'flashes': [], 'he': [], 'ranks': []}
    data.update(extra)
    return data


META = {'file': 'de_dust_20260101_1200_mm.json', 'stem': 'de_dust_20260101_1200_mm',
        'map': 'de_dust', 'mode': 'mm', 'played_epoch': 1767268800,
        'played_label': '2026-01-01 12:00', 'demo': 'x.dem'}


def build(kills, rounds=None, **extra):
    return moments.build_match_index(make_match(kills, rounds, **extra), META)


def kinds(idx, kind):
    return [m for m in idx['moments'] if m['kind'] == kind]


# ── weapon classification ─────────────────────────────────────────────────────
def test_weapon_class():
    eq(moments.weapon_class('weapon_ak47'), 'rifle', 'AK is a rifle')
    eq(moments.weapon_class('weapon_awp'), 'sniper', 'AWP is a sniper')
    eq(moments.weapon_class('weapon_m4a1_silencer'), 'rifle', 'M4A1-S is a rifle')
    eq(moments.weapon_class('weapon_knife_karambit'), 'knife', 'any knife variant is a knife')
    eq(moments.weapon_class('weapon_bayonet'), 'knife', 'bayonet is a knife')
    eq(moments.weapon_class('weapon_brandnewgun'), 'other',
       'an unknown weapon gets its own bucket, never a guessed class')
    eq(moments.weapon_class(None), 'other', 'missing weapon does not crash')
    eq(moments.weapon_label('weapon_m4a1_silencer'), 'M4A1-S', 'readable weapon label')
    eq(moments.weapon_label('weapon_ak47'), 'AK-47', 'readable AK label')


# ── buy classification ────────────────────────────────────────────────────────
def test_classify_buy():
    eq(moments.classify_buy(0, 1), 'pistol', 'round 1 is a pistol round by definition')
    eq(moments.classify_buy(25000, 13), 'pistol', 'round 13 is a pistol round even with money')
    eq(moments.classify_buy(1200, 5), 'eco', 'cheap buy is an eco')
    eq(moments.classify_buy(8000, 5), 'force', 'mid buy is a force')
    eq(moments.classify_buy(25000, 5), 'full', 'expensive buy is a full buy')
    eq(moments.classify_buy(None, 5), None, 'missing economy stays unknown, not "eco"')


# ── round lookup ──────────────────────────────────────────────────────────────
def test_round_lookup():
    rounds = [{'round_num': 1, 'start': 0}, {'round_num': 2, 'start': 1000},
              {'round_num': 3, 'start': 2000}]
    lk = moments.round_lookup(rounds)
    eq(lk(0), 1, 'exact round start')
    eq(lk(999), 1, 'just before the next round')
    eq(lk(1000), 2, 'next round start is the next round')
    eq(lk(5000), 3, 'after the last start stays in the last round')
    eq(lk(None), None, 'no tick, no round')
    eq(moments.round_lookup([])(5), None, 'no rounds at all is not a crash')


# ── every kill becomes two rows ───────────────────────────────────────────────
def test_kill_and_death_rows():
    idx = build([kill(30 * TR, 1, 'a1', 'b1')])
    k, d = kinds(idx, 'kill'), kinds(idx, 'death')
    eq(len(k), 1, 'one kill row')
    eq(len(d), 1, 'one death row')
    eq(k[0]['player'], 'a1', 'kill row subject is the attacker')
    eq(k[0]['other'], 'b1', 'kill row other is the victim')
    eq(d[0]['player'], 'b1', 'death row subject is the victim')
    eq(d[0]['other'], 'a1', 'death row other is the killer')
    eq(k[0]['team'], 1, 'attacker is team 1')
    eq(d[0]['team'], 2, 'victim is team 2')
    ok(k[0]['start_tick'] < k[0]['tick'] < k[0]['end_tick'],
       'clip window brackets the moment')


def test_suicide_makes_no_kill_row():
    idx = build([kill(30 * TR, 1, 'a1', 'a1', suicide=True)])
    eq(len(kinds(idx, 'kill')), 0, 'a suicide is nobody\'s kill')
    eq(len(kinds(idx, 'death')), 1, 'a suicide is still a death')


# ── alive counts / man advantage ──────────────────────────────────────────────
def test_alive_counts_are_before_the_kill():
    idx = build([kill(30 * TR, 1, 'a1', 'b1'),
                 kill(31 * TR, 1, 'a1', 'b2'),
                 kill(32 * TR, 1, 'b3', 'a1')])
    k = kinds(idx, 'kill')
    eq(k[0]['attrs']['situation'], '5v5', 'first kill happens at 5v5')
    eq(k[1]['attrs']['situation'], '5v4', 'second kill happens after one T died')
    eq(k[1]['attrs']['man_adv'], 1, 'attacker was a man up')
    eq(k[2]['attrs']['situation'], '3v5',
       'the T killer sees it from their own side: 3 T alive vs 5 CT')
    eq(k[2]['attrs']['man_adv'], -2, 'man advantage is signed for the subject')
    d = kinds(idx, 'death')
    eq(d[0]['attrs']['situation'], '5v5',
       'the victim sees the same moment from their side, also before the kill')


def test_man_advantage_is_per_subject():
    idx = build([kill(30 * TR, 1, 'a1', 'b1'), kill(31 * TR, 1, 'a2', 'b2'),
                 kill(32 * TR, 1, 'a3', 'b3')])
    k = kinds(idx, 'kill')[2]
    d = [m for m in kinds(idx, 'death') if m['tick'] == 32 * TR][0]
    eq(k['attrs']['man_adv'], 2, 'CT killer was two men up')
    eq(d['attrs']['man_adv'], -2, 'the T victim was two men down at the same instant')


# ── opening duel ──────────────────────────────────────────────────────────────
def test_opening_duel():
    idx = build([kill(30 * TR, 1, 'a1', 'b1'), kill(40 * TR, 1, 'a2', 'b2')])
    k = kinds(idx, 'kill')
    ok(k[0]['attrs']['opening'], 'first kill of the round is the opening duel')
    ok(not k[1]['attrs']['opening'], 'the second kill is not')
    d = kinds(idx, 'death')
    ok(d[0]['attrs']['opening'], 'the opening duel is also the victim\'s row')


# ── trades ────────────────────────────────────────────────────────────────────
def test_trade_within_window():
    # b1 kills a1; a2 kills b1 two seconds later -> a trade.
    idx = build([kill(30 * TR, 1, 'b1', 'a1'), kill(32 * TR, 1, 'a2', 'b1')])
    k = kinds(idx, 'kill')
    ok(not k[0]['attrs']['trade'], 'the first kill of an exchange is not a trade')
    ok(k[1]['attrs']['trade'], 'killing your teammate\'s killer within 5 s is a trade')


def test_trade_outside_window_is_not_a_trade():
    idx = build([kill(30 * TR, 1, 'b1', 'a1'), kill(40 * TR, 1, 'a2', 'b1')])
    k = kinds(idx, 'kill')
    ok(not k[1]['attrs']['trade'], '10 s later is revenge, not a trade')


def test_trade_requires_the_same_killer():
    # b1 kills a1; a2 kills b2 (a different opponent) -> not a trade.
    idx = build([kill(30 * TR, 1, 'b1', 'a1'), kill(31 * TR, 1, 'a2', 'b2')])
    ok(not kinds(idx, 'kill')[1]['attrs']['trade'],
       'killing someone else in the window does not trade the death')


# ── clutches ──────────────────────────────────────────────────────────────────
def test_clutch_attempt_recorded_for_the_loser_too():
    # All four CT teammates of a5 die; a5 is left 1v5 and the round is lost.
    kills = [kill((30 + i) * TR, 1, 'b%d' % (i + 1), 'a%d' % (i + 1)) for i in range(4)]
    idx = build(kills, rounds=[{'round_num': 1, 'start': 0, 'freeze_end': 20 * TR,
                                'end': 100 * TR, 'winner': 't', 'reason': 't_killed',
                                'economy': {}}])
    cl = kinds(idx, 'clutch')
    eq(len(cl), 1, 'one clutch attempt')
    eq(cl[0]['player'], 'a5', 'the last man alive is the clutcher')
    eq(cl[0]['attrs']['clutch_n'], 5, 'faced all five opponents')
    eq(cl[0]['attrs']['won'], False,
       'a lost clutch is still recorded - highlights.py only ever kept the wins')


def test_clutch_needs_a_full_roster_not_just_who_appears():
    """The bug this guards: reconstructing the roster from a round's kill
    events alone counts a team as smaller than it is, so a player who is one
    of three survivors gets reported as clutching. Only two CTs appear here;
    the other three are alive and untouched, so there is no clutch."""
    idx = build([kill(30 * TR, 1, 'b1', 'a1'), kill(31 * TR, 1, 'b1', 'a2')])
    eq(len(kinds(idx, 'clutch')), 0,
       'three CTs are still alive - nobody is clutching')


def test_one_v_one_records_both_players():
    # Four die on each side -> a5 and b5 are both alone.
    kills = []
    t = 30
    for i in range(4):
        kills.append(kill(t * TR, 1, 'b%d' % (i + 1), 'a%d' % (i + 1))); t += 1
    for i in range(4):
        kills.append(kill(t * TR, 1, 'a5', 'b%d' % (i + 1))); t += 1
    idx = build(kills, rounds=[{'round_num': 1, 'start': 0, 'freeze_end': 20 * TR,
                                'end': 100 * TR, 'winner': 'ct', 'reason': 'ct_killed',
                                'economy': {}}])
    cl = kinds(idx, 'clutch')
    eq(sorted(c['player'] for c in cl), ['a5', 'b5'],
       'a 1v1 is two simultaneous clutch attempts, one per team')
    won = {c['player']: c['attrs']['won'] for c in cl}
    eq(won['a5'], True, 'the CT clutcher won')
    eq(won['b5'], False, 'the T clutcher lost')


# ── multikills ────────────────────────────────────────────────────────────────
def test_multikill():
    kills = [kill((30 + i) * TR, 1, 'a1', 'b%d' % (i + 1)) for i in range(3)]
    idx = build(kills)
    mk = kinds(idx, 'multikill')
    eq(len(mk), 1, 'one multikill')
    eq(mk[0]['label'], '3K', 'three kills is a 3K')
    eq(mk[0]['player'], 'a1', 'credited to the player')
    ok(mk[0]['start_tick'] <= kills[0]['tick'],
       'the clip spans the whole sequence, not just the last kill')
    ok(mk[0]['end_tick'] >= kills[-1]['tick'], 'and runs past the final kill')
    ks = kinds(idx, 'kill')
    eq([k['attrs']['multikill_n'] for k in ks], [1, 2, 3],
       'each kill knows which number in the round it was')


def test_single_kill_is_not_a_multikill():
    eq(len(kinds(build([kill(30 * TR, 1, 'a1', 'b1')]), 'multikill')), 0,
       'one kill is not a multikill')


def test_ace():
    kills = [kill((30 + i) * TR, 1, 'a1', 'b%d' % (i + 1)) for i in range(5)]
    eq(kinds(build(kills), 'multikill')[0]['label'], 'ACE', 'five kills is an ace')


# ── round context ─────────────────────────────────────────────────────────────
def test_round_phase_and_timing():
    # Round 5, not round 1 - a pistol round is classified as 'pistol' whatever
    # the economy says, which would mask the buy thresholds under test.
    rounds = [{'round_num': 5, 'start': 0, 'freeze_end': 20 * TR, 'end': 200 * TR,
               'winner': 'ct', 'reason': 'bomb_defused', 'bomb_plant': 60 * TR,
               'bomb_site': 'bombsite_a', 'economy': {'ct_equip': 25000, 't_equip': 3000}}]
    idx = build([kill(25 * TR, 5, 'a1', 'b1'),     # 5 s in  -> early
                 kill(40 * TR, 5, 'a1', 'b2'),     # 20 s in -> mid
                 kill(70 * TR, 5, 'a1', 'b3')],    # after the plant
                rounds=rounds)
    k = kinds(idx, 'kill')
    eq(k[0]['attrs']['t_into_round'], 5.0, 'seconds measured from freeze-time end')
    eq(k[0]['attrs']['round_phase'], 'early', 'first 15 s is early')
    eq(k[1]['attrs']['round_phase'], 'mid', 'after that is mid')
    eq(k[2]['attrs']['round_phase'], 'postplant', 'after the plant is post-plant')
    ok(not k[0]['attrs']['bomb_planted'], 'bomb not down yet')
    ok(k[2]['attrs']['bomb_planted'], 'bomb down')
    eq(k[2]['attrs']['t_since_plant'], 10.0, 'seconds since the plant')
    eq(k[0]['attrs']['bomb_site'], 'A', 'bombsite_a normalises to A')
    # The parser writes the literal 'not_planted' on unplanted rounds; that is
    # an absence, not a third bombsite, and must not become a facet bucket.
    eq(moments._site_label('not_planted'), None, "'not_planted' is not a site")
    eq(moments._site_label('bombsite_b'), 'B', 'bombsite_b normalises to B')
    eq(moments._site_label(None), None, 'no site is no site')
    eq(k[0]['attrs']['buy_self'], 'full', 'CT full buy from their own economy')
    eq(k[0]['attrs']['buy_enemy'], 'eco', 'and the enemy eco from theirs')
    ok(k[0]['attrs']['won_round'], 'CT won, so the CT subject won the round')
    d = kinds(idx, 'death')[0]
    ok(not d['attrs']['won_round'], 'the T victim did not')


def test_halftime_swap_keeps_team_identity():
    """After the swap the literal side flips but the fixed team identity must
    not - otherwise every cross-half query silently splits one team in two."""
    rounds = [{'round_num': 1, 'start': 0, 'freeze_end': 10, 'end': 1000,
               'winner': 'ct', 'reason': 'ct_killed', 'economy': {}},
              {'round_num': 13, 'start': 2000, 'freeze_end': 2010, 'end': 3000,
               'winner': 'ct', 'reason': 'ct_killed', 'economy': {}}]
    k1 = {'tick': 500, 'round_num': 1, 'attacker_name': 'a1', 'attacker_side': 'ct',
          'victim_name': 'b1', 'victim_side': 't', 'weapon': 'weapon_ak47', 'headshot': False}
    # Round 13: a1 is now T, b1 is now CT.
    k2 = {'tick': 2500, 'round_num': 13, 'attacker_name': 'a1', 'attacker_side': 't',
          'victim_name': 'b1', 'victim_side': 'ct', 'weapon': 'weapon_ak47', 'headshot': False}
    idx = moments.build_match_index(make_match([k1, k2], rounds), META)
    k = kinds(idx, 'kill')
    eq([m['team'] for m in k], [1, 1], 'a1 stays team 1 across the swap')
    eq([m['side'] for m in k], ['ct', 't'], 'but their literal side flips')
    eq([m['attrs']['half'] for m in k], [1, 2], 'halves are labelled')
    # Round 13 was won by the literal CT, who is now team 2.
    eq(k[1]['attrs']['won_round'], False, 'a1 (team 1) lost round 13')


def test_round_moments():
    idx = build([kill(30 * TR, 1, 'a1', 'b1')])
    r = kinds(idx, 'round')
    eq(len(r), 1, 'one round moment per round')
    eq(r[0]['attrs']['buy_ct'], 'pistol', 'round 1 is a pistol round whatever the economy says')
    live = build([kill(30 * TR, 5, 'a1', 'b1')],
                 rounds=[{'round_num': 5, 'start': 0, 'freeze_end': 20 * TR,
                          'end': 100 * TR, 'winner': 'ct', 'reason': 'ct_killed',
                          'economy': {'ct_equip': 25000, 't_equip': 1000}}])
    rr = kinds(live, 'round')[0]['attrs']
    eq((rr['buy_ct'], rr['buy_t']), ('full', 'eco'), 'round row carries both buys')


# ── utility ───────────────────────────────────────────────────────────────────
def test_flash_effect_join():
    data = make_match([kill(30 * TR, 1, 'a1', 'b1')])
    data['flashes'] = [{'tick': 25 * TR, 'X': 1.0, 'Y': 2.0, 'thrower_name': 'a1'}]
    data['blind'] = [
        {'tick': 25 * TR + 2, 'attacker_name': 'a1', 'victim_name': 'b1',
         'victim_side': 't', 'blind_duration': 3.5},
        {'tick': 25 * TR + 3, 'attacker_name': 'a1', 'victim_name': 'b2',
         'victim_side': 't', 'blind_duration': 1.5},
        {'tick': 25 * TR + 4, 'attacker_name': 'a1', 'victim_name': 'a2',
         'victim_side': 'ct', 'blind_duration': 2.0},
    ]
    f = kinds(moments.build_match_index(data, META), 'flash')[0]
    eq(f['attrs']['enemies_flashed'], 2, 'two enemies blinded')
    eq(f['attrs']['friends_flashed'], 1, 'one teammate blinded')
    eq(f['attrs']['blind_time'], 5.0, 'enemy blind time is summed')
    eq(f['attrs']['friendly_blind_time'], 2.0, 'teammate blind time is kept apart')
    ok('2 enemies blinded' in f['detail'], 'detail reads naturally')


def test_utility_effect_counters_always_exist():
    """A flash that blinded nobody must report 0, not a missing key - two
    encodings of "nothing happened" is how a filter silently returns the
    wrong set."""
    data = make_match([kill(30 * TR, 1, 'a1', 'b1')])
    data['flashes'] = [{'tick': 25 * TR, 'X': 0, 'Y': 0, 'thrower_name': 'a1'}]
    data['he'] = [{'tick': 26 * TR, 'X': 0, 'Y': 0, 'thrower_name': 'a1'}]
    idx = moments.build_match_index(data, META)
    f = kinds(idx, 'flash')[0]
    eq(f['attrs']['enemies_flashed'], 0, 'no enemies blinded reads as 0')
    eq(f['attrs']['friends_flashed'], 0, 'no teammates blinded reads as 0')
    h = kinds(idx, 'he')[0]
    eq(h['attrs']['damage'], 0, 'an HE that hit nothing reads as 0 damage')
    eq(h['attrs']['team_damage'], 0, 'and 0 team damage')


def test_he_damage_join():
    data = make_match([kill(90 * TR, 1, 'a1', 'b1')])
    data['he'] = [{'tick': 30 * TR, 'X': 0, 'Y': 0, 'thrower_name': 'a1'}]
    data['damage'] = [
        {'tick': 30 * TR + 1, 'round_num': 1, 'attacker_name': 'a1', 'attacker_side': 'ct',
         'victim_name': 'b1', 'victim_side': 't', 'dmg_health': 40, 'weapon': 'hegrenade'},
        {'tick': 30 * TR + 2, 'round_num': 1, 'attacker_name': 'a1', 'attacker_side': 'ct',
         'victim_name': 'a2', 'victim_side': 'ct', 'dmg_health': 15, 'weapon': 'hegrenade'},
        {'tick': 30 * TR + 3, 'round_num': 1, 'attacker_name': 'a1', 'attacker_side': 'ct',
         'victim_name': 'b2', 'victim_side': 't', 'dmg_health': 20, 'weapon': 'weapon_ak47'},
    ]
    h = kinds(moments.build_match_index(data, META), 'he')[0]
    eq(h['attrs']['damage'], 40, 'enemy grenade damage counted')
    eq(h['attrs']['team_damage'], 15, 'team damage kept separate')
    eq(h['attrs']['enemies_hit'], 1, 'one enemy hit')


def test_utility_without_round_num_is_bucketed_by_tick():
    """Smokes/flashes carried no round_num before the 2026-08 re-parse; the
    index must still place them in the right round."""
    rounds = [{'round_num': 1, 'start': 0, 'freeze_end': 10, 'end': 1000,
               'winner': 'ct', 'reason': 'x', 'economy': {}},
              {'round_num': 2, 'start': 2000, 'freeze_end': 2010, 'end': 3000,
               'winner': 't', 'reason': 'x', 'economy': {}}]
    data = make_match([], rounds)
    data['smokes'] = [{'start_tick': 2500, 'end_tick': 2900, 'X': 0, 'Y': 0,
                       'thrower_name': 'a1'}]
    s = kinds(moments.build_match_index(data, META), 'smoke')[0]
    eq(s['round'], 2, 'a smoke with no round_num lands in the round its tick is in')


def test_parser_round_num_wins_over_the_tick_lookup():
    rounds = [{'round_num': 1, 'start': 0, 'freeze_end': 10, 'end': 1000,
               'winner': 'ct', 'reason': 'x', 'economy': {}},
              {'round_num': 2, 'start': 2000, 'freeze_end': 2010, 'end': 3000,
               'winner': 't', 'reason': 'x', 'economy': {}}]
    data = make_match([], rounds)
    data['smokes'] = [{'start_tick': 2500, 'end_tick': 2900, 'X': 0, 'Y': 0,
                       'thrower_name': 'a1', 'round_num': 1}]
    s = kinds(moments.build_match_index(data, META), 'smoke')[0]
    eq(s['round'], 1, "the parser's own round_num is trusted when present")


# ── index header ──────────────────────────────────────────────────────────────
def test_header_takes_mode_and_date_from_meta_not_the_json():
    """The parser declares `mode`/`timestamp` on MatchOutput but writes them
    empty; they live in the filename. Reading them from the JSON makes every
    mode filter match nothing."""
    data = make_match([kill(30 * TR, 1, 'a1', 'b1')])
    data['mode'] = ''
    data['timestamp'] = ''
    idx = moments.build_match_index(data, META)
    eq(idx['mode'], 'mm', 'mode comes from the filename metadata')
    eq(idx['played_epoch'], META['played_epoch'], 'so does the played date')
    # Both names are canonical, so a match stored under the legacy `de_dust`
    # spelling indexes, facets and filters as the same map as a `de_dust2` one.
    eq(idx['map'], 'de_dust2', "map comes from the match document, canonicalised")
    eq(idx['map_key'], 'de_dust2', 'map_key comes from the filename, canonicalised')
    eq(idx['v'], moments.MOMENTS_SCHEMA, 'schema version stamped for cache invalidation')


def test_caps_report_missing_kill_coordinates():
    idx = build([kill(30 * TR, 1, 'a1', 'b1')])
    eq(idx['caps']['kill_coords'], False, 'old data cannot answer positional queries')
    idx2 = build([kill(30 * TR, 1, 'a1', 'b1', victim_x=100.0, victim_y=200.0)])
    eq(idx2['caps']['kill_coords'], True, 're-parsed data can')


def test_empty_match_does_not_crash():
    idx = moments.build_match_index(
        {'mapName': 'de_dust2', 'rounds': [], 'kills': []}, META)
    eq(idx['moments'], [], 'a match with nothing in it indexes to nothing')


# ══ query engine ══════════════════════════════════════════════════════════════

def corpus():
    rounds = [{'round_num': 1, 'start': 0, 'freeze_end': 20 * TR, 'end': 200 * TR,
               'winner': 'ct', 'reason': 'ct_killed', 'bomb_plant': 60 * TR,
               'bomb_site': 'bombsite_a',
               'economy': {'ct_equip': 25000, 't_equip': 1000}}]
    kills = [
        kill(25 * TR, 1, 'a1', 'b1', headshot=True, weapon='weapon_awp'),
        kill(30 * TR, 1, 'a2', 'b2', weapon='weapon_ak47', through_smoke=True),
        kill(70 * TR, 1, 'b3', 'a1', weapon='weapon_deagle'),
    ]
    return [build(kills, rounds)]


def run(spec):
    return query.evaluate(spec, corpus())


def test_query_unit_selects_perspective():
    eq(run({'unit': 'kill'})['total'], 3, 'three kills')
    eq(run({'unit': 'death'})['total'], 3, 'three deaths')
    eq(run({'unit': 'duel'})['total'], 6, 'both perspectives is six rows')


def test_query_and_or_not():
    # The exact shape the old filter could not express.
    spec = {'unit': 'kill', 'where': {'all': [
        {'f': 'side', 'op': 'eq', 'v': 'ct'},
        {'any': [{'f': 'weapon_class', 'op': 'eq', 'v': 'sniper'},
                 {'f': 'through_smoke', 'op': 'is', 'v': True}]},
        {'not': {'f': 'bomb_planted', 'op': 'is', 'v': True}},
    ]}}
    eq(run(spec)['total'], 2, 'AND of an OR and a NOT')

    spec_and = {'unit': 'kill', 'where': {'all': [
        {'f': 'weapon_class', 'op': 'eq', 'v': 'sniper'},
        {'f': 'through_smoke', 'op': 'is', 'v': True}]}}
    eq(run(spec_and)['total'], 0,
       'a true AND intersects - the old filter could only ever union')


def test_query_operators():
    eq(run({'unit': 'kill', 'where': {'f': 'hs', 'op': 'is', 'v': True}})['total'], 1, 'is true')
    eq(run({'unit': 'kill', 'where': {'f': 'hs', 'op': 'is', 'v': False}})['total'], 2, 'is false')
    eq(run({'unit': 'kill', 'where': {'f': 'player', 'op': 'in', 'v': ['a1', 'a2']}})['total'], 2, 'in')
    eq(run({'unit': 'kill', 'where': {'f': 'player', 'op': 'nin', 'v': ['a1', 'a2']}})['total'], 1, 'nin')
    eq(run({'unit': 'kill', 'where': {'f': 't_into_round', 'op': 'between', 'v': [0, 15]}})['total'], 2, 'between')
    eq(run({'unit': 'kill', 'where': {'f': 't_into_round', 'op': 'gt', 'v': 15}})['total'], 1, 'gt')
    eq(run({'unit': 'kill', 'where': {'f': 'weapon', 'op': 'contains', 'v': 'awp'}})['total'], 1,
       'contains is case-insensitive')
    eq(run({'unit': 'kill', 'where': {'f': 'assist', 'op': 'exists'}})['total'], 0, 'exists on an absent field')


def test_query_is_false_does_not_match_absent():
    """A smoke has no `hs`. `hs is false` must not sweep it up - that's how a
    "non-headshot kills" filter silently starts including utility."""
    data = make_match([kill(30 * TR, 1, 'a1', 'b1')])
    data['smokes'] = [{'start_tick': 30 * TR, 'end_tick': 40 * TR, 'X': 0, 'Y': 0,
                       'thrower_name': 'a1'}]
    idxs = [moments.build_match_index(data, META)]
    r = query.evaluate({'unit': 'utility', 'where': {'f': 'hs', 'op': 'is', 'v': False}}, idxs)
    eq(r['total'], 0, 'an absent attribute is not false')


def test_query_empty_where_matches_everything():
    eq(run({'unit': 'kill', 'where': None})['total'], 3, 'no conditions is the whole corpus')
    eq(run({'unit': 'kill', 'where': {'all': []}})['total'], 3, 'an empty all matches')


def test_query_rejects_unknown_field_and_unit():
    try:
        run({'unit': 'kill', 'where': {'f': 'nonsense', 'op': 'eq', 'v': 1}})
        ok(False, 'unknown field should raise')
    except query.QueryError as e:
        ok('nonsense' in str(e), 'the error names the offending field')
    try:
        run({'unit': 'nonsense'})
        ok(False, 'unknown unit should raise')
    except query.QueryError as e:
        ok('nonsense' in str(e), 'the error names the offending unit')


def test_query_scope():
    idxs = corpus()
    eq(query.evaluate({'unit': 'kill', 'scope': {'maps': ['de_dust2']}}, idxs)['total'], 3,
       'scope by canonical map name')
    eq(query.evaluate({'unit': 'kill', 'scope': {'maps': ['de_dust']}}, idxs)['total'], 3,
       'a link shared under the legacy map name still selects the same matches')
    eq(query.evaluate({'unit': 'kill', 'scope': {'maps': ['de_nuke']}}, idxs)['total'], 0,
       'a map nothing was played on returns nothing')
    eq(query.evaluate({'unit': 'kill', 'scope': {'modes': ['comp']}}, idxs)['total'], 0, 'scope by mode')


def test_query_scope_by_match_file():
    """`scope.files` is how the analyser expresses "just this match" - the
    difference between analysing the match you came from and its whole map."""
    idxs = corpus()
    me = META['file']
    eq(query.evaluate({'unit': 'kill', 'scope': {'files': [me]}}, idxs)['total'], 3,
       'scoping to this match keeps its moments')
    eq(query.evaluate({'unit': 'kill', 'scope': {'files': ['someone_else.json']}}, idxs)['total'], 0,
       'scoping to another match returns nothing from this one')
    eq(query.evaluate({'unit': 'kill', 'scope': {'files': [me, 'someone_else.json']}}, idxs)['total'], 3,
       'a set scope is a union over the files it names')
    # files AND maps intersect rather than widening.
    eq(query.evaluate({'unit': 'kill',
                       'scope': {'files': [me], 'maps': ['de_nuke']}}, idxs)['total'], 0,
       'scope keys intersect - a file on the wrong map is still out')


def test_query_scope_ignores_unknown_keys():
    """The client carries a `kind` sentinel inside `scope` so that "all maps"
    - which sets no other scope key - survives a URL round-trip. That only
    works if the engine ignores keys it doesn't know, so pin it."""
    idxs = corpus()
    plain = query.evaluate({'unit': 'kill'}, idxs)['total']
    eq(query.evaluate({'unit': 'kill', 'scope': {'kind': 'all'}}, idxs)['total'], plain,
       'an all-maps sentinel restricts nothing')
    eq(query.evaluate({'unit': 'kill',
                       'scope': {'kind': 'match', 'files': [META['file']]}}, idxs)['total'], 3,
       'and rides alongside the keys that do restrict')


def test_query_scanned_reports_the_scoped_set():
    """`scanned` is the denominator the results header prints ("N of M moments
    in scope"), so it has to be the size of the SCOPE, not of the corpus."""
    idxs = corpus()
    wide = query.evaluate({'unit': 'kill'}, idxs)
    narrow = query.evaluate({'unit': 'kill', 'scope': {'files': ['nothing.json']}}, idxs)
    ok(wide['scanned'] > 0, 'an unscoped query scans the corpus')
    eq(narrow['scanned'], 0, 'a scope that excludes every match scans nothing')


def test_query_limit_does_not_truncate_the_numbers():
    r = query.evaluate({'unit': 'kill', 'limit': 1}, corpus())
    eq(len(r['rows']), 1, 'rows are capped')
    eq(r['total'], 3, 'but the total counts every hit')
    eq(r['aggregates']['n'], 3, 'and the aggregates describe the full set, not the page')
    ok(r['truncated'], 'truncation is reported')


def test_query_aggregates_and_wilson():
    r = run({'unit': 'kill'})
    a = r['aggregates']
    eq(a['n'], 3, 'moment count')
    eq(a['n_rounds'], 1, 'one round touched')
    eq(a['n_players'], 3, 'three distinct subjects')
    hs = [x for x in a['rates'] if x['key'] == 'hs'][0]
    eq((hs['k'], hs['n']), (1, 3), 'rates carry raw k/n, not just a percentage')
    ok(hs['ci'][0] < hs['p'] < hs['ci'][1], 'the Wilson interval brackets the estimate')
    ok(0.0 <= hs['ci'][0] and hs['ci'][1] <= 1.0, 'and stays inside [0,1]')


def test_query_facets():
    f = run({'unit': 'kill'})['facets']
    ok('by_player' in f, 'player facet returned')
    eq(sum(v['n'] for v in f['by_player']['values']), 3, 'facet counts sum to the result set')
    weapons = {v['value']: v['n'] for v in f['by_weapon']['values']}
    eq(weapons.get('AWP'), 1, 'facets are readable weapon labels, not weapon_ ids')


def test_query_rows_are_self_contained():
    """A result row is rendered in a cross-match list, so everything the row
    needs - match file, map, clip window, focus player - must travel with it."""
    row = run({'unit': 'kill'})['rows'][0]
    for k in ('match', 'match_stem', 'map', 'round', 'tick', 'start_tick',
              'end_tick', 'player', 'label', 'detail', 'attrs', 'type'):
        ok(k in row, 'row carries %s' % k)


def test_presets_all_valid():
    idxs = corpus()
    for p in query.PRESETS:
        try:
            query.evaluate(p['spec'], idxs)
        except query.QueryError as e:
            ok(False, 'preset %r is not a valid query: %s' % (p['id'], e))
    ok(True, 'every shipped preset parses and runs')


def test_query_breakdown_splits_and_aggregates_each_bucket():
    r = run({'unit': 'kill', 'group_by': 'by_player'})
    b = r['breakdown']
    eq(b['by'], 'by_player', 'the breakdown names its dimension')
    eq(len(b['groups']), 3, 'one bucket per distinct player')
    eq(sum(g['n'] for g in b['groups']), r['total'],
       'bucket counts sum to the result set')
    ok(b['groups'][0]['n'] >= b['groups'][-1]['n'], 'biggest bucket first')
    a1 = [g for g in b['groups'] if g['value'] == 'a1'][0]
    hs = [x for x in a1['rates'] if x['key'] == 'hs'][0]
    eq((hs['k'], hs['n']), (1, 1), "a bucket's rate is computed within the bucket")
    ok(hs['ci'] is not None, 'and carries its own interval, not the global one')
    ok('time_hist' not in a1, 'the per-bucket histogram is dropped from the payload')


def test_query_breakdown_reports_rows_the_dimension_misses():
    # Utility moments have no bomb site here, so the bucket table would
    # otherwise not add up to the result count - that has to be stated.
    r = run({'unit': 'kill', 'group_by': 'by_site'})
    b = r['breakdown']
    eq(sum(g['n'] for g in b['groups']) + b['skipped'], r['total'],
       'every row is either in a bucket or counted as skipped')


def test_query_breakdown_rejects_an_unknown_dimension():
    try:
        run({'unit': 'kill', 'group_by': 'by_nonsense'})
        ok(False, 'an unknown breakdown should be an error, not an empty table')
    except query.QueryError as e:
        ok('by_player' in str(e), 'the error names the dimensions that do exist')


def test_query_without_group_by_has_no_breakdown():
    ok('breakdown' not in run({'unit': 'kill'}),
       'the breakdown is only computed when one is asked for')


def coords_corpus():
    """The same fixture with kill coordinates, as a post-2026-08-19 parse
    emits them (attacker + victim world positions on every kill)."""
    rounds = [{'round_num': 1, 'start': 0, 'freeze_end': 20 * TR, 'end': 200 * TR,
               'winner': 'ct', 'reason': 'ct_killed',
               'economy': {'ct_equip': 25000, 't_equip': 1000}}]
    kills = [
        kill(25 * TR, 1, 'a1', 'b1', attacker_x=100.0, attacker_y=-200.0,
             victim_x=150.0, victim_y=-250.0),
        kill(30 * TR, 1, 'a2', 'b2', attacker_x=-300.0, attacker_y=400.0,
             victim_x=-320.0, victim_y=420.0),
        kill(70 * TR, 1, 'b3', 'a1', attacker_x=0.0, attacker_y=0.0,
             victim_x=10.0, victim_y=10.0),
    ]
    return [build(kills, rounds)]


def test_query_points_cover_every_hit_not_just_the_page():
    r = query.evaluate({'unit': 'kill', 'limit': 1, 'points': True}, coords_corpus())
    eq(len(r['rows']), 1, 'the row page is still capped')
    pts = r['points']
    eq(len(pts['pts']) + pts['missing'], r['total'],
       'every hit is either plotted or counted as unplottable')
    ok(all(p.get('map') for p in pts['pts']),
       'each point carries its map, so a cross-map result cannot be drawn on one radar')


def test_query_points_skip_moments_that_have_no_position():
    # A round moment describes a round, not a spot - as do clutches and
    # multi-kills. Defaulting them to (0,0) would burn a permanent hotspot
    # into the corner of the radar.
    r = query.evaluate({'unit': 'round', 'points': True}, coords_corpus())
    ok(r['total'] > 0, 'the fixture really does contain round moments')
    eq(r['points']['pts'], [], 'they contribute no points')
    eq(r['points']['missing'], r['total'], 'and are all reported as missing')


def test_query_points_are_opt_in():
    ok('points' not in query.evaluate({'unit': 'kill'}, coords_corpus()),
       'no coordinate payload unless it was asked for')


def test_field_registry_hides_ungated_fields():
    reg = query.field_registry({'kill_coords': False})
    ok('distance' not in reg, 'a field the corpus cannot answer is not offered')
    ok('distance' in query.field_registry({'kill_coords': True}), 'and is offered once it can')
    ok(all('path' not in f for f in reg.values()), 'internal paths are not leaked to the client')


# ── kill distance ─────────────────────────────────────────────────────────────
def test_distance_is_computed_from_the_coordinates():
    """The parser has never emitted a `distance` field, so reading one gave
    None forever while the UI offered the filter and advertised a mean."""
    k = kill(30 * TR, 1, 'a1', 'b1',
             attacker_x=0.0, attacker_y=0.0, attacker_z=0.0,
             victim_x=3.0, victim_y=4.0, victim_z=0.0)
    idx = build([k])
    eq(kinds(idx, 'kill')[0]['attrs']['distance'], 5.0, '3-4-5 triangle')


def test_distance_is_three_dimensional():
    k = kill(30 * TR, 1, 'a1', 'b1',
             attacker_x=0.0, attacker_y=0.0, attacker_z=0.0,
             victim_x=0.0, victim_y=3.0, victim_z=4.0)
    eq(kinds(build([k]), 'kill')[0]['attrs']['distance'], 5.0,
       'a height difference counts - Nuke and Vertigo are not flat')


def test_distance_is_absent_not_zero_on_an_old_parse():
    idx = build([kill(30 * TR, 1, 'a1', 'b1')])
    eq(kinds(idx, 'kill')[0]['attrs']['distance'], None,
       'no coordinates means not measured, which is not the same as 0')
    eq(idx['caps']['kill_coords'], False, 'and the cap says so')


# ── the forward-looking trade ─────────────────────────────────────────────────
def test_death_row_knows_it_was_traded():
    """b1 kills a1; a2 kills b1 two seconds later. a1 was traded."""
    idx = build([kill(30 * TR, 1, 'b1', 'a1'), kill(32 * TR, 1, 'a2', 'b1')])
    deaths = {d['player']: d for d in kinds(idx, 'death')}
    eq(deaths['a1']['attrs']['traded'], True, 'a1 was avenged')
    eq(deaths['a1']['attrs']['traded_by'], 'a2', 'by a2')
    eq(deaths['a1']['attrs']['traded_latency'], 2.0, 'two seconds later')
    eq(deaths['b1']['attrs']['traded'], False, 'b1 was not avenged by anyone')


def test_trade_stays_a_property_of_the_kill_on_both_rows():
    """The deliberate NON-change. `trade` describes the kill, so it reads the
    same from either side of it; redefining it per perspective would silently
    blend two populations under the shipped "Trade kill %" rate, whose
    denominator spans kills and deaths alike."""
    idx = build([kill(30 * TR, 1, 'b1', 'a1'), kill(32 * TR, 1, 'a2', 'b1')])
    avenging_kill = [k for k in kinds(idx, 'kill') if k['tick'] == 32 * TR][0]
    victim_row = [d for d in kinds(idx, 'death') if d['tick'] == 32 * TR][0]
    eq(avenging_kill['attrs']['trade'], True, 'the kill was a trade')
    eq(victim_row['attrs']['trade'], True, 'and it still was, seen from the victim')
    eq(victim_row['attrs'].get('traded'), False,
       'but b1 was not themselves avenged - the two questions are separate')


def test_a_death_outside_the_window_is_not_traded():
    idx = build([kill(30 * TR, 1, 'b1', 'a1'), kill(40 * TR, 1, 'a2', 'b1')])
    deaths = {d['player']: d for d in kinds(idx, 'death')}
    eq(deaths['a1']['attrs']['traded'], False, '10 s later is not a trade')


def test_a_teammate_killing_someone_else_is_not_a_trade():
    idx = build([kill(30 * TR, 1, 'b1', 'a1'), kill(31 * TR, 1, 'a2', 'b2')])
    deaths = {d['player']: d for d in kinds(idx, 'death')}
    eq(deaths['a1']['attrs']['traded'], False, 'the killer has to be the one who dies')


# ── trade moments ─────────────────────────────────────────────────────────────
def test_trade_row_names_the_fallen_teammate():
    idx = build([kill(30 * TR, 1, 'b1', 'a1'), kill(32 * TR, 1, 'a2', 'b1')])
    rows = kinds(idx, 'trade')
    eq(len(rows), 1, 'one trade')
    t = rows[0]
    eq(t['player'], 'a2', 'the subject is the trader')
    eq(t['other'], 'b1', 'against the enemy they killed')
    eq(t['attrs']['partner'], 'a1', 'the partner is who they avenged')
    eq(t['attrs']['trade_latency'], 2.0, 'and how long it took')
    eq(t['attrs']['mates'], ['a1'], 'the teammate is in mates')


def test_a_plain_kill_makes_no_trade_row():
    eq(len(kinds(build([kill(30 * TR, 1, 'a1', 'b1')]), 'trade')), 0,
       'nothing to avenge, nothing to record')


# ── mates and opps ────────────────────────────────────────────────────────────
def test_mates_and_opps_on_an_assisted_kill():
    idx = build([kill(30 * TR, 1, 'a1', 'b1', assister_name='a2')])
    k = kinds(idx, 'kill')[0]
    d = kinds(idx, 'death')[0]
    eq(k['attrs']['mates'], ['a2'], 'the assister is a teammate of the killer')
    eq(k['attrs']['opps'], ['b1'], 'the victim is the opponent')
    eq(d['attrs']['opps'], ['a1', 'a2'],
       'from the victim, both the killer and the assister are opponents')


def test_every_row_carries_both_lists():
    """[] and absent must not be two ways of saying "nobody helped"."""
    idx = build([kill(30 * TR, 1, 'a1', 'b1')],
                smokes=[{'start_tick': 25 * TR, 'end_tick': 40 * TR,
                         'X': 1.0, 'Y': 2.0, 'thrower_name': 'a3'}])
    missing = [m['kind'] for m in idx['moments']
               if 'mates' not in m['attrs'] or 'opps' not in m['attrs']]
    eq(missing, [], 'every kind has both lists')
    smoke = kinds(idx, 'smoke')[0]
    eq(smoke['attrs']['mates'], [], 'a lone smoke involved nobody else')


def test_clutch_carries_the_surviving_opponents():
    ks = [kill((30 + i) * TR, 1, 'b1', a) for i, a in enumerate(['a1', 'a2', 'a3', 'a4'])]
    idx = build(ks)
    c = [m for m in kinds(idx, 'clutch') if m['team'] == 1][0]
    eq(c['player'], 'a5', 'a5 is alone')
    eq(c['attrs']['opps'], sorted(T), 'and these are the five they had to beat')


def test_multikill_carries_its_victims():
    ks = [kill((30 + i) * TR, 1, 'a1', v) for i, v in enumerate(['b1', 'b2', 'b3'])]
    mk = kinds(build(ks), 'multikill')[0]
    eq(mk['attrs']['opps'], ['b1', 'b2', 'b3'], 'the sequence, not just its length')


# ── flash support ─────────────────────────────────────────────────────────────
def _flash_match(blind_rows, kills_rows):
    return build(kills_rows, blind=blind_rows)


def test_flash_support_names_the_teammate():
    idx = _flash_match(
        [{'tick': 30 * TR, 'attacker_name': 'a2', 'attacker_side': 'ct',
          'victim_name': 'b1', 'victim_side': 't', 'blind_duration': 2.0}],
        [kill(31 * TR, 1, 'a1', 'b1')])
    rows = kinds(idx, 'support')
    eq(len(rows), 1, 'one support')
    eq(rows[0]['player'], 'a2', 'the subject is the flasher')
    eq(rows[0]['attrs']['partner'], 'a1', 'the partner is who converted it')
    eq(rows[0]['other'], 'b1', 'against the blinded enemy')
    k = kinds(idx, 'kill')[0]
    eq(k['attrs']['set_up_by'], 'a2', 'and the kill row names its setter')
    ok('a2' in k['attrs']['mates'], 'who is also a teammate on that row')


def test_a_self_flash_is_not_support():
    """31 of the corpus's naive matches are this - a player flashing themselves
    into their own kill, which is skill, not teamplay."""
    idx = _flash_match(
        [{'tick': 30 * TR, 'attacker_name': 'a1', 'attacker_side': 'ct',
          'victim_name': 'b1', 'victim_side': 't', 'blind_duration': 2.0}],
        [kill(31 * TR, 1, 'a1', 'b1')])
    eq(len(kinds(idx, 'support')), 0, 'the flasher cannot be the killer')


def test_an_enemy_flash_is_not_support():
    idx = _flash_match(
        [{'tick': 30 * TR, 'attacker_name': 'b2', 'attacker_side': 't',
          'victim_name': 'b1', 'victim_side': 't', 'blind_duration': 2.0}],
        [kill(31 * TR, 1, 'a1', 'b1')])
    eq(len(kinds(idx, 'support')), 0, 'the flasher has to be on the killer\'s team')


def test_a_flash_that_had_worn_off_is_not_support():
    idx = _flash_match(
        [{'tick': 30 * TR, 'attacker_name': 'a2', 'attacker_side': 'ct',
          'victim_name': 'b1', 'victim_side': 't', 'blind_duration': 0.5}],
        [kill(33 * TR, 1, 'a1', 'b1')])
    eq(len(kinds(idx, 'support')), 0,
       'a flash that merely happened first enabled nothing')


def test_flash_rows_count_the_kills_they_enabled():
    idx = build([kill(31 * TR, 1, 'a1', 'b1')],
                blind=[{'tick': 30 * TR, 'attacker_name': 'a2', 'attacker_side': 'ct',
                        'victim_name': 'b1', 'victim_side': 't', 'blind_duration': 2.0}],
                flashes=[{'tick': 30 * TR, 'X': 1.0, 'Y': 2.0, 'thrower_name': 'a2'}])
    f = kinds(idx, 'flash')[0]
    eq(f['attrs']['enabled_kills'], 1, 'the flash converted once')
    ok('a1' in f['attrs']['mates'], 'and knows who converted it')


def test_a_flash_that_enabled_nothing_says_zero_not_nothing():
    idx = build([], flashes=[{'tick': 30 * TR, 'X': 1.0, 'Y': 2.0, 'thrower_name': 'a2'}])
    eq(kinds(idx, 'flash')[0]['attrs']['enabled_kills'], 0,
       '0 and absent must not both mean "led to nothing"')


# ── crossfire ─────────────────────────────────────────────────────────────────
def _dmg(tick, atk, vic, hp=30):
    return {'tick': tick, 'round_num': 1, 'attacker_name': atk,
            'attacker_side': 'ct' if atk in CT else 't', 'victim_name': vic,
            'victim_side': 'ct' if vic in CT else 't', 'dmg_health': hp,
            'weapon': 'weapon_ak47'}


def test_crossfire_pairs_two_teammates_on_one_enemy():
    idx = build([], damage=[_dmg(30 * TR, 'a1', 'b1'), _dmg(31 * TR, 'a2', 'b1')])
    rows = kinds(idx, 'crossfire')
    eq(len(rows), 1, 'one crossfire')
    eq(rows[0]['player'], 'a1', 'subject is whoever shot first')
    eq(rows[0]['attrs']['partner'], 'a2', 'partner is who joined them')
    eq(rows[0]['other'], 'b1', 'on the shared enemy')
    eq(rows[0]['attrs']['damage'], 60, 'their damage together')


def test_one_player_shooting_twice_is_not_a_crossfire():
    idx = build([], damage=[_dmg(30 * TR, 'a1', 'b1'), _dmg(31 * TR, 'a1', 'b1')])
    eq(len(kinds(idx, 'crossfire')), 0, 'it takes two')


def test_crossfire_needs_the_two_to_be_close_in_time():
    idx = build([], damage=[_dmg(30 * TR, 'a1', 'b1'), _dmg(40 * TR, 'a2', 'b1')])
    eq(len(kinds(idx, 'crossfire')), 0, 'ten seconds apart is two separate fights')


def test_a_duo_on_one_enemy_is_recorded_once():
    idx = build([], damage=[_dmg(30 * TR, 'a1', 'b1'), _dmg(31 * TR, 'a2', 'b1'),
                            _dmg(32 * TR, 'a1', 'b1'), _dmg(33 * TR, 'a2', 'b1')])
    eq(len(kinds(idx, 'crossfire')), 1,
       'a long firefight is one piece of teamplay, not four')


# ── executes ──────────────────────────────────────────────────────────────────
def _util(tick, thrower, x=0.0, y=0.0):
    return {'start_tick': tick, 'end_tick': tick + 100, 'X': x, 'Y': y,
            'thrower_name': thrower}


def _nade(tick, thrower, x=0.0, y=0.0):
    return {'tick': tick, 'X': x, 'Y': y, 'thrower_name': thrower}


def _exec_match(**extra):
    return build([], **extra)


def test_execute_needs_three_pieces_from_two_players():
    idx = _exec_match(smokes=[_util(30 * TR, 'b1')],
                      flashes=[_nade(31 * TR, 'b2'), _nade(32 * TR, 'b2')])
    rows = kinds(idx, 'execute')
    eq(len(rows), 1, 'three pieces, two throwers, one smoke')
    eq(rows[0]['player'], None, 'an execute is a thing a team did')
    eq(rows[0]['team'], 2, 'and the team is named')
    eq(sorted(rows[0]['attrs']['mates']), ['b1', 'b2'], 'everyone who threw')
    eq(rows[0]['attrs']['util_n'], 3, 'counted')


def test_one_player_throwing_three_nades_is_not_an_execute():
    idx = _exec_match(smokes=[_util(30 * TR, 'b1')],
                      flashes=[_nade(31 * TR, 'b1'), _nade(32 * TR, 'b1')])
    eq(len(kinds(idx, 'execute')), 0, 'that is a setup, not an execute')


def test_an_execute_needs_a_smoke():
    idx = _exec_match(flashes=[_nade(30 * TR, 'b1'), _nade(31 * TR, 'b2')],
                      he=[_nade(32 * TR, 'b1')])
    eq(len(kinds(idx, 'execute')), 0,
       'without the smoke requirement the rate triples and the word stops meaning anything')


def test_an_execute_rejects_scattered_utility():
    idx = _exec_match(smokes=[_util(30 * TR, 'b1', 0.0, 0.0)],
                      flashes=[_nade(31 * TR, 'b2', 5000.0, 0.0),
                               _nade(32 * TR, 'b2', 0.0, 5000.0)])
    eq(len(kinds(idx, 'execute')), 0,
       'three nades across the map inside six seconds is three unrelated things')


def test_an_execute_is_not_double_counted():
    idx = _exec_match(smokes=[_util(30 * TR, 'b1')],
                      flashes=[_nade(30 * TR, 'b2'), _nade(31 * TR, 'b2'),
                               _nade(31 * TR, 'b1'), _nade(32 * TR, 'b2')])
    eq(len(kinds(idx, 'execute')), 1, 'one burst, one row')


def test_execute_site_comes_from_an_observed_plant():
    bomb = [{'tick': 60 * TR, 'event': 'plant', 'X': 0.0, 'Y': 0.0,
             'name': 'b1', 'bombsite': 'A'}]
    idx = _exec_match(smokes=[_util(30 * TR, 'b1', 10.0, 10.0)],
                      flashes=[_nade(31 * TR, 'b2', 10.0, 10.0)],
                      he=[_nade(32 * TR, 'b1', 10.0, 10.0)],
                      bomb=bomb)
    eq(kinds(idx, 'execute')[0]['attrs']['exec_site'], 'A', 'measured, not guessed')


def test_an_execute_far_from_every_plant_is_unlabelled():
    bomb = [{'tick': 60 * TR, 'event': 'plant', 'X': 0.0, 'Y': 0.0,
             'name': 'b1', 'bombsite': 'A'}]
    idx = _exec_match(smokes=[_util(30 * TR, 'b1', 9000.0, 9000.0)],
                      flashes=[_nade(31 * TR, 'b2', 9000.0, 9000.0)],
                      he=[_nade(32 * TR, 'b1', 9000.0, 9000.0)],
                      bomb=bomb)
    eq(kinds(idx, 'execute')[0]['attrs']['exec_site'], None,
       'unlabelled is honest; "A" would be a guess you cannot see')


# ── caps ──────────────────────────────────────────────────────────────────────
def test_caps_report_a_parse_without_blind_events():
    idx = build([kill(30 * TR, 1, 'a1', 'b1')])
    eq(idx['caps']['blind_pairs'], False, 'no blind[] means flash support is unanswerable')
    idx2 = build([kill(30 * TR, 1, 'a1', 'b1')],
                 blind=[{'tick': 29 * TR, 'attacker_name': 'a2', 'attacker_side': 'ct',
                         'victim_name': 'b1', 'victim_side': 't', 'blind_duration': 1.0}])
    eq(idx2['caps']['blind_pairs'], True, 'and answerable once they exist')


# ── the has operator ──────────────────────────────────────────────────────────
def _teamplay_corpus():
    idx = build([kill(30 * TR, 1, 'b1', 'a1'), kill(32 * TR, 1, 'a2', 'b1')])
    return [idx]


def test_query_has_matches_a_list_member():
    r = query.evaluate({'unit': 'trade',
                        'where': {'all': [{'f': 'with_player', 'op': 'has', 'v': 'a1'}]}},
                       _teamplay_corpus())
    eq(r['total'], 1, 'the trade names a1 as the teammate avenged')


def test_query_has_does_not_substring_match():
    """The exact false positive `contains` produces, and the reason `has`
    exists: `contains "a"` against ['a1'] is true through the letter."""
    r = query.evaluate({'unit': 'trade',
                        'where': {'all': [{'f': 'with_player', 'op': 'has', 'v': 'a'}]}},
                       _teamplay_corpus())
    eq(r['total'], 0, 'a prefix is not a member')


def test_query_contains_on_a_list_checks_elements_not_the_repr():
    r = query.evaluate({'unit': 'trade',
                        'where': {'all': [{'f': 'with_player', 'op': 'contains', 'v': "['"}]}},
                       _teamplay_corpus())
    eq(r['total'], 0, 'the brackets and quotes of the repr are not content')


def test_query_nhas_is_tristate():
    corpus = _teamplay_corpus()
    have = query.evaluate({'unit': 'trade',
                           'where': {'all': [{'f': 'with_player', 'op': 'nhas', 'v': 'zz'}]}},
                          corpus)
    eq(have['total'], 1, 'a present list without that name matches nhas')
    absent = query.evaluate({'unit': 'kill',
                             'where': {'all': [{'f': 'partner', 'op': 'has', 'v': 'a1'}]}},
                            corpus)
    eq(absent['total'], 0, 'a scalar field is not a list and matches neither')


def test_query_two_has_leaves_are_an_and():
    idx = build([kill(30 * TR, 1, 'a1', 'b1', assister_name='a2')])
    both = query.evaluate({'unit': 'kill', 'where': {'all': [
        {'f': 'vs_player', 'op': 'has', 'v': 'b1'},
        {'f': 'with_player', 'op': 'has', 'v': 'a2'}]}}, [idx])
    eq(both['total'], 1, 'both conditions hold on the one kill')
    neither = query.evaluate({'unit': 'kill', 'where': {'all': [
        {'f': 'with_player', 'op': 'has', 'v': 'a2'},
        {'f': 'with_player', 'op': 'has', 'v': 'a3'}]}}, [idx])
    eq(neither['total'], 0, 'and two on one field mean BOTH, not either')


# ── multi-valued facets and breakdowns ────────────────────────────────────────
def test_facet_by_mate_counts_a_row_under_every_teammate():
    idx = build([kill(30 * TR, 1, 'b1', 'a1'), kill(32 * TR, 1, 'a2', 'b1')])
    f = query.evaluate({'unit': 'trade'}, [idx])['facets']['by_mate']
    eq(f['multi'], True, 'the block declares itself multi-valued')
    eq(f['rows'], 1, 'and reports how many moments produced the counts')


def test_breakdown_by_mate_reports_that_buckets_overlap():
    idx = build([kill(30 * TR, 1, 'a1', 'b1', assister_name='a2')])
    b = query.evaluate({'unit': 'kill', 'group_by': 'by_mate'}, [idx])['breakdown']
    eq(b['multi'], True, 'declared')
    eq(b['n_rows'], 1, 'one moment')
    eq(b['placements'], 1, 'in one bucket')
    ok('skipped' in b, 'and rows with no teammate are still reported')


def test_breakdown_single_valued_dimensions_still_partition():
    """The regression guard: the multi extension must not leak into the
    dimensions whose column a reader is entitled to add up."""
    idx = build([kill(30 * TR, 1, 'a1', 'b1'), kill(40 * TR, 1, 'a2', 'b2')])
    r = query.evaluate({'unit': 'kill', 'group_by': 'by_weapon'}, [idx])
    b = r['breakdown']
    eq(sum(g['n'] for g in b['groups']) + b['skipped'], r['total'],
       'buckets plus skipped equals the result count')
    ok(not b.get('multi'), 'and it is not flagged as overlapping')



for _fn in list(globals().values()):
    if callable(_fn) and getattr(_fn, '__name__', '').startswith('test_'):
        _fn()

print('\n%d passed, %d failed' % (_passed, _failed))
sys.exit(1 if _failed else 0)
