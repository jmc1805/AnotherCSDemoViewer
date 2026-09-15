/**
 * matchhero.js - DOM glue for the "hero" summary band shared by the stats
 * page, 2D replay viewer, and multi-round analyser: map icon/name/mode, avg
 * rank pill (real skill-group icon when static/assets/ is present, else text),
 * and rounds/kills/total-damage pills. Pure math lives in matchhero.logic.js.
 *
 * Every page embeds the same markup (see templates/match.html's #match-hero
 * block) and calls MatchHero.update(opts) once its data is ready:
 *   { mapRaw, mode, ranks, rounds, kills, totalDmg }
 * `rounds`/`kills`/`totalDmg` are plain numbers - callers that show more than
 * one match (the multi-round analyser) pass running sums across every match
 * loaded so far; ranks is the flat array to average (accumulated the same way).
 * Showing/hiding the #match-hero container is left to the caller, since pages
 * reveal it alongside other once-loaded UI.
 *
 * Loads after static/maps.logic.js, which turns the raw map name into the one
 * a human reads, and optionally after static/assets.js - no assets means the
 * icons simply stay hidden and the text labels carry the band.
 */
(function () {
    'use strict';
    const LOGIC = (typeof MatchHeroLogic !== 'undefined') ? MatchHeroLogic : null;

    // FACEIT match room. The id is the `1-<uuid>` prefix of the demo's filename.
    const FACEIT_ROOM_URL = 'https://www.faceit.com/en/cs2/room/';

    function update(opts) {
        opts = opts || {};
        const mapRaw = opts.mapRaw || '';
        const mapLabel = MapsLogic.labelUpper(mapRaw);

        const mapEl = document.getElementById('hero-map');
        if (mapEl) mapEl.textContent = mapLabel;
        // The filename's mode token is only what the uploader called the match,
        // so a Premier match arrived here labelled MM. The ranks this band is
        // already given say what the demo itself was - see
        // MatchHeroLogic.modeLabel(). Fixes all three callers (match page,
        // replayer, analyser) in one place, since each passes its own ranks.
        const modeEl = document.getElementById('hero-mode');
        if (modeEl) {
            const label = LOGIC ? LOGIC.modeLabel(opts.mode, opts.ranks)
                                : String(opts.mode || '').toUpperCase();
            modeEl.textContent = label;
            // A FACEIT demo knows its own room id (cmd/parser reads it off the
            // .dem filename) but NOT the players' elo - that number exists only
            // in FACEIT's API, not anywhere in the demo. So the label links to
            // the page that has it rather than the app inventing one. This adds
            // an <a> the user can click; the page still fetches nothing.
            if (opts.faceitId) {
                const a = document.createElement('a');
                a.href = FACEIT_ROOM_URL + encodeURIComponent(opts.faceitId);
                a.target = '_blank';
                a.rel = 'noopener noreferrer';
                a.textContent = label;
                a.title = 'Open this match on faceit.com - player elo lives there, not in the demo';
                // Styled here rather than in each of the three templates that
                // embed this band: it is one element on one line.
                a.style.color = 'inherit';
                a.style.textDecoration = 'none';
                a.style.borderBottom = '1px dotted currentColor';
                modeEl.textContent = '';
                modeEl.appendChild(a);
            }
        }

        const mapIco = document.getElementById('hero-map-ico');
        if (mapIco) {
            const mapIcoUrl = window.Assets ? Assets.url('map_icons', mapRaw) : null;
            if (mapIcoUrl) { mapIco.src = mapIcoUrl; mapIco.alt = mapLabel; mapIco.style.display = ''; }
            else { mapIco.style.display = 'none'; }
        }

        const roundsEl = document.getElementById('hero-rounds');
        if (roundsEl) roundsEl.textContent = (opts.rounds || 0) + ' rounds';
        const killsEl = document.getElementById('hero-kills');
        if (killsEl) killsEl.textContent = (opts.kills || 0) + ' kills';
        const dmgEl = document.getElementById('hero-dmg');
        if (dmgEl) dmgEl.textContent = FmtLogic.num(opts.totalDmg) + ' total dmg';

        const rankEl = document.getElementById('hero-rank');
        if (rankEl && LOGIC) {
            const ar = LOGIC.computeAvgRank(opts.ranks);
            if (!ar) {
                rankEl.style.display = 'none';
            } else {
                const pref = ar.rankType === 11 ? 'Premier · ' : ar.rankType === 7 ? 'Wingman · ' : 'Competitive · ';
                // Premier is a rating, not a skill group, so it has no badge
                // image - it gets the rating-coloured chip instead, sized from
                // the same --rank-badge-h as the image would have been. Before
                // this it was bare text, which read as a stray number beside
                // the caption rather than as the match's average rank.
                const ico = (window.Assets && ar.rankType !== 11) ? Assets.url('skillgroups', ar.roundAvg) : null;
                const tier = LOGIC.rankTier(ar.rankType, ar.roundAvg);
                rankEl.title = pref + ar.label + ` · avg of ${ar.n} ranked`;
                rankEl.style.display = 'flex';
                rankEl.innerHTML = '<span class="hero-rank-cap">Avg Rank</span>' +
                    (ico ? `<img class="rank-ico" src="${ico}" alt="${ar.label}">`
                         : `<span class="rank-chip ${tier}">${ar.label}</span>`);
            }
        }
    }

    window.MatchHero = { update };
})();
