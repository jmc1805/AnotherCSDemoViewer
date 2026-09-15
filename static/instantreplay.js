/**
 * instantreplay.js - embedded "Instant Replay" 2D mini-widget.
 *
 * Renders a short, looping top-down replay of a bounded tick window (a kill,
 * a multi-kill sequence, a clutch) inline in a card/row instead of only
 * linking out to the full /viewer page. Reuses the same building blocks
 * viewer.js does (ChunkSource, Match2DLogic, Viewport/ViewportLogic) but is
 * a self-contained, independently-instantiable module - several can be
 * mounted on one page at once (e.g. several moment rows expanded together),
 * unlike viewer.js which is a page-global singleton.
 *
 * Requires (loaded first): match2d.logic.js, viewport.logic.js, viewport.js,
 * chunks.logic.js, chunks.js. Optional, feature-detected: match2d.js (spray
 * cones), utilfx.logic.js + utilfx.js (smoke/fire clouds) - absent, the clip
 * simply renders without those effects.
 *
 * InstantReplay.mount(container, {
 *   matchFile,            // logical '<id>.json' name
 *   matchData,            // optional - pass the already-fetched match JSON
 *                          // to skip a redundant /data/<matchFile> fetch
 *   startTick, endTick,   // clip bounds (analysis/clipwindow.py convention)
 *   focusPlayer,          // name to center the camera on and highlight
 *   caption,              // optional short text shown under the canvas
 *   width, height,        // optional canvas size in px (default 340 square) -
 *                          // the player page sizes it to match the in-game
 *                          // clip box so the two sit side by side as a pair
 * }) -> { play, pause, destroy }
 */
const InstantReplay = (() => {
    'use strict';

    const CANVAS_SIZE   = 340;
    const WORLD_SPAN     = 900;   // world units visible across the mini canvas
    const TICKRATE       = 64;
    const LOOP_PAUSE_MS  = 650;   // pause at the end of the clip before looping
    const CT_COLOR = '#4a9eff', T_COLOR = '#ffaa22';
    const ZOOM_MIN = 0.15, ZOOM_MAX = 6;   // multiples of the auto "fit" zoom
    const ZOOM_STEP = 1.3;
    const GRENADE_LOOKBACK = 96;  // ticks of thrown-grenade trail kept visible
    // Effects live slightly outside the clip window so a flash/HE that pops
    // just before the first frame is still fading when the clip opens.
    const FX_MARGIN = 96;
    const GRENADE_COLORS = {
        Flashbang:    '#ffffaa', HEGrenade:   '#ff6633',
        SmokeGrenade: '#aaccaa', Molotov:     '#ff8800',
        Incendiary:   '#ff8800', Decoy:       '#aaaaff',
    };
    const easeOut = t => 1 - Math.pow(1 - t, 2);

    const matchCache = new Map();   // matchFile -> Promise<matchData>

    /**
     * Fetch and memoise one match document for the inline previews.
     * @param {string} matchFile  e.g. 'de_dust2_….json'
     * @returns {Promise<Object>} the slim match JSON, shared by every widget
     *   on the page so N rows cost one fetch.
     */
    function getMatchData(matchFile) {
        if (!matchCache.has(matchFile)) {
            matchCache.set(matchFile, fetch(`/data/${matchFile}`)
                .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
                .catch(err => { matchCache.delete(matchFile); throw err; }));
        }
        return matchCache.get(matchFile);
    }

    /**
     * Mount a small looping 2D replay of one moment.
     * @param {HTMLElement} container
     * @param {Object} opts
     * @param {string} opts.match       match file
     * @param {number} opts.startTick   window start
     * @param {number} opts.endTick     window end
     * @param {string} [opts.focus]     player to centre on
     * @returns {{destroy: Function}} always destroy() before removing the
     *   container - the widget holds a rAF loop and a chunk fetch.
     */
    function mount(container, opts) {
        const { matchFile, startTick, endTick, focusPlayer, caption } = opts;
        const CW = opts.width  || CANVAS_SIZE;
        const CH = opts.height || opts.width || CANVAS_SIZE;
        let matchData   = opts.matchData || null;
        let destroyed   = false;
        let chunkIndex  = [];
        let tickDict    = {};
        let frames      = [];      // sorted tick numbers within [startTick, endTick]
        let frameIdx    = 0;
        let mapImg      = new Image();
        let mapConfig   = null;
        let mapReady    = false;
        let view        = ViewportLogic.create({ minScale: 0.3, maxScale: 30 });
        let rafId       = null;
        let lastFrameTime = 0;
        let pauseUntil  = 0;
        let playing     = false;
        // Zoom is a multiple of the auto "fit" zoom recomputed every frame by
        // frameView() - storing the factor (not an absolute scale) keeps the
        // camera locked on the focus player while the user zooms out to see
        // where the utility/teammates are.
        let userZoom    = 1;
        // Round effects, sliced to this clip's window at load time.
        let fx = { smokes: [], infernos: [], infernoWeapons: [], flashes: [],
                   he: [], shots: [], trails: [] };
        const hasUtilFX = typeof UtilFX !== 'undefined' && typeof UtilFXLogic !== 'undefined';

        container.innerHTML = '';
        const wrap = document.createElement('div');
        wrap.className = 'instant-replay';
        // Head row matching .clip-box-head: when a 2D preview and a recorded
        // in-game clip are open side by side they are one matched pair, and
        // each needs to say which it is. See static/momentrow.css.
        const head = document.createElement('div');
        head.className = 'ir-head';
        head.innerHTML = '<span class="dot"></span>2D replay';
        const canvas = document.createElement('canvas');
        canvas.width = CW; canvas.height = CH;
        canvas.className = 'ir-canvas';
        const ctx = canvas.getContext('2d');
        const controls = document.createElement('div');
        controls.className = 'ir-controls';
        const playBtn = document.createElement('button');
        playBtn.type = 'button';
        playBtn.className = 'ir-btn';
        playBtn.textContent = '⏸';
        const zoomOutBtn = document.createElement('button');
        zoomOutBtn.type = 'button';
        zoomOutBtn.className = 'ir-btn';
        zoomOutBtn.textContent = '−';
        zoomOutBtn.title = 'Zoom out (mouse wheel)';
        const zoomInBtn = document.createElement('button');
        zoomInBtn.type = 'button';
        zoomInBtn.className = 'ir-btn';
        zoomInBtn.textContent = '+';
        zoomInBtn.title = 'Zoom in (mouse wheel)';
        const zoomResetBtn = document.createElement('button');
        zoomResetBtn.type = 'button';
        zoomResetBtn.className = 'ir-btn';
        zoomResetBtn.textContent = '⟲';
        zoomResetBtn.title = 'Reset zoom';
        const status = document.createElement('span');
        status.className = 'ir-status';
        status.textContent = 'Loading clip…';
        controls.appendChild(playBtn);
        controls.appendChild(zoomOutBtn);
        controls.appendChild(zoomInBtn);
        controls.appendChild(zoomResetBtn);
        controls.appendChild(status);
        wrap.appendChild(head);
        wrap.appendChild(canvas);
        wrap.appendChild(controls);
        if (caption) {
            const cap = document.createElement('div');
            cap.className = 'ir-caption';
            cap.textContent = caption;
            wrap.appendChild(cap);
        }
        container.appendChild(wrap);

        playBtn.onclick = () => { playing ? pause() : play(); };

        // Zoom keeps the focus player centred (frameView re-derives the pan
        // from the focus position), so there's no pan state to maintain here.
        function setZoom(z) {
            userZoom = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, z));
            if (frames.length) { frameView(); render(); }
        }
        zoomOutBtn.onclick   = () => setZoom(userZoom / ZOOM_STEP);
        zoomInBtn.onclick    = () => setZoom(userZoom * ZOOM_STEP);
        zoomResetBtn.onclick = () => setZoom(1);
        const onWheel = e => {
            e.preventDefault();
            setZoom(userZoom * (e.deltaY < 0 ? ZOOM_STEP : 1 / ZOOM_STEP));
        };
        canvas.addEventListener('wheel', onWheel, { passive: false });

        /**
     * Index of the v2 chunk holding `tick`.
     * @param {number} tick
     * @returns {number} -1 when no chunk covers it
     */
    function chunkIdxForTick(tick) {
            for (let i = 0; i < chunkIndex.length; i++) {
                const c = chunkIndex[i];
                if (tick >= c.tickStart && tick <= c.tickEnd) return i;
            }
            return -1;
        }

        async function init() {
            if (!matchData) matchData = await getMatchData(matchFile);
            if (destroyed) return;

            const mapName = MapsLogic.canonical(matchData.mapName);
            mapConfig = Match2DLogic.mapConfigFor(mapName);
            await new Promise(resolve => {
                mapImg.onload = () => { mapReady = true; resolve(); };
                mapImg.onerror = () => { mapReady = false; resolve(); };
                mapImg.src = MapsLogic.radarUrl(mapName);
            });
            if (destroyed) return;

            const chunkSource = ChunkSource.create(matchData);
            if (chunkSource.entries.length > 0) {
                chunkIndex = chunkSource.entries;
                const idxStart = Math.max(0, chunkIdxForTick(startTick));
                let idxEnd = chunkIdxForTick(endTick);
                if (idxEnd < 0) idxEnd = idxStart;
                const needed = [];
                for (let i = idxStart; i <= idxEnd; i++) needed.push(i);
                const dicts = await Promise.all(needed.map(i => chunkSource.fetchDict(i)));
                tickDict = Object.assign({}, ...dicts);
            } else if (matchData.ticks) {
                tickDict = matchData.ticks;
            }
            if (destroyed) return;

            frames = Object.keys(tickDict).map(Number)
                .filter(t => t >= startTick && t <= endTick)
                .sort((a, b) => a - b);
            if (frames.length === 0) {
                // Clip window missed every stored snapshot (very short window,
                // or data predates chunking) - fall back to whatever's loaded.
                frames = Object.keys(tickDict).map(Number).sort((a, b) => a - b);
            }
            if (frames.length === 0) {
                status.textContent = 'No tick data for this clip.';
                return;
            }

            loadEffects();
            frameIdx = 0;
            frameView();
            status.textContent = '';
            render();
            play();
        }

        // Slice the match-wide event arrays down to what this clip window can
        // actually show. Same lifetime clamping the 2D viewer applies
        // (utilfx.logic.js): the demo's SmokeExpired/InfernoExpired fire well
        // after the visual effect really ends, so end_tick is a ceiling, not
        // the truth.
        function loadEffects() {
            const lo = startTick - FX_MARGIN, hi = endTick + FX_MARGIN;
            const overlaps = e => e.start_tick <= hi && e.end_tick >= lo;
            const inWindow = e => e.tick >= lo && e.tick <= hi;

            if (hasUtilFX) {
                fx.smokes = (matchData.smokes || [])
                    .map(sm => Object.assign({}, sm, {
                        end_tick: UtilFXLogic.clampEnd(sm.start_tick, sm.end_tick,
                                                       UtilFXLogic.SMOKE_LIFETIME_TICKS) }))
                    .filter(overlaps);
                fx.infernos = (matchData.infernos || [])
                    .map(inf => Object.assign({}, inf, {
                        end_tick: UtilFXLogic.clampEnd(inf.start_tick, inf.end_tick,
                                                       UtilFXLogic.FIRE_LIFETIME_TICKS) }))
                    .filter(overlaps);
                fx.infernoWeapons = UtilFXLogic.classifyInfernos(fx.infernos, matchData.grenades || []);
            }
            fx.flashes = (matchData.flashes || []).filter(inWindow);
            fx.he      = (matchData.he      || []).filter(inWindow);
            fx.shots   = (matchData.shots   || []).filter(inWindow)
                                                  .sort((a, b) => a.tick - b.tick);

            // Thrown-grenade trajectories, grouped per projectile so a trail
            // can be drawn behind each one (viewer.js does the same from the
            // flat `grenades` array).
            const byEntity = new Map();
            for (const g of (matchData.grenades || [])) {
                if (g.entity_id == null || g.tick < lo - GRENADE_LOOKBACK || g.tick > hi) continue;
                if (!byEntity.has(g.entity_id)) byEntity.set(g.entity_id, []);
                byEntity.get(g.entity_id).push(g);
            }
            fx.trails = [];
            byEntity.forEach(points => {
                points.sort((a, b) => a.tick - b.tick);
                fx.trails.push(points);
            });
        }

        function currentRecs() {
            return tickDict[String(frames[frameIdx])] || [];
        }

        function frameView() {
            const recs = currentRecs();
            const focus = recs.find(p => p.name === focusPlayer) || recs[0];
            const base = focus
                ? Match2DLogic.worldToCanvas(mapConfig, focus.X, focus.Y)
                : { x: 512, y: 512 };
            const baseSpan = WORLD_SPAN / mapConfig.scale;
            const scale = ViewportLogic.clampScale(view, (CW / baseSpan) * userZoom);
            view.scale = scale;
            view.tx = CW / 2 - base.x * scale;
            view.ty = CH / 2 - base.y * scale;
        }

        const toCanvas = (X, Y) => Match2DLogic.worldToCanvas(mapConfig, X, Y);
        const onCanvas = pos => ViewportLogic.isVisible(view, pos.x, pos.y, CW, CH, 120);

        // Thrown-grenade trails + the projectile head, fading with age.
        function drawGrenades(tick) {
            ctx.save();
            ctx.lineJoin = 'round'; ctx.lineCap = 'round';
            for (const points of fx.trails) {
                const seg = points.filter(g => g.tick <= tick && g.tick >= tick - GRENADE_LOOKBACK);
                if (seg.length < 2) continue;
                const color = GRENADE_COLORS[seg[0].type] || '#ffffff';
                for (let i = 1; i < seg.length; i++) {
                    const p0 = toCanvas(seg[i - 1].X, seg[i - 1].Y);
                    const p1 = toCanvas(seg[i].X, seg[i].Y);
                    if (!onCanvas(p0) && !onCanvas(p1)) continue;
                    ctx.beginPath(); ctx.moveTo(p0.x, p0.y); ctx.lineTo(p1.x, p1.y);
                    ctx.strokeStyle = color;
                    ctx.globalAlpha = Math.max(0, 1 - (tick - seg[i].tick) / GRENADE_LOOKBACK) * 0.85;
                    ctx.lineWidth = 2; ctx.stroke();
                }
                const head = toCanvas(seg[seg.length - 1].X, seg[seg.length - 1].Y);
                if (onCanvas(head)) {
                    ctx.globalAlpha = 1;
                    ctx.beginPath(); ctx.arc(head.x, head.y, 3.5, 0, Math.PI * 2);
                    ctx.fillStyle = color; ctx.fill();
                    ctx.strokeStyle = 'rgba(255,255,255,0.6)'; ctx.lineWidth = 1; ctx.stroke();
                }
            }
            ctx.globalAlpha = 1;
            ctx.restore();
        }

        // Smoke clouds and fire, at their real world-unit radii - the same
        // shared utilfx modules the 2D viewer and multi-round analyser use, so
        // a clip and the full replay show identical geometry.
        function drawUtility(tick) {
            if (!hasUtilFX) return;
            for (const smoke of fx.smokes) {
                if (tick < smoke.start_tick || tick > smoke.end_tick) continue;
                const pos = toCanvas(smoke.X, smoke.Y);
                if (!onCanvas(pos)) continue;
                const holes = UtilFXLogic.smokeHoles(smoke, fx.he, tick).map(h => {
                    const hp = toCanvas(h.x, h.y);
                    return { x: hp.x, y: hp.y, r: h.r / mapConfig.scale, strength: h.strength };
                });
                UtilFX.drawSmokeCloud(ctx, {
                    x: pos.x, y: pos.y,
                    rPx: UtilFXLogic.SMOKE_RADIUS / mapConfig.scale,
                    tick,
                    seed: (smoke.start_tick * 31 + Math.round(smoke.X)) | 0,
                    bloom: UtilFXLogic.smokeBloom(tick - smoke.start_tick),
                    fade: smoke.end_tick - tick < 128 ? (smoke.end_tick - tick) / 128 : 1,
                    holes, label: false,
                });
            }
            for (let i = 0; i < fx.infernos.length; i++) {
                const inf = fx.infernos[i];
                if (tick < inf.start_tick || tick > inf.end_tick) continue;
                const pos = toCanvas(inf.X, inf.Y);
                if (!onCanvas(pos)) continue;
                const weapon = fx.infernoWeapons[i] || 'Molotov';
                let cells = null;
                if (inf.cells && inf.cells.length) {
                    cells = [];
                    for (const c of inf.cells) {
                        if (c.tick > tick) break;   // cells are ignition-ordered
                        const cp = toCanvas(c.X, c.Y);
                        cells.push({ x: cp.x, y: cp.y });
                    }
                }
                UtilFX.drawFire(ctx, {
                    x: pos.x, y: pos.y,
                    rPx: UtilFXLogic.fireSpreadRadius(tick - inf.start_tick,
                                                      UtilFXLogic.infernoMaxRadius(weapon)) / mapConfig.scale,
                    tick,
                    seed: (inf.start_tick * 17 + Math.round(inf.X)) | 0,
                    weapon,
                    alpha: inf.end_tick - tick < 96 ? (inf.end_tick - tick) / 96 : 1,
                    cells,
                });
            }
        }

        function drawHE(tick) {
            for (const he of fx.he) {
                const age = tick - he.tick;
                if (age < 0 || age > Match2DLogic.HE_FADE_TICKS) continue;
                const pos = toCanvas(he.X, he.Y);
                if (!onCanvas(pos)) continue;
                const t = age / Match2DLogic.HE_FADE_TICKS;
                const alpha = easeOut(1 - t);
                const r = 8 + easeOut(t) * 30;
                ctx.beginPath(); ctx.arc(pos.x, pos.y, r, 0, Math.PI * 2);
                ctx.strokeStyle = `rgba(255,180,60,${alpha * 0.9})`; ctx.lineWidth = 2.5; ctx.stroke();
                ctx.beginPath(); ctx.arc(pos.x, pos.y, r * 0.6, 0, Math.PI * 2);
                ctx.fillStyle = `rgba(255,230,100,${alpha * 0.4})`; ctx.fill();
            }
        }

        function drawFlashes(tick) {
            const FADE = TICKRATE * 1.5, MAX_R = 44;
            for (const f of fx.flashes) {
                const age = tick - f.tick;
                if (age < 0 || age > FADE) continue;
                const pos = toCanvas(f.X, f.Y);
                if (!onCanvas(pos)) continue;
                const r = MAX_R * (0.4 + 0.6 * easeOut(Math.min(age / 10, 1)));
                const base = easeOut(1 - age / FADE);
                const alpha = base * (0.85 + Math.sin(age * 0.04) * 0.15);
                const grad = ctx.createRadialGradient(pos.x, pos.y, 0, pos.x, pos.y, r);
                grad.addColorStop(0,   `rgba(255,255,255,${alpha})`);
                grad.addColorStop(0.5, `rgba(235,245,255,${alpha * 0.5})`);
                grad.addColorStop(1,   'rgba(200,230,255,0)');
                ctx.beginPath(); ctx.arc(pos.x, pos.y, r, 0, Math.PI * 2);
                ctx.fillStyle = grad; ctx.fill();
            }
        }

        // Names of players whose shot is recent enough to still show a muzzle
        // cone this frame (SPRAY_DURATION_TICKS, same as the 2D viewer).
        function firingNow(tick) {
            const firing = new Set();
            for (let i = fx.shots.length - 1; i >= 0; i--) {
                const sh = fx.shots[i];
                if (sh.tick > tick) continue;
                if (tick - sh.tick > Match2DLogic.SPRAY_DURATION_TICKS) break;
                firing.add(sh.player_name);
            }
            return firing;
        }

        function drawPlayer(p, firing) {
            const pos = toCanvas(p.X, p.Y);
            const isCT = p.side === 'ct';
            const isFocus = p.name === focusPlayer;
            // Blind: the dot washes out towards white the more flash time the
            // player has left, and wears a soft white halo - same curve the 2D
            // viewer uses (flash_duration is remaining seconds).
            const blind = p.flash_duration > 0
                ? Math.pow(Math.min(p.flash_duration / 2.5, 1), 0.8) : 0;
            const base = isCT ? [74, 158, 255] : [255, 170, 34];
            const color = blind > 0
                ? `rgb(${base.map(c => Math.round(c + (255 - c) * blind)).join(',')})`
                : (isCT ? CT_COLOR : T_COLOR);

            if (p.health <= 0) {
                ctx.save();
                ctx.strokeStyle = color;
                ctx.globalAlpha = 0.8;
                ctx.lineWidth = 2;
                ctx.beginPath();
                ctx.moveTo(pos.x - 5, pos.y - 5); ctx.lineTo(pos.x + 5, pos.y + 5);
                ctx.moveTo(pos.x + 5, pos.y - 5); ctx.lineTo(pos.x - 5, pos.y + 5);
                ctx.stroke();
                ctx.restore();
                return;
            }

            if (isFocus) {
                ctx.save();
                ctx.strokeStyle = '#ffd35c';
                ctx.globalAlpha = 0.8;
                ctx.lineWidth = 2;
                ctx.beginPath(); ctx.arc(pos.x, pos.y, 15, 0, Math.PI * 2); ctx.stroke();
                ctx.restore();
            }

            if (blind > 0) {
                ctx.save();
                ctx.beginPath(); ctx.arc(pos.x, pos.y, 9 + blind * 5, 0, Math.PI * 2);
                ctx.fillStyle = `rgba(255,255,255,${(0.30 * blind).toFixed(3)})`;
                ctx.fill();
                ctx.restore();
            }
            if (firing && firing.has(p.name)) drawSpray(pos, color, p.yaw);
            ctx.beginPath(); ctx.arc(pos.x, pos.y, 7, 0, Math.PI * 2);
            ctx.fillStyle = color; ctx.fill();
            ctx.strokeStyle = 'rgba(255,255,255,0.85)';
            ctx.lineWidth = 1.5; ctx.stroke();

            if (p.yaw != null) {
                const yawRad = (-p.yaw) * (Math.PI / 180);
                const tipX = pos.x + Math.cos(yawRad) * 12;
                const tipY = pos.y + Math.sin(yawRad) * 12;
                const perpX = -Math.sin(yawRad), perpY = Math.cos(yawRad);
                ctx.beginPath();
                ctx.moveTo(tipX, tipY);
                ctx.lineTo(pos.x + Math.cos(yawRad) * 7 + perpX * 3.5, pos.y + Math.sin(yawRad) * 7 + perpY * 3.5);
                ctx.lineTo(pos.x + Math.cos(yawRad) * 7 - perpX * 3.5, pos.y + Math.sin(yawRad) * 7 - perpY * 3.5);
                ctx.closePath();
                ctx.fillStyle = color;
                ctx.fill();
            }
        }

        // Muzzle cone for a player who just fired. match2d.js owns the shape
        // the 2D viewer draws; the inline fallback keeps clips working on a
        // page that doesn't load it.
        function drawSpray(pos, color, yaw) {
            if (yaw == null) return;
            if (typeof Match2D !== 'undefined' && Match2D.drawSpray) { Match2D.drawSpray(ctx, pos, color, yaw); return; }
            const a = (-yaw) * (Math.PI / 180), r = 7, len = 25;
            ctx.save();
            ctx.beginPath();
            ctx.moveTo(pos.x + Math.cos(a) * r, pos.y + Math.sin(a) * r);
            ctx.lineTo(pos.x + Math.cos(a) * (r + len), pos.y + Math.sin(a) * (r + len));
            ctx.setLineDash([4, 3]);
            ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.globalAlpha = 0.9; ctx.stroke();
            ctx.restore();
        }

        // Draw order mirrors viewer.js: map, then thrown utility in flight,
        // then the ground effects it becomes, then players on top of it all.
        function render() {
            const tick = frames.length ? frames[frameIdx] : startTick;
            ctx.setTransform(1, 0, 0, 1, 0, 0);
            ctx.fillStyle = '#0d1117';
            ctx.fillRect(0, 0, CW, CH);
            Viewport.apply(ctx, view);
            if (mapReady) ctx.drawImage(mapImg, 0, 0, 1024, 1024);
            drawGrenades(tick);
            drawUtility(tick);
            drawHE(tick);
            drawFlashes(tick);
            const firing = firingNow(tick);
            currentRecs().forEach(p => drawPlayer(p, firing));
            Viewport.clear(ctx);
        }

        // A CLIP MUST LAST (endTick - startTick) / TICKRATE SECONDS, because the
        // recorded in-game clip of the same moment lasts exactly that - HLAE
        // captures one frame per tick between the same two ticks and ffmpeg muxes
        // them at TICKRATE, so a 256-tick window is 4.000s of video, measured. The
        // preview sits directly beside it in the same media strip (momentrow.css),
        // so any drift here reads as the two boxes disagreeing about the moment.
        //
        // MAX_CATCHUP_FRAMES bounds the work after a real stall (a backgrounded
        // tab stops rAF entirely); past it we resync the clock to now and accept
        // the lost time rather than burning a frame budget replaying it.
        const MAX_CATCHUP_FRAMES = 8;

        function animate(ts) {
            if (destroyed || !playing) return;
            if (ts < pauseUntil) { rafId = requestAnimationFrame(animate); return; }
            if (!lastFrameTime) lastFrameTime = ts;
            let advanced = 0;
            while (true) {
                const nextIdx = frameIdx + 1;
                if (nextIdx >= frames.length) {
                    // End of clip - pause briefly, then loop.
                    pauseUntil = ts + LOOP_PAUSE_MS;
                    frameIdx = 0;
                    frameView();
                    render();
                    lastFrameTime = 0;
                    rafId = requestAnimationFrame(animate);
                    return;
                }
                // Snapshots are evenly spaced (32Hz in v2 chunks, 64Hz in the old
                // JSON ticks), but a gap in the data must not become a long freeze
                // - clamped the same way viewer.js clamps its playback gaps.
                const dtTicks = frames[nextIdx] - frames[frameIdx];
                const intervalMs = Math.min(250, (dtTicks / TICKRATE) * 1000);
                if (ts - lastFrameTime < intervalMs) break;
                frameIdx = nextIdx;
                lastFrameTime += intervalMs;
                if (++advanced >= MAX_CATCHUP_FRAMES) { lastFrameTime = ts; break; }
            }
            if (advanced) { frameView(); render(); }
            rafId = requestAnimationFrame(animate);
        }

        function play() {
            if (frames.length === 0) return;
            playing = true;
            playBtn.textContent = '⏸';
            lastFrameTime = 0;
            pauseUntil = 0;
            if (frameIdx >= frames.length - 1) frameIdx = 0;
            rafId = requestAnimationFrame(animate);
        }

        function pause() {
            playing = false;
            playBtn.textContent = '▶';
            if (rafId) cancelAnimationFrame(rafId);
        }

        function destroy() {
            destroyed = true;
            pause();
            canvas.removeEventListener('wheel', onWheel);
            container.innerHTML = '';
        }

        init().catch(err => {
            status.textContent = `Failed to load clip: ${err.message}`;
        });

        return { play, pause, destroy };
    }

    return { mount };
})();
