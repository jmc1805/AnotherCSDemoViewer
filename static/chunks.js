/**
 * chunks.js - browser-side tick-chunk source for the 2D pages.
 * Requires chunks.logic.js (window.ChunksLogic) loaded first.
 *
 * ChunkSource.create(matchData) returns:
 *   entries   unified chunk index (chunkIndexV2 preferred, else chunkIndex)
 *   isV2      whether the binary path is active
 *   step      tick spacing of stored snapshots (synthetic-timeline entries)
 *   fetchDict(idx) → Promise<{tickStr: [playerRec]}>  (cached per source)
 *
 * The v2 path decodes tick chunks in a Web Worker
 * (static/decoder.js - a core file that is the single place mirroring
 * cmd/parser/chunks_v2.go).
 */
'use strict';

const ChunkSource = (() => {
    let worker = null;
    let nextId = 1;
    const pending = new Map();   // id -> {resolve, reject}

    function getWorker() {
        if (worker) return worker;
        worker = new Worker('/static/decoder.js');
        worker.onmessage = ({ data }) => {
            const p = pending.get(data.id);
            if (!p) return;
            pending.delete(data.id);
            if (data.error) p.reject(new Error(data.error));
            else p.resolve(data.chunk);
        };
        worker.onerror = e => {
            // Fail every in-flight decode; the next fetchDict spawns a fresh worker.
            for (const p of pending.values()) p.reject(new Error(`decoder worker: ${e.message || 'error'}`));
            pending.clear();
            worker = null;
        };
        return worker;
    }

    function decodeInWorker(url) {
        return new Promise((resolve, reject) => {
            const id = nextId++;
            pending.set(id, { resolve, reject });
            getWorker().postMessage({ id, url });
        });
    }

    function create(matchData) {
        const { entries, isV2 } = ChunksLogic.unifiedIndex(matchData);
        const step = ChunksLogic.effectiveStep(matchData, isV2);
        const blindIndex = isV2 ? ChunksLogic.buildBlindIndex(matchData.blind) : null;
        const roundNumAt = isV2 ? ChunksLogic.buildRoundLookup(matchData.rounds) : null;
        const cache = new Map();   // idx -> Promise<dict>

        function fetchDict(idx) {
            if (cache.has(idx)) return cache.get(idx);
            const info = entries[idx];
            if (!info) return Promise.resolve({});
            const p = (isV2
                ? decodeInWorker(info.file).then(chunk =>
                      ChunksLogic.v2ToTickDict(chunk, blindIndex, roundNumAt))
                : fetch(info.file).then(r => {
                      if (!r.ok) throw new Error(`Chunk ${idx} fetch failed: HTTP ${r.status}`);
                      return r.json();
                  })
            ).catch(err => { cache.delete(idx); throw err; });
            cache.set(idx, p);
            return p;
        }

        return { entries, isV2, step, fetchDict };
    }

    return { create };
})();
