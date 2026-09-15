package main

// v2 binary tick chunks - the app's compact tick format, read by every viewer
// via static/decoder.js (which must mirror the header/decode logic below).
// Emitted with -chunks-v2; -no-json-chunks skips the legacy JSON tick writer.
// This header comment is the format spec.
//
// Layout (all little-endian), then the whole buffer brotli-compressed to
// ticks_chunk_%03d.bin.br (served with Content-Encoding: br):
//
//   magic "CS2V" | u8 version=2 | u8 posStep | u8 playerCount | u8 weaponCount
//   u32 tickStart | u32 tickEnd | u32 snapCount | u32 angCount
//   f32 minX minY minZ maxX maxY maxZ            (quantization frame, per chunk)
//   playerCount × { u64 steamID, u8 side, u8 nameLen, name }
//   weaponCount × { u8 nameLen, name }           (weapon id = table index)
//   snapTicks   u32 × snapCount   (first absolute, then delta from previous)
//   presence    u32 × snapCount   (bit i = player slot i present)
//   per-field SoA streams, snapshot-major, present players only, each value
//   delta-encoded (mod field width) against that player's previous present
//   value - first appearance is absolute. Compression comes from brotli over
//   the near-zero deltas, not from variable-width records (deviation from the
//   original i8-escape design: same ratio, far simpler decoder).
//     posX posY posZ eyeZ  u16 (quantized to chunk AABB)
//     yaw                  u16 (65536ths of 360°)
//     pitch                u16 ((pitch+90)/180, Source pitch positive = down)
//     velX velY velZ       u16 (i16 bits, 1/8 unit/s fixed point, clamp ±4095)
//     health armor         u8
//     money                u16
//     flags                u16 (bit0 alive, 1 hasC4, 2 helmet, 3 defusekit,
//                               4 blinded, 5 side==t - per-record side survives
//                               a halftime swap inside one chunk)
//     weapon               u8  (active weapon, weapon-table index)
//     inventory            u32 (bit = weapon-table index < 32)
//   angTicks    u32 × angCount   (delta; EVERY stored tick - full-rate angles)
//   angPresence u32 × angCount
//   angYaw angPitch  u16 streams (same delta scheme)

import (
	"bytes"
	"encoding/binary"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"sort"

	"github.com/andybalholm/brotli"
)

const (
	v2PosStep    = 2 // position snapshot every 2nd stored tick (~32 Hz)
	v2MaxPlayers = 32
	v2MaxVel     = 4095 // units/s clamp before 1/8 fixed-point

	v2FlagAlive   = 1 << 0
	v2FlagHasC4   = 1 << 1
	v2FlagHelmet  = 1 << 2
	v2FlagDefuse  = 1 << 3
	v2FlagBlinded = 1 << 4
	v2FlagSideT   = 1 << 5
)

type v2Writer struct {
	buf bytes.Buffer
}

func (w *v2Writer) u8(v uint8) { w.buf.WriteByte(v) }
func (w *v2Writer) u16(v uint16) {
	var b [2]byte
	binary.LittleEndian.PutUint16(b[:], v)
	w.buf.Write(b[:])
}
func (w *v2Writer) u32(v uint32) {
	var b [4]byte
	binary.LittleEndian.PutUint32(b[:], v)
	w.buf.Write(b[:])
}
func (w *v2Writer) u64(v uint64) {
	var b [8]byte
	binary.LittleEndian.PutUint64(b[:], v)
	w.buf.Write(b[:])
}
func (w *v2Writer) f32(v float64) {
	var b [4]byte
	binary.LittleEndian.PutUint32(b[:], math.Float32bits(float32(v)))
	w.buf.Write(b[:])
}

func quant16(v, min, max float64) uint16 {
	span := max - min
	if span <= 0 {
		span = 1
	}
	q := math.Round((v - min) / span * 65535)
	if q < 0 {
		q = 0
	} else if q > 65535 {
		q = 65535
	}
	return uint16(q)
}

func quantYaw(yaw float64) uint16 {
	yaw = math.Mod(yaw, 360)
	if yaw < 0 {
		yaw += 360
	}
	return uint16(math.Round(yaw/360*65536)) & 0xffff
}

func quantPitch(pitch float64) uint16 {
	if pitch < -90 {
		pitch = -90
	} else if pitch > 90 {
		pitch = 90
	}
	return uint16(math.Round((pitch + 90) / 180 * 65535))
}

func quantVel(v float64) uint16 {
	if v > v2MaxVel {
		v = v2MaxVel
	} else if v < -v2MaxVel {
		v = -v2MaxVel
	}
	return uint16(int16(math.Round(v * 8)))
}

// writeChunksV2 mirrors writeChunks' round grouping exactly (roundsPerChunk)
// so both indexes describe the same tick ranges.
func writeChunksV2(c *collector, chunksDir string) ([]ChunkEntry, error) {
	if err := os.MkdirAll(chunksDir, 0o755); err != nil {
		return nil, err
	}

	ticks := make([]int, 0, len(c.tickData))
	for t := range c.tickData {
		ticks = append(ticks, t)
	}
	sort.Ints(ticks)

	roundMap := map[int][]int{}
	var roundOrder []int
	for _, t := range ticks {
		rn := c.roundForTick(t)
		if rn == 0 {
			continue
		}
		if _, ok := roundMap[rn]; !ok {
			roundOrder = append(roundOrder, rn)
		}
		roundMap[rn] = append(roundMap[rn], t)
	}
	sort.Ints(roundOrder)

	var entries []ChunkEntry
	chunkIdx := 0
	// Chunk writing is ~30% of a parse, so it reports per chunk rather than
	// sitting at one fraction for seconds (see reportProgress in main.go).
	nChunks := (len(roundOrder) + roundsPerChunk - 1) / roundsPerChunk
	for i := 0; i < len(roundOrder); i += roundsPerChunk {
		end := i + roundsPerChunk
		if end > len(roundOrder) {
			end = len(roundOrder)
		}
		group := roundOrder[i:end]

		var chunkTicks []int
		for _, rn := range group {
			chunkTicks = append(chunkTicks, roundMap[rn]...)
		}
		sort.Ints(chunkTicks)
		if len(chunkTicks) == 0 {
			continue
		}

		filename := fmt.Sprintf("ticks_chunk_%03d.bin.br", chunkIdx)
		if err := writeOneChunkV2(c, chunkTicks, filepath.Join(chunksDir, filename)); err != nil {
			return nil, fmt.Errorf("v2 chunk %d: %w", chunkIdx, err)
		}
		entries = append(entries, ChunkEntry{
			File: filename, Chunk: chunkIdx,
			RoundStart: group[0], RoundEnd: group[len(group)-1],
			TickStart: chunkTicks[0], TickEnd: chunkTicks[len(chunkTicks)-1],
		})
		chunkIdx++
		if nChunks > 0 {
			reportProgress("chunks", float64(chunkIdx)/float64(nChunks))
		}
	}
	return entries, nil
}

func writeOneChunkV2(c *collector, chunkTicks []int, path string) error {
	// Position snapshots = every v2PosStep-th stored tick; angles = every tick.
	var snapTicks []int
	for i := 0; i < len(chunkTicks); i += v2PosStep {
		snapTicks = append(snapTicks, chunkTicks[i])
	}

	// Player table: slot = order of first appearance in this chunk.
	slot := map[string]int{}
	var names []string
	var sides []string
	for _, t := range chunkTicks {
		for _, pt := range c.tickData[t] {
			if _, ok := slot[pt.Name]; !ok && len(names) < v2MaxPlayers {
				slot[pt.Name] = len(names)
				names = append(names, pt.Name)
				sides = append(sides, pt.Side)
			}
		}
	}

	// Weapon table: ids for active weapons + inventory items seen in the chunk.
	weaponID := map[string]int{}
	var weapons []string
	widFor := func(name string) int {
		if name == "" {
			return 0
		}
		if id, ok := weaponID[name]; ok {
			return id
		}
		if len(weapons) >= 255 {
			return 0
		}
		weaponID[name] = len(weapons)
		weapons = append(weapons, name)
		return weaponID[name]
	}
	// reserve id 0 as "none"
	weapons = append(weapons, "-")
	weaponID["-"] = 0

	// Quantization frame: chunk AABB over positions and eye Z.
	minX, minY, minZ := math.Inf(1), math.Inf(1), math.Inf(1)
	maxX, maxY, maxZ := math.Inf(-1), math.Inf(-1), math.Inf(-1)
	for _, t := range chunkTicks {
		for _, pt := range c.tickData[t] {
			minX, maxX = math.Min(minX, pt.X), math.Max(maxX, pt.X)
			minY, maxY = math.Min(minY, pt.Y), math.Max(maxY, pt.Y)
			minZ, maxZ = math.Min(minZ, pt.Z), math.Max(maxZ, pt.Z)
			minZ, maxZ = math.Min(minZ, pt.EyeZ), math.Max(maxZ, pt.EyeZ)
			widFor(pt.ActiveWeapon)
			for _, inv := range pt.Inventory {
				widFor(inv)
			}
		}
	}
	if math.IsInf(minX, 1) { // empty chunk safety
		minX, minY, minZ, maxX, maxY, maxZ = 0, 0, 0, 1, 1, 1
	}

	// Records per snapshot (present players sorted by slot), plus per-player
	// backward-difference velocities in game units/s.
	type rec struct {
		s   int // slot
		pt  PlayerTick
		vel [3]float64
	}
	lastPos := map[int][3]float64{}
	lastTick := map[int]int{}
	snapRecs := make([][]rec, len(snapTicks))
	for si, t := range snapTicks {
		var rs []rec
		for _, pt := range c.tickData[t] {
			sl, ok := slot[pt.Name]
			if !ok {
				continue
			}
			r := rec{s: sl, pt: pt}
			if pt2, ok2 := lastPos[sl]; ok2 {
				dt := float64(t-lastTick[sl]) / 64.0
				if dt > 0 && dt <= 0.5 { // gap → velocity unknown, keep 0
					r.vel = [3]float64{(pt.X - pt2[0]) / dt, (pt.Y - pt2[1]) / dt, (pt.Z - pt2[2]) / dt}
				}
			}
			lastPos[sl] = [3]float64{pt.X, pt.Y, pt.Z}
			lastTick[sl] = t
			rs = append(rs, r)
		}
		sort.Slice(rs, func(a, b int) bool { return rs[a].s < rs[b].s })
		snapRecs[si] = rs
	}

	w := &v2Writer{}
	w.buf.WriteString("CS2V")
	w.u8(2)
	w.u8(v2PosStep)
	w.u8(uint8(len(names)))
	w.u8(uint8(len(weapons)))
	w.u32(uint32(chunkTicks[0]))
	w.u32(uint32(chunkTicks[len(chunkTicks)-1]))
	w.u32(uint32(len(snapTicks)))
	w.u32(uint32(len(chunkTicks)))
	w.f32(minX)
	w.f32(minY)
	w.f32(minZ)
	w.f32(maxX)
	w.f32(maxY)
	w.f32(maxZ)
	for i, n := range names {
		w.u64(c.steamIDs[n])
		side := uint8(2)
		if sides[i] == "ct" {
			side = 0
		} else if sides[i] == "t" {
			side = 1
		}
		w.u8(side)
		nb := []byte(n)
		if len(nb) > 255 {
			nb = nb[:255]
		}
		w.u8(uint8(len(nb)))
		w.buf.Write(nb)
	}
	for _, wn := range weapons {
		nb := []byte(wn)
		if len(nb) > 255 {
			nb = nb[:255]
		}
		w.u8(uint8(len(nb)))
		w.buf.Write(nb)
	}

	// snapTicks (delta) + presence
	prevTick := 0
	for i, t := range snapTicks {
		if i == 0 {
			w.u32(uint32(t))
		} else {
			w.u32(uint32(t - prevTick))
		}
		prevTick = t
	}
	for _, rs := range snapRecs {
		var mask uint32
		for _, r := range rs {
			mask |= 1 << uint(r.s)
		}
		w.u32(mask)
	}

	// Per-field delta streams. emit16/emit8/emit32 hold per-player last values.
	emit16 := func(val func(rec) uint16) {
		last := map[int]uint16{}
		seen := map[int]bool{}
		for _, rs := range snapRecs {
			for _, r := range rs {
				v := val(r)
				if seen[r.s] {
					w.u16(v - last[r.s]) // mod-2^16 delta
				} else {
					w.u16(v)
					seen[r.s] = true
				}
				last[r.s] = v
			}
		}
	}
	emit8 := func(val func(rec) uint8) {
		last := map[int]uint8{}
		seen := map[int]bool{}
		for _, rs := range snapRecs {
			for _, r := range rs {
				v := val(r)
				if seen[r.s] {
					w.u8(v - last[r.s])
				} else {
					w.u8(v)
					seen[r.s] = true
				}
				last[r.s] = v
			}
		}
	}
	emit32 := func(val func(rec) uint32) {
		last := map[int]uint32{}
		seen := map[int]bool{}
		for _, rs := range snapRecs {
			for _, r := range rs {
				v := val(r)
				if seen[r.s] {
					w.u32(v - last[r.s])
				} else {
					w.u32(v)
					seen[r.s] = true
				}
				last[r.s] = v
			}
		}
	}

	emit16(func(r rec) uint16 { return quant16(r.pt.X, minX, maxX) })
	emit16(func(r rec) uint16 { return quant16(r.pt.Y, minY, maxY) })
	emit16(func(r rec) uint16 { return quant16(r.pt.Z, minZ, maxZ) })
	emit16(func(r rec) uint16 { return quant16(r.pt.EyeZ, minZ, maxZ) })
	emit16(func(r rec) uint16 { return quantYaw(r.pt.Yaw) })
	emit16(func(r rec) uint16 { return quantPitch(r.pt.Pitch) })
	emit16(func(r rec) uint16 { return quantVel(r.vel[0]) })
	emit16(func(r rec) uint16 { return quantVel(r.vel[1]) })
	emit16(func(r rec) uint16 { return quantVel(r.vel[2]) })
	emit8(func(r rec) uint8 {
		h := r.pt.Health
		if h < 0 {
			h = 0
		} else if h > 255 {
			h = 255
		}
		return uint8(h)
	})
	emit8(func(r rec) uint8 {
		a := r.pt.Armor
		if a < 0 {
			a = 0
		} else if a > 255 {
			a = 255
		}
		return uint8(a)
	})
	emit16(func(r rec) uint16 {
		m := r.pt.Money
		if m < 0 {
			m = 0
		} else if m > 65535 {
			m = 65535
		}
		return uint16(m)
	})
	emit16(func(r rec) uint16 {
		var f uint16
		if r.pt.Health > 0 {
			f |= v2FlagAlive
		}
		if r.pt.HasC4 {
			f |= v2FlagHasC4
		}
		if r.pt.Helmet {
			f |= v2FlagHelmet
		}
		if r.pt.DefuseKit {
			f |= v2FlagDefuse
		}
		if r.pt.FlashDuration > 0 {
			f |= v2FlagBlinded
		}
		if r.pt.Side == "t" {
			f |= v2FlagSideT
		}
		return f
	})
	emit8(func(r rec) uint8 { return uint8(widFor(r.pt.ActiveWeapon)) })
	emit32(func(r rec) uint32 {
		var bits uint32
		for _, inv := range r.pt.Inventory {
			if id := widFor(inv); id > 0 && id < 32 {
				bits |= 1 << uint(id)
			}
		}
		return bits
	})

	// Full-rate angle stream: every stored tick.
	prevTick = 0
	for i, t := range chunkTicks {
		if i == 0 {
			w.u32(uint32(t))
		} else {
			w.u32(uint32(t - prevTick))
		}
		prevTick = t
	}
	angRecs := make([][]rec, len(chunkTicks))
	for ti, t := range chunkTicks {
		var rs []rec
		for _, pt := range c.tickData[t] {
			if sl, ok := slot[pt.Name]; ok {
				rs = append(rs, rec{s: sl, pt: pt})
			}
		}
		sort.Slice(rs, func(a, b int) bool { return rs[a].s < rs[b].s })
		angRecs[ti] = rs
	}
	for _, rs := range angRecs {
		var mask uint32
		for _, r := range rs {
			mask |= 1 << uint(r.s)
		}
		w.u32(mask)
	}
	emitAng := func(val func(rec) uint16) {
		last := map[int]uint16{}
		seen := map[int]bool{}
		for _, rs := range angRecs {
			for _, r := range rs {
				v := val(r)
				if seen[r.s] {
					w.u16(v - last[r.s])
				} else {
					w.u16(v)
					seen[r.s] = true
				}
				last[r.s] = v
			}
		}
	}
	emitAng(func(r rec) uint16 { return quantYaw(r.pt.Yaw) })
	emitAng(func(r rec) uint16 { return quantPitch(r.pt.Pitch) })

	f, err := os.Create(path)
	if err != nil {
		return err
	}
	defer f.Close()
	bw := brotli.NewWriterLevel(f, 9)
	if _, err := bw.Write(w.buf.Bytes()); err != nil {
		return err
	}
	return bw.Close()
}
