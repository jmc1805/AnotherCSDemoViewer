/**
 * matchhero.logic.js - pure computation behind the "hero" summary band shared
 * by the stats page, 2D replay viewer, and multi-round analyser (map name +
 * mode, avg rank, rounds/kills/damage pills). No DOM: unit-testable under node
 * (test/matchhero.logic.test.mjs). DOM glue lives in static/matchhero.js.
 *
 * Every function is total and side-effect free.
 */
(function (root, factory) {
    if (typeof module === 'object' && module.exports) module.exports = factory(require('./fmt.logic.js'));
    else root.MatchHeroLogic = factory(root.FmtLogic);
}(typeof self !== 'undefined' ? self : this, function (FmtLogic) {
    'use strict';

    // Competitive / Wingman skill-group names, indexed by rank so index 0 is
    // the "Unranked" placeholder and rank n sits at index n. Mirrored
    // server-side by match_summary.py's _COMP_RANK_NAMES; the two are
    // duplicated because the browser cannot import Python, and
    // test/matchhero.logic.test.mjs asserts they still agree.
    const CS_SKILL_GROUPS = ['Unranked','Silver I','Silver II','Silver III','Silver IV',
        'Silver Elite','Silver Elite Master','Gold Nova I','Gold Nova II','Gold Nova III',
        'Gold Nova Master','Master Guardian I','Master Guardian II','Master Guardian Elite',
        'Distinguished Master Guardian','Legendary Eagle','Legendary Eagle Master',
        'Supreme Master First Class','The Global Elite'];

    // CS2 Premier rating colour tiers (the in-game rating badge colours), as
    // CSS class names. One definition for every page that paints a rating -
    // the match page's rank chips, the matches dashboard's Avg Rank column -
    // and mirrored server-side by app.py's _ow_band_tier() for Overwatch's
    // rank bands, which colour ranges rather than single ratings.
    function premierTier(rating) {
        const r = rating || 0;
        if (r < 5000)  return 'p-gray';
        if (r < 10000) return 'p-lblue';
        if (r < 15000) return 'p-blue';
        if (r < 20000) return 'p-purple';
        if (r < 25000) return 'p-pink';
        if (r < 30000) return 'p-red';
        return 'p-gold';
    }

    // Chip tier class for any rank: a Premier rating gets its rating colour,
    // Competitive/Wingman skill groups share the one 'sg' gold, anything else
    // has no tier.
    function rankTier(rankType, rank) {
        if (rankType === 11) return premierTier(rank);
        if (rankType === 12 || rankType === 7) return 'sg';
        return '';
    }

    // Average match rank across ranked players - mirrors match_summary.avg_rank():
    // average within the dominant rank type, format accordingly. Returns
    // {rankType, avg, roundAvg, label, n} or null when no player is ranked.
    function computeAvgRank(ranks) {
        const byType = {};
        (ranks || []).forEach(r => {
            const rk = r.rank || 0;
            if (rk > 0) (byType[r.rank_type || 0] = byType[r.rank_type || 0] || []).push(rk);
        });
        const types = Object.keys(byType);
        if (!types.length) return null;
        const rt = +types.reduce((a, b) => byType[b].length > byType[a].length ? b : a);
        const vals = byType[rt];
        const avg = vals.reduce((s, v) => s + v, 0) / vals.length;
        const roundAvg = Math.round(avg);
        let label;
        if (rt === 11) label = FmtLogic.rating(roundAvg);                       // Premier rating
        else if (rt === 12 || rt === 7)                                         // Competitive / Wingman
            label = (CS_SKILL_GROUPS[roundAvg] || ('Rank ' + roundAvg)) + (rt === 7 ? ' (WM)' : '');
        else label = String(roundAvg);
        return { rankType: rt, avg, roundAvg, label, n: vals.length };
    }

    // Replays `damage` events grouped by (round, victim) in tick order, tracking
    // that life's remaining health from 100, and calls cb(event, actualDamage)
    // for each hit with actualDamage clamped to health-remaining-at-that-instant.
    //
    // Why: the game's raw dmg_health on a killing blow can exceed the health the
    // victim actually had left (e.g. a 108-damage headshot on a 26-HP target),
    // because it reports the shot's theoretical damage, not what was removed.
    // The standard stat-site "ADR" clamps each hit to the health actually
    // removed before summing, which is what makes a number here comparable to
    // one from those sites. This is the single implementation of that clamp,
    // so match.html, the viewer and the multi-round analyser cannot quote
    // three different ADRs for the same match.
    function forEachClampedHit(damageEvents, cb) {
        const lives = {};
        (damageEvents || []).forEach(d => {
            if (!d.attacker_name || !d.victim_name || d.attacker_name === d.victim_name) return;
            const key = d.round_num + '|' + d.victim_name;
            (lives[key] = lives[key] || []).push(d);
        });
        Object.values(lives).forEach(evs => {
            evs.sort((a, b) => (a.tick || 0) - (b.tick || 0));
            let health = 100;
            evs.forEach(d => {
                const raw = d.dmg_health || 0;
                const actual = Math.max(0, Math.min(raw, health));
                health = Math.max(0, health - raw);
                cb(d, actual);
            });
        });
    }

    // Total match damage (all players, all victims), clamped per the above.
    function totalDamage(damageEvents) {
        let total = 0;
        forEachClampedHit(damageEvents, (d, actual) => { total += actual; });
        return total;
    }

    // ── Mode label ────────────────────────────────────────────────────────
    // Browser twin of match_summary.premier_ranks_present() / mode_label().
    // Keep the two in step: the dashboard labels a match server-side from the
    // summary index, every other page labels it here, and one saying PREM
    // while the other says MM is exactly the bug this replaced.
    const PREMIER_RANK_TYPE = 11;   // 12 = Competitive, 7 = Wingman

    function premierRanksPresent(ranks) {
        if (!Array.isArray(ranks)) return false;
        return ranks.some(r => r && r.rank_type === PREMIER_RANK_TYPE && r.rank);
    }

    // What a match's mode should be LABELLED, uppercased.
    //
    // `mode` is the token from the match filename, i.e. whatever was chosen at
    // upload ('mm' by default) - it says what the uploader called the match.
    // A ranks[] row's rank_type comes from the demo itself, so a Premier rating
    // is the better evidence and wins. ANY such row counts, not the dominant
    // type: rank rows are absent for unranked players, so a Premier match can
    // surface a single rating, and one is proof. `rank: 0` is "unranked" and
    // must not promote a match.
    // Mode tokens the DEMO decided rather than the uploader: cmd/parser reads
    // the server name out of the header, so 'faceit' is already the match's own
    // account of where it was played and nothing in ranks[] can outrank it. A
    // FACEIT match is not a Valve Premier match whatever rank rows it carries
    // (in practice it carries none - every FACEIT player is rank_type -1).
    // Twin of match_summary.DEMO_DECIDED_MODES.
    const DEMO_DECIDED_MODES = ['faceit'];

    function modeLabel(mode, ranks) {
        const token = String(mode || '').trim();
        if (DEMO_DECIDED_MODES.indexOf(token.toLowerCase()) >= 0) return token.toUpperCase();
        if (premierRanksPresent(ranks)) return 'PREM';
        return token.toUpperCase();
    }

    return { CS_SKILL_GROUPS, premierTier, rankTier, computeAvgRank,
             forEachClampedHit, totalDamage, premierRanksPresent, modeLabel,
             PREMIER_RANK_TYPE, DEMO_DECIDED_MODES };
}));
