/**
 * assets.js - browser glue for the CS2 UI image assets (skill-group icons, map
 * icons, overhead maps, equipment icons). Loads static/assets/manifest.json once
 * and hands out URLs + decode-once <img> elements. When the manifest is absent
 * (assets never extracted - the default state), every lookup returns null and
 * callers fall back to their text label / CSS chip. See assets.logic.js for the
 * pure resolution logic.
 *
 * Perf posture (repo rule - "low impact on RAM/performance"): the manifest is
 * fetched exactly once; each image is decoded exactly once into a shared cache
 * and reused by every consumer/frame. Never `new Image()` per frame or per row.
 */
(function () {
    'use strict';
    const LOGIC = (typeof AssetsLogic !== 'undefined') ? AssetsLogic : null;
    // app.py's /assets/ route, which serves paths.ASSETS_DIR wherever it lives
    // (static/assets/ in a checkout, the user's data dir in the desktop build).
    const BASE = '/assets/';

    let manifest = null;         // parsed manifest.json, or null when absent
    let ready = null;            // in-flight/resolved load promise (fetch once)
    const imgCache = new Map();  // relPath → HTMLImageElement (decode once)

    // Kick off (or reuse) the one manifest fetch. Resolves to true if assets are
    // available, false otherwise. Never rejects - a missing manifest is normal.
    function load() {
        if (ready) return ready;
        // 'no-cache' → always revalidate with the server (cheap 304 when
        // unchanged, fresh 200 after a re-extract). force-cache would pin a stale
        // manifest and never pick up newly extracted assets.
        ready = fetch(BASE + 'manifest.json', { cache: 'no-cache' })
            .then(r => (r.ok ? r.json() : null))
            .then(m => { manifest = (m && m.kinds) ? m : null; return !!manifest; })
            .catch(() => { manifest = null; return false; });
        return ready;
    }

    function available() { return !!manifest; }

    // Relative path for (kind, key), or null. Requires load() to have resolved;
    // synchronous so it's cheap to call inside a render loop.
    function path(kind, key) {
        if (!manifest || !LOGIC) return null;
        return LOGIC.resolve(manifest, kind, key);
    }

    // Absolute URL for (kind, key), or null.
    function url(kind, key) {
        const p = path(kind, key);
        return p ? BASE + p : null;
    }

    // A decode-once HTMLImageElement for (kind, key), or null. Cached by path so
    // repeated lookups (every scoreboard row, every canvas frame) share one image.
    function img(kind, key) {
        const p = path(kind, key);
        if (!p) return null;
        let im = imgCache.get(p);
        if (!im) {
            im = new Image();
            im.decoding = 'async';
            im.src = BASE + p;
            imgCache.set(p, im);
        }
        return im;
    }

    // Convenience: the skill-group icon URL for a parser ranks[] entry, or null.
    function rankIconUrl(rankEntry) {
        if (!LOGIC) return null;
        return url('skillgroups', LOGIC.skillGroupKey(rankEntry));
    }

    // The tier-tinted Premier banner URL for a CS Rating, or null.
    function premierBannerUrl(rating) {
        if (!LOGIC) return null;
        return url('premier', LOGIC.premierBannerKey(rating));
    }

    // Publish the Premier banners to CSS rather than to callers.
    //
    // A Premier rating is not a self-contained image the way a skill group is:
    // it is a banner with the number drawn ON it, so it cannot be an <img> swap
    // like rankIconUrl(). Every surface that shows a rank already emits
    // `<span class="rank-chip p-gold">17,900</span>` (match/players/player/hero/
    // preprocessor - five hand-rolled copies), so handing the URLs to
    // rankbadge.css as custom properties upgrades all five at once and keeps the
    // no-assets fallback exactly as it was. The marker class is what the
    // stylesheet gates on; without it nothing changes.
    function publishPremierBanners() {
        if (!manifest || !manifest.kinds || !manifest.kinds.premier) return;
        const el = document.documentElement;
        let n = 0;
        for (const key of ['0', '1', '2', '3', '4', '5', '6', 'none']) {
            const u = url('premier', key);
            if (u) { el.style.setProperty('--rank-banner-' + key, 'url("' + u + '")'); n++; }
        }
        if (n) el.classList.add('has-premier-banner');
    }

    window.Assets = { load, available, path, url, img, rankIconUrl, premierBannerUrl };

    // Begin loading immediately so the manifest is ready by first render; callers
    // that need to await it can still `Assets.load().then(...)`.
    load().then(ok => { if (ok) publishPremierBanners(); });
}());
