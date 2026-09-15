/**
 * v2 chunk decoder - Web Worker.
 * Fetches a .bin.br chunk (browser decompresses via Content-Encoding: br) and
 * decodes the SoA delta streams into flat typed arrays, transferred back
 * zero-copy. Must mirror cmd/parser/chunks_v2.go exactly.
 *
 * Decoded layout: all per-record streams are "present players only,
 * snapshot-major"; recOff[s] is the record index where snapshot s starts.
 * Positions/eyeZ stay u16-quantized (dequantize with min/max at render time);
 * yaw stays u16 (65536ths of 360°); pitch u16 ((v/65535)*180-90).
 */
'use strict';

function decode(buf) {
    const dv = new DataView(buf);
    let off = 0;
    const u8 = () => dv.getUint8(off++);
    const u16 = () => { const v = dv.getUint16(off, true); off += 2; return v; };
    const u32 = () => { const v = dv.getUint32(off, true); off += 4; return v; };
    const u64 = () => { const v = dv.getBigUint64(off, true); off += 8; return v; };
    const f32 = () => { const v = dv.getFloat32(off, true); off += 4; return v; };
    const str = n => {
        const s = new TextDecoder().decode(new Uint8Array(buf, off, n));
        off += n;
        return s;
    };

    if (str(4) !== 'CS2V') throw new Error('bad magic');
    const version = u8();
    if (version !== 2) throw new Error(`unsupported chunk version ${version}`);
    const posStep = u8();
    const playerCount = u8();
    const weaponCount = u8();
    const tickStart = u32(), tickEnd = u32();
    const snapCount = u32(), angCount = u32();
    const minX = f32(), minY = f32(), minZ = f32();
    const maxX = f32(), maxY = f32(), maxZ = f32();

    const players = [];
    for (let i = 0; i < playerCount; i++) {
        const steamId = u64().toString();
        const side = u8();          // 0=ct 1=t 2=other (table side; flags bit5 is authoritative)
        players.push({ steamId, side: side === 0 ? 'ct' : side === 1 ? 't' : '?', name: str(u8()) });
    }
    const weapons = [];
    for (let i = 0; i < weaponCount; i++) weapons.push(str(u8()));

    // snapshot ticks (delta) + presence + record offsets
    const snapTicks = new Uint32Array(snapCount);
    for (let i = 0, t = 0; i < snapCount; i++) { t = i === 0 ? u32() : t + u32(); snapTicks[i] = t; }
    const presence = new Uint32Array(snapCount);
    const recOff = new Uint32Array(snapCount + 1);
    let total = 0;
    for (let i = 0; i < snapCount; i++) {
        const m = u32();
        presence[i] = m;
        recOff[i] = total;
        let n = m;
        n = n - ((n >>> 1) & 0x55555555);
        n = (n & 0x33333333) + ((n >>> 2) & 0x33333333);
        total += (((n + (n >>> 4)) & 0x0f0f0f0f) * 0x01010101) >>> 24;
    }
    recOff[snapCount] = total;

    // Delta-stream readers: reconstruct absolute values per player slot.
    function read16(count, offsets, masks) {
        const out = new Uint16Array(count);
        const last = new Uint16Array(32);
        const seen = new Uint8Array(32);
        let r = 0;
        for (let s = 0; s < masks.length; s++) {
            let m = masks[s];
            while (m) {
                const slot = 31 - Math.clz32(m & -m);
                m &= m - 1;
                const raw = u16();
                const v = seen[slot] ? (last[slot] + raw) & 0xffff : raw;
                seen[slot] = 1;
                last[slot] = v;
                out[r++] = v;
            }
        }
        return out;
    }
    function read8(count, masks) {
        const out = new Uint8Array(count);
        const last = new Uint8Array(32);
        const seen = new Uint8Array(32);
        let r = 0;
        for (let s = 0; s < masks.length; s++) {
            let m = masks[s];
            while (m) {
                const slot = 31 - Math.clz32(m & -m);
                m &= m - 1;
                const raw = u8();
                const v = seen[slot] ? (last[slot] + raw) & 0xff : raw;
                seen[slot] = 1;
                last[slot] = v;
                out[r++] = v;
            }
        }
        return out;
    }
    function read32(count, masks) {
        const out = new Uint32Array(count);
        const last = new Uint32Array(32);
        const seen = new Uint8Array(32);
        let r = 0;
        for (let s = 0; s < masks.length; s++) {
            let m = masks[s];
            while (m) {
                const slot = 31 - Math.clz32(m & -m);
                m &= m - 1;
                const raw = u32();
                const v = seen[slot] ? (last[slot] + raw) >>> 0 : raw;
                seen[slot] = 1;
                last[slot] = v;
                out[r++] = v;
            }
        }
        return out;
    }

    const posX = read16(total, recOff, presence);
    const posY = read16(total, recOff, presence);
    const posZ = read16(total, recOff, presence);
    const eyeZ = read16(total, recOff, presence);
    const yaw = read16(total, recOff, presence);
    const pitch = read16(total, recOff, presence);
    const velX = new Int16Array(read16(total, recOff, presence).buffer);
    const velY = new Int16Array(read16(total, recOff, presence).buffer);
    const velZ = new Int16Array(read16(total, recOff, presence).buffer);
    const health = read8(total, presence);
    const armor = read8(total, presence);
    const money = read16(total, recOff, presence);
    const flags = read16(total, recOff, presence);
    const weapon = read8(total, presence);
    const inventory = read32(total, presence);

    // Full-rate angle stream
    const angTicks = new Uint32Array(angCount);
    for (let i = 0, t = 0; i < angCount; i++) { t = i === 0 ? u32() : t + u32(); angTicks[i] = t; }
    const angPresence = new Uint32Array(angCount);
    const angOff = new Uint32Array(angCount + 1);
    let angTotal = 0;
    for (let i = 0; i < angCount; i++) {
        const m = u32();
        angPresence[i] = m;
        angOff[i] = angTotal;
        let n = m;
        n = n - ((n >>> 1) & 0x55555555);
        n = (n & 0x33333333) + ((n >>> 2) & 0x33333333);
        angTotal += (((n + (n >>> 4)) & 0x0f0f0f0f) * 0x01010101) >>> 24;
    }
    angOff[angCount] = angTotal;
    const angYaw = read16(angTotal, angOff, angPresence);
    const angPitch = read16(angTotal, angOff, angPresence);

    if (off !== buf.byteLength) throw new Error(`trailing bytes: ${buf.byteLength - off}`);

    return {
        posStep, tickStart, tickEnd, players, weapons,
        quant: { minX, minY, minZ, maxX, maxY, maxZ },
        snapTicks, presence, recOff,
        posX, posY, posZ, eyeZ, yaw, pitch, velX, velY, velZ,
        health, armor, money, flags, weapon, inventory,
        angTicks, angPresence, angOff, angYaw, angPitch,
    };
}

self.onmessage = async ({ data }) => {
    const { id, url } = data;
    try {
        const res = await fetch(url);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const chunk = decode(await res.arrayBuffer());
        const transfers = [
            chunk.snapTicks, chunk.presence, chunk.recOff,
            chunk.posX, chunk.posY, chunk.posZ, chunk.eyeZ, chunk.yaw, chunk.pitch,
            chunk.velX, chunk.velY, chunk.velZ,
            chunk.health, chunk.armor, chunk.money, chunk.flags, chunk.weapon, chunk.inventory,
            chunk.angTicks, chunk.angPresence, chunk.angOff, chunk.angYaw, chunk.angPitch,
        ].map(a => a.buffer);
        self.postMessage({ id, chunk }, [...new Set(transfers)]);
    } catch (e) {
        self.postMessage({ id, error: String(e) });
    }
};
