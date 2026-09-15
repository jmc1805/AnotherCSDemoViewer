// Overwatch add-on extractor - 2nd-pass demo analysis data for the anti-cheat
// ("Overwatch") tab. Fully independent of cmd/parser: reads the .dem itself and
// writes a single raw-features JSON. Deleting this binary leaves the main
// pipeline untouched.
//
// Usage: overwatch -demo <path.dem> -out <overwatch_raw.json>
//
// Output (consumed by analysis/ in Python):
//   - players: steamid ↔ name
//   - kills: enriched (penetration, smoke, blind, noscope, distance)
//   - damage: with hitgroup + attacker speed (first-bullet HS, hits-moving)
//   - windows: per-kill context windows, 224 frames before / 32 after
//     (AntiCheatPT-style), per-frame attacker+victim state incl. pitch & Z
//   - tot_episodes: Time-on-Target episode lengths per player
//   - occlusion: crosshair-on-unspotted-enemy aggregates + streak moments
//   - first_sights: crosshair divergence when an enemy first becomes spotted
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"math"
	"os"
	"sort"
	"strconv"

	dem "github.com/markus-wa/demoinfocs-golang/v5/pkg/demoinfocs"
	"github.com/markus-wa/demoinfocs-golang/v5/pkg/demoinfocs/common"
	"github.com/markus-wa/demoinfocs-golang/v5/pkg/demoinfocs/events"
)

const (
	windowBefore = 224 // frames of context before each kill
	windowAfter  = 32  // frames after
	historyLen   = windowBefore + 8

	totMaxAngleDeg  = 5.0    // crosshair within this of a spotted enemy = "on target"
	totMinEpisode   = 4      // ignore episodes shorter than this (frames)
	occMaxAngleDeg  = 4.0    // crosshair within this of an UNSPOTTED enemy
	occMaxDistUnits = 2500.0 // ignore occluded enemies farther than this
	occStreakMin    = 32     // frames (~0.5 s) of continuous occluded tracking = a "moment"

	maxEpisodesPerPlayer = 4000
	maxFirstSights       = 30000
	maxOccStreaks        = 2000
)

// Window row column order. Keep analysis/features.py WINDOW_COLS in sync.
var windowColumns = []string{
	"rel_tick",       // 0  frame offset from the kill (-224..32)
	"a_yaw",          // 1  attacker view yaw (deg)
	"a_pitch",        // 2  attacker view pitch (deg)
	"a_x", "a_y", "a_z", // 3,4,5
	"a_speed",        // 6  attacker horizontal speed (units/s)
	"v_x", "v_y", "v_z", // 7,8,9
	"ang_to_victim",  // 10 angle crosshair ↔ attacker-eye→victim-eye (deg)
	"v_spotted",      // 11 victim spotted by attacker's team (0/1)
	"a_fired",        // 12 attacker fired this frame (0/1)
	"a_health",       // 13
	"v_health",       // 14
	"a_flash",        // 15 attacker flash duration remaining (s)
	"a_duck",         // 16 attacker ducking (0/1)
}

type OWPlayer struct {
	SteamID string `json:"steam_id"`
	Name    string `json:"name"`
}

type OWKill struct {
	Tick         int     `json:"tick"`
	Round        int     `json:"round"`
	AttackerID   string  `json:"attacker_id"`
	VictimID     string  `json:"victim_id"`
	Weapon       string  `json:"weapon"`
	Headshot     bool    `json:"headshot"`
	Penetrated   int     `json:"penetrated"`
	ThroughSmoke bool    `json:"through_smoke"`
	AttackerBlind bool   `json:"attacker_blind"`
	NoScope      bool    `json:"noscope"`
	Distance     float64 `json:"distance"`
}

type OWDamage struct {
	Tick          int     `json:"tick"`
	Round         int     `json:"round"`
	AttackerID    string  `json:"attacker_id"`
	VictimID      string  `json:"victim_id"`
	Weapon        string  `json:"weapon"`
	HitGroup      int     `json:"hitgroup"` // 1 = head
	Dmg           int     `json:"dmg"`
	AttackerSpeed float64 `json:"attacker_speed"` // horizontal units/s at hit
}

type OWWindow struct {
	Kill      int         `json:"kill"` // index into kills
	StartTick int         `json:"start_tick"`
	Rows      [][]float64 `json:"rows"`
}

type OWOcclusion struct {
	TrackFrames int            `json:"track_frames"` // crosshair-near-occluded-enemy frames
	TotalFrames int            `json:"total_frames"` // (player, occluded enemy in range) frame pairs
	Streaks     []OWOccStreak  `json:"streaks"`
}

type OWOccStreak struct {
	EnemyID   string  `json:"enemy_id"`
	StartTick int     `json:"start_tick"`
	EndTick   int     `json:"end_tick"`
	MeanAngle float64 `json:"mean_angle"`
}

type OWFirstSight struct {
	Tick       int     `json:"tick"`
	ObserverID string  `json:"observer_id"`
	EnemyID    string  `json:"enemy_id"`
	Divergence float64 `json:"divergence"` // crosshair ↔ enemy angle at first spot (deg)
	Distance   float64 `json:"distance"`   // units
}

type Output struct {
	Tickrate      float64                 `json:"tickrate"`
	WindowColumns []string                `json:"window_columns"`
	Players       []OWPlayer              `json:"players"`
	Kills         []OWKill                `json:"kills"`
	Damage        []OWDamage              `json:"damage"`
	Windows       []OWWindow              `json:"windows"`
	TotEpisodes   map[string][]int        `json:"tot_episodes"`
	Occlusion     map[string]*OWOcclusion `json:"occlusion"`
	FirstSights   []OWFirstSight          `json:"first_sights"`
}

// ── Per-frame state ───────────────────────────────────────────────────────────

type pstate struct {
	x, y, z    float64
	yaw, pitch float64
	speed      float64
	health     int
	flash      float64
	duck       bool
	alive      bool
	team       common.Team
	// view direction unit vector (from yaw/pitch)
	vx, vy, vz float64
	// eye position
	ex, ey, ez float64
}

type frameSnap struct {
	tick    int
	states  map[uint64]*pstate
	pairAng map[[2]uint64]float64 // [observer][enemy] crosshair↔enemy angle (deg)
	spotted map[[2]uint64]bool    // [observer][enemy] enemy spotted by observer's TEAM
}

type pendingWindow struct {
	killIdx    int
	attacker   uint64
	victim     uint64
	killTick   int
	remaining  int
	rows       [][]float64
}

type extractor struct {
	p dem.Parser

	names map[uint64]string

	kills   []OWKill
	damage  []OWDamage
	windows []OWWindow
	pending []*pendingWindow

	history []frameSnap

	roundLive bool

	totStreak   map[[2]uint64]int
	totEpisodes map[uint64][]int

	occStreak map[[2]uint64]*occStreakState
	occTrack  map[uint64]int
	occTotal  map[uint64]int
	occMoments map[uint64][]OWOccStreak

	prevSpottedByObs map[[2]uint64]bool
	firstSights      []OWFirstSight

	shotThisFrame map[uint64]bool
	pendingDmg    []pendingDmgRef
}

type pendingDmgRef struct {
	idx      int
	attacker uint64
}

type occStreakState struct {
	startTick int
	sumAngle  float64
	frames    int
}

func newExtractor(p dem.Parser) *extractor {
	return &extractor{
		p:                p,
		names:            map[uint64]string{},
		totStreak:        map[[2]uint64]int{},
		totEpisodes:      map[uint64][]int{},
		occStreak:        map[[2]uint64]*occStreakState{},
		occTrack:         map[uint64]int{},
		occTotal:         map[uint64]int{},
		occMoments:       map[uint64][]OWOccStreak{},
		prevSpottedByObs: map[[2]uint64]bool{},
		shotThisFrame:    map[uint64]bool{},
	}
}

// ── Helpers ───────────────────────────────────────────────────────────────────

func sid(p *common.Player) uint64 {
	if p == nil {
		return 0
	}
	return p.SteamID64
}

func sidStr(id uint64) string { return strconv.FormatUint(id, 10) }

func weaponName(e *common.Equipment) string {
	if e == nil {
		return ""
	}
	return e.String()
}

// Source convention: yaw CCW around Z, pitch positive looking down.
func viewVector(yawDeg, pitchDeg float64) (x, y, z float64) {
	yaw := yawDeg * math.Pi / 180
	pitch := pitchDeg * math.Pi / 180
	cp := math.Cos(pitch)
	return cp * math.Cos(yaw), cp * math.Sin(yaw), -math.Sin(pitch)
}

// Angle in degrees between a view direction and the vector from eye to target.
func angleTo(s *pstate, tx, ty, tz float64) float64 {
	dx, dy, dz := tx-s.ex, ty-s.ey, tz-s.ez
	n := math.Sqrt(dx*dx + dy*dy + dz*dz)
	if n == 0 {
		return 0
	}
	dot := (s.vx*dx + s.vy*dy + s.vz*dz) / n
	if dot > 1 {
		dot = 1
	} else if dot < -1 {
		dot = -1
	}
	return math.Acos(dot) * 180 / math.Pi
}

func dist2D(a, b *pstate) float64 {
	dx, dy := a.x-b.x, a.y-b.y
	return math.Sqrt(dx*dx + dy*dy)
}

// Horizontal speed (units/s) from the position delta vs the previous frame -
// this demoinfocs version exposes no Player.Velocity().
func (c *extractor) speedOf(pl *common.Player) float64 {
	if pl == nil || len(c.history) == 0 {
		return 0
	}
	last := &c.history[len(c.history)-1]
	prev, ok := last.states[pl.SteamID64]
	if !ok {
		return 0
	}
	dt := c.p.CurrentFrame() - last.tick
	if dt <= 0 {
		return 0
	}
	tickrate := c.p.TickRate()
	if tickrate <= 0 {
		tickrate = 64
	}
	pos := pl.Position()
	return math.Hypot(pos.X-prev.x, pos.Y-prev.y) * tickrate / float64(dt)
}

func r2(v float64) float64 {
	if math.IsNaN(v) || math.IsInf(v, 0) {
		return 0
	}
	return math.Round(v*100) / 100
}

// ── Event handlers ────────────────────────────────────────────────────────────

func (c *extractor) register() {
	p := c.p

	// The match starts HERE, which is not always the first round: FACEIT plays
	// a knife round (game phase Pregame, so neither the warmup flag nor
	// IsMatchStarted excludes it) before the real match, and its knife duels
	// would otherwise be scored as ordinary kills by every aim feature.
	//
	// Same rule as cmd/parser's trimPreMatchRounds - the last MatchStart fired
	// at 0-0 - so the extractor and the match JSON agree on which kills are in
	// the match. Only a MatchStart at 0-0 counts, or a mid-match restart would
	// throw away real rounds.
	//
	// Everything collected so far is dropped by rebuilding the accumulators,
	// rather than filtered afterwards: OWWindow.Kill is an INDEX into kills,
	// so a post-hoc filter would have to remap every cross-reference.
	p.RegisterEventHandler(func(e events.MatchStart) {
		gs := p.GameState()
		if gs.TeamCounterTerrorists().Score() != 0 || gs.TeamTerrorists().Score() != 0 {
			return
		}
		// Names are kept: the roster is legitimately learned before the match.
		names := c.names
		*c = *newExtractor(c.p)
		c.names = names
	})

	p.RegisterEventHandler(func(e events.RoundStart) {
		// New round: aim streaks across the round boundary are meaningless.
		c.roundLive = false
		c.flushStreaks()
	})
	p.RegisterEventHandler(func(e events.RoundFreezetimeEnd) {
		c.roundLive = true
	})
	p.RegisterEventHandler(func(e events.RoundEnd) {
		c.roundLive = false
		c.flushStreaks()
	})

	p.RegisterEventHandler(func(e events.WeaponFire) {
		if e.Shooter != nil {
			c.shotThisFrame[sid(e.Shooter)] = true
		}
	})

	p.RegisterEventHandler(func(e events.PlayerHurt) {
		if e.Attacker == nil || e.Player == nil || sid(e.Attacker) == 0 {
			return
		}
		// AttackerSpeed is resolved at FrameDone - entity positions for the
		// current frame are not final at event-dispatch time.
		c.damage = append(c.damage, OWDamage{
			Tick:          p.CurrentFrame(),
			Round:         p.GameState().TotalRoundsPlayed() + 1,
			AttackerID:    sidStr(sid(e.Attacker)),
			VictimID:      sidStr(sid(e.Player)),
			Weapon:        weaponName(e.Weapon),
			HitGroup:      int(e.HitGroup),
			Dmg:           e.HealthDamage,
			AttackerSpeed: -1,
		})
		c.pendingDmg = append(c.pendingDmg, pendingDmgRef{
			idx: len(c.damage) - 1, attacker: sid(e.Attacker),
		})
	})

	p.RegisterEventHandler(func(e events.Kill) {
		if e.Killer == nil || e.Victim == nil || sid(e.Killer) == 0 ||
			e.Killer.Team == e.Victim.Team {
			return
		}
		tick := p.CurrentFrame()
		killIdx := len(c.kills)
		c.kills = append(c.kills, OWKill{
			Tick:          tick,
			Round:         p.GameState().TotalRoundsPlayed() + 1,
			AttackerID:    sidStr(sid(e.Killer)),
			VictimID:      sidStr(sid(e.Victim)),
			Weapon:        weaponName(e.Weapon),
			Headshot:      e.IsHeadshot,
			Penetrated:    e.PenetratedObjects,
			ThroughSmoke:  e.ThroughSmoke,
			AttackerBlind: e.AttackerBlind,
			NoScope:       e.NoScope,
			Distance:      r2(float64(e.Distance)),
		})

		// Build the context window: history covers the 224 frames before the
		// kill; the 32 after are appended as future frames arrive.
		w := &pendingWindow{
			killIdx:   killIdx,
			attacker:  sid(e.Killer),
			victim:    sid(e.Victim),
			killTick:  tick,
			remaining: windowAfter,
		}
		for i := range c.history {
			f := &c.history[i]
			if f.tick < tick-windowBefore || f.tick > tick {
				continue
			}
			if row := c.windowRow(f, w); row != nil {
				w.rows = append(w.rows, row)
			}
		}
		c.pending = append(c.pending, w)
	})

	p.RegisterEventHandler(func(e events.FrameDone) {
		gs := p.GameState()
		if gs.IsWarmupPeriod() || !gs.IsMatchStarted() {
			c.shotThisFrame = map[uint64]bool{}
			return
		}
		c.onFrame()
	})
}

// ── Per-frame work ────────────────────────────────────────────────────────────

func (c *extractor) onFrame() {
	p := c.p
	gs := p.GameState()
	tick := p.CurrentFrame()

	players := gs.Participants().Playing()
	snap := frameSnap{
		tick:    tick,
		states:  make(map[uint64]*pstate, len(players)),
		pairAng: map[[2]uint64]float64{},
		spotted: map[[2]uint64]bool{},
	}

	for _, pl := range players {
		if pl == nil || pl.SteamID64 == 0 {
			continue
		}
		id := pl.SteamID64
		c.names[id] = pl.Name
		pos := pl.Position()
		eye, _ := pl.PositionEyes()
		yaw := float64(pl.ViewDirectionX())
		pitch := float64(pl.ViewDirectionY())
		vx, vy, vz := viewVector(yaw, pitch)
		fl := 0.0
		if d := pl.FlashDurationTime(); d > 0 {
			fl = d.Seconds()
		}
		snap.states[id] = &pstate{
			x: pos.X, y: pos.Y, z: pos.Z,
			yaw: yaw, pitch: pitch,
			speed:  c.speedOf(pl),
			health: pl.Health(), flash: fl,
			duck: pl.IsDucking(), alive: pl.IsAlive(), team: pl.Team,
			vx: vx, vy: vy, vz: vz,
			ex: eye.X, ey: eye.Y, ez: eye.Z,
		}
	}

	// Pair geometry + spotted state, plus ToT / occlusion / first-sight
	// bookkeeping. Observer→enemy over opposing, alive pairs.
	for _, obs := range players {
		oid := sid(obs)
		os_, ok := snap.states[oid]
		if !ok || !os_.alive {
			continue
		}
		for _, en := range players {
			eid := sid(en)
			es, ok := snap.states[eid]
			if !ok || eid == oid || es.team == os_.team || !es.alive {
				continue
			}
			key := [2]uint64{oid, eid}
			ang := angleTo(os_, es.ex, es.ey, es.ez)
			snap.pairAng[key] = ang

			spottedByObs := en.IsSpottedBy(obs)
			spottedByTeam := spottedByObs
			if !spottedByTeam {
				for _, tm := range players {
					if tm != nil && sid(tm) != oid && tm.Team == obs.Team &&
						tm.IsAlive() && en.IsSpottedBy(tm) {
						spottedByTeam = true
						break
					}
				}
			}
			snap.spotted[key] = spottedByTeam

			if !c.roundLive {
				continue
			}

			// Time-on-Target: crosshair near an enemy the observer has spotted.
			if spottedByObs && ang <= totMaxAngleDeg {
				c.totStreak[key]++
			} else if s := c.totStreak[key]; s > 0 {
				if s >= totMinEpisode && len(c.totEpisodes[oid]) < maxEpisodesPerPlayer {
					c.totEpisodes[oid] = append(c.totEpisodes[oid], s)
				}
				c.totStreak[key] = 0
			}

			// Occlusion proxy: enemy not spotted by ANYONE on the observer's
			// team, in range - how often does the crosshair sit on them anyway?
			if !spottedByTeam && dist2D(os_, es) <= occMaxDistUnits {
				c.occTotal[oid]++
				if ang <= occMaxAngleDeg {
					c.occTrack[oid]++
					st := c.occStreak[key]
					if st == nil {
						st = &occStreakState{startTick: tick}
						c.occStreak[key] = st
					}
					st.frames++
					st.sumAngle += ang
				} else {
					c.endOccStreak(key, tick)
				}
			} else {
				c.endOccStreak(key, tick)
			}

			// First sight: enemy transitions to spotted-by-observer.
			if spottedByObs && !c.prevSpottedByObs[key] {
				if len(c.firstSights) < maxFirstSights {
					c.firstSights = append(c.firstSights, OWFirstSight{
						Tick: tick, ObserverID: sidStr(oid), EnemyID: sidStr(eid),
						Divergence: r2(ang),
						Distance:   r2(dist2D(os_, es)),
					})
				}
			}
			c.prevSpottedByObs[key] = spottedByObs
		}
	}

	// Window upkeep: append this frame to any window awaiting post-kill frames.
	live := c.pending[:0]
	for _, w := range c.pending {
		if row := c.windowRow(&snap, w); row != nil {
			w.rows = append(w.rows, row)
		}
		w.remaining--
		if w.remaining <= 0 {
			c.windows = append(c.windows, OWWindow{
				Kill: w.killIdx, StartTick: w.killTick - windowBefore, Rows: w.rows,
			})
		} else {
			live = append(live, w)
		}
	}
	c.pending = live

	// Resolve attacker speed for this frame's damage events from the now-final
	// frame positions.
	for _, pd := range c.pendingDmg {
		if s, ok := snap.states[pd.attacker]; ok {
			c.damage[pd.idx].AttackerSpeed = r2(s.speed)
		} else {
			c.damage[pd.idx].AttackerSpeed = 0
		}
	}
	c.pendingDmg = c.pendingDmg[:0]

	c.history = append(c.history, snap)
	if len(c.history) > historyLen {
		c.history = c.history[1:]
	}
	c.shotThisFrame = map[uint64]bool{}
}

func (c *extractor) windowRow(f *frameSnap, w *pendingWindow) []float64 {
	a, ok := f.states[w.attacker]
	if !ok {
		return nil
	}
	row := make([]float64, len(windowColumns))
	row[0] = float64(f.tick - w.killTick)
	row[1], row[2] = r2(a.yaw), r2(a.pitch)
	row[3], row[4], row[5] = r2(a.x), r2(a.y), r2(a.z)
	row[6] = r2(a.speed)
	row[13] = float64(a.health)
	row[15] = r2(a.flash)
	if a.duck {
		row[16] = 1
	}
	if v, ok := f.states[w.victim]; ok {
		row[7], row[8], row[9] = r2(v.x), r2(v.y), r2(v.z)
		row[14] = float64(v.health)
		key := [2]uint64{w.attacker, w.victim}
		if ang, ok := f.pairAng[key]; ok {
			row[10] = r2(ang)
		} else if v.alive {
			row[10] = r2(angleTo(a, v.ex, v.ey, v.ez))
		} else {
			row[10] = -1 // victim dead (post-kill frames)
		}
		if f.spotted[key] {
			row[11] = 1
		}
	} else {
		row[10] = -1
	}
	if c.shotFrame(f, w.attacker) {
		row[12] = 1
	}
	return row
}

// a_fired is only known for the live frame (events are consumed per frame);
// when building rows from history we can't recover it, so it's 0 there. The
// post-kill frames and the kill frame itself - the ones that matter for
// settle/spray analysis - are always appended live.
func (c *extractor) shotFrame(f *frameSnap, player uint64) bool {
	return f.tick == c.p.CurrentFrame() && c.shotThisFrame[player]
}

func (c *extractor) endOccStreak(key [2]uint64, tick int) {
	st := c.occStreak[key]
	if st == nil {
		return
	}
	delete(c.occStreak, key)
	if st.frames >= occStreakMin {
		oid := key[0]
		total := 0
		for _, m := range c.occMoments {
			total += len(m)
		}
		if total < maxOccStreaks {
			c.occMoments[oid] = append(c.occMoments[oid], OWOccStreak{
				EnemyID:   sidStr(key[1]),
				StartTick: st.startTick,
				EndTick:   st.startTick + st.frames,
				MeanAngle: r2(st.sumAngle / float64(st.frames)),
			})
		}
	}
}

func (c *extractor) flushStreaks() {
	tick := c.p.CurrentFrame()
	for key, s := range c.totStreak {
		if s >= totMinEpisode && len(c.totEpisodes[key[0]]) < maxEpisodesPerPlayer {
			c.totEpisodes[key[0]] = append(c.totEpisodes[key[0]], s)
		}
		delete(c.totStreak, key)
	}
	for key := range c.occStreak {
		c.endOccStreak(key, tick)
	}
	c.prevSpottedByObs = map[[2]uint64]bool{}
}

// ── main ──────────────────────────────────────────────────────────────────────

func main() {
	demoPath := flag.String("demo", "", "path to the .dem file")
	outPath := flag.String("out", "", "output overwatch_raw JSON path")
	flag.Parse()
	if *demoPath == "" || *outPath == "" {
		flag.Usage()
		os.Exit(1)
	}

	f, err := os.Open(*demoPath)
	if err != nil {
		log.Fatalf("open demo: %v", err)
	}
	defer f.Close()

	p := dem.NewParser(f)
	c := newExtractor(p)
	c.register()

	if err := p.ParseToEnd(); err != nil {
		log.Printf("parse warning (non-fatal): %v", err)
	}

	// Flush windows still waiting on post-kill frames (demo ended).
	for _, w := range c.pending {
		c.windows = append(c.windows, OWWindow{
			Kill: w.killIdx, StartTick: w.killTick - windowBefore, Rows: w.rows,
		})
	}
	c.flushStreaks()

	players := make([]OWPlayer, 0, len(c.names))
	for id, name := range c.names {
		players = append(players, OWPlayer{SteamID: sidStr(id), Name: name})
	}
	sort.Slice(players, func(i, j int) bool { return players[i].SteamID < players[j].SteamID })

	occ := map[string]*OWOcclusion{}
	for id := range c.names {
		o := &OWOcclusion{
			TrackFrames: c.occTrack[id],
			TotalFrames: c.occTotal[id],
			Streaks:     c.occMoments[id],
		}
		if o.Streaks == nil {
			o.Streaks = []OWOccStreak{}
		}
		occ[sidStr(id)] = o
	}
	tot := map[string][]int{}
	for id, eps := range c.totEpisodes {
		tot[sidStr(id)] = eps
	}

	out := Output{
		Tickrate:      p.TickRate(),
		WindowColumns: windowColumns,
		Players:       players,
		Kills:         c.kills,
		Damage:        c.damage,
		Windows:       c.windows,
		TotEpisodes:   tot,
		Occlusion:     occ,
		FirstSights:   c.firstSights,
	}
	if out.Kills == nil {
		out.Kills = []OWKill{}
	}
	if out.Damage == nil {
		out.Damage = []OWDamage{}
	}
	if out.Windows == nil {
		out.Windows = []OWWindow{}
	}
	if out.FirstSights == nil {
		out.FirstSights = []OWFirstSight{}
	}

	of, err := os.Create(*outPath)
	if err != nil {
		log.Fatalf("create output: %v", err)
	}
	defer of.Close()
	enc := json.NewEncoder(of)
	if err := enc.Encode(out); err != nil {
		log.Fatalf("write output: %v", err)
	}
	fmt.Printf("overwatch raw: %d players, %d kills, %d windows, %d first-sights\n",
		len(players), len(c.kills), len(c.windows), len(c.firstSights))
}
