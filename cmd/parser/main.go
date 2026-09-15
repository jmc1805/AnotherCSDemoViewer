// CS2 demo parser - outputs the exact JSON schema that viewer.js + match.html expect.
// Usage: parser -demo <path.dem> -out <master.json> -chunks <chunks_dir>
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"math"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	dem "github.com/markus-wa/demoinfocs-golang/v5/pkg/demoinfocs"
	"github.com/markus-wa/demoinfocs-golang/v5/pkg/demoinfocs/common"
	"github.com/markus-wa/demoinfocs-golang/v5/pkg/demoinfocs/events"
)

// ── Constants ─────────────────────────────────────────────────────────────────

const (
	roundsPerChunk = 3
	tickStep       = 1
	smokeDuration  = 1152 // 18 s at 64 tick/s (CS wiki: smoke lifetime post-deploy)
	fireDuration   = 450  // 7.03125 s at 64 tick/s (CS wiki: molotov/incendiary burn time)
)

// ── Output JSON types ─────────────────────────────────────────────────────────

type Round struct {
	RoundNum  int      `json:"round_num"`
	Start     int      `json:"start"`
	End       *int     `json:"end"`
	FreezeEnd *int     `json:"freeze_end"`
	Winner    string   `json:"winner"`
	Reason    string   `json:"reason"`
	BombPlant *int     `json:"bomb_plant"`
	BombSite  string   `json:"bomb_site"`
	Economy   *Economy `json:"economy,omitempty"`
}

// Economy - team totals captured on the first frame after RoundFreezetimeEnd (the standard "buy
// snapshot" moment: after the buy phase, before anyone's spent a bullet).
// Pointer + omitempty so matches parsed before this field existed just lack
// the key; the frontend guards with `r.economy != null` (repo convention for
// additive fields).
type Economy struct {
	CTEquip int `json:"ct_equip"`
	CTCash  int `json:"ct_cash"`
	CTSpent int `json:"ct_spent"`
	TEquip  int `json:"t_equip"`
	TCash   int `json:"t_cash"`
	TSpent  int `json:"t_spent"`
}

type Kill struct {
	Tick          int    `json:"tick"`
	RoundNum      int    `json:"round_num"`
	AttackerName  string `json:"attacker_name"`
	AttackerSide  string `json:"attacker_side"`
	VictimName    string `json:"victim_name"`
	VictimSide    string `json:"victim_side"`
	AssisterName  string `json:"assister_name,omitempty"`
	AssistFlash   bool   `json:"assist_flash,omitempty"`
	Weapon        string `json:"weapon"`
	Headshot      bool   `json:"headshot"`
	Wallbang      bool   `json:"wallbang,omitempty"`
	ThroughSmoke  bool   `json:"through_smoke,omitempty"`
	AttackerBlind bool   `json:"attacker_blind,omitempty"`
	NoScope       bool   `json:"noscope,omitempty"`
	AttackerAir   bool   `json:"attacker_air,omitempty"`
	Suicide       bool   `json:"suicide,omitempty"`

	// Where the duel happened, in world units - the attacker's position and
	// the victim's, both read at the kill event. Additive (absent on matches
	// parsed before 2026-08-19): analysis/moments.py gates on victim_x via the
	// index's `kill_coords` cap, and the analyser's result heatmap plots a
	// kill at the attacker's spot and a death at the victim's. No omitempty:
	// 0 is a legal world coordinate, so "absent" and "zero" have to stay
	// distinguishable.
	AttackerX float64 `json:"attacker_x"`
	AttackerY float64 `json:"attacker_y"`
	AttackerZ float64 `json:"attacker_z"`
	VictimX   float64 `json:"victim_x"`
	VictimY   float64 `json:"victim_y"`
	VictimZ   float64 `json:"victim_z"`
}

type Damage struct {
	Tick         int    `json:"tick"`
	RoundNum     int    `json:"round_num"`
	AttackerName string `json:"attacker_name"`
	AttackerSide string `json:"attacker_side"`
	VictimName   string `json:"victim_name"`
	VictimSide   string `json:"victim_side"`
	DmgHealth    int    `json:"dmg_health"`
	Weapon       string `json:"weapon"`
}

type Blind struct {
	Tick          int     `json:"tick"`
	AttackerName  string  `json:"attacker_name"`
	AttackerSide  string  `json:"attacker_side"`
	VictimName    string  `json:"victim_name"`
	VictimSide    string  `json:"victim_side"`
	BlindDuration float64 `json:"blind_duration"`
}

type Shot struct {
	Tick       int    `json:"tick"`
	PlayerName string `json:"player_name"`
	PlayerSide string `json:"player_side"`
	Weapon     string `json:"weapon"`
	// Ground-truth aim at the moment of fire (plan §1.5): the first-person
	// camera snaps to these regardless of snapshot sampling.
	Yaw   float64 `json:"yaw"`
	Pitch float64 `json:"pitch"`
	EyeX  float64 `json:"eye_x"`
	EyeY  float64 `json:"eye_y"`
	EyeZ  float64 `json:"eye_z"`
}

type SmokeEvent struct {
	StartTick   int     `json:"start_tick"`
	EndTick     int     `json:"end_tick"`
	X           float64 `json:"X"`
	Y           float64 `json:"Y"`
	Z           float64 `json:"Z"`
	ThrowerName string  `json:"thrower_name"`
	// Additive, infernos only (2026-07-18): "Molotov" | "Incendiary", and the
	// fire-cell ignition timeline sampled from Inferno.Fires() in FrameDone -
	// lets the 2D viewer draw the true spreading footprint. Empty on smokes
	// and on matches parsed before these fields existed (frontend falls back
	// to trajectory correlation + radial approximation, see utilfx.logic.js).
	Weapon string     `json:"weapon,omitempty"`
	Cells  []FireCell `json:"cells,omitempty"`
}

// FireCell is one fire spot's first-seen (ignition) tick and position.
// Cells only ever accumulate while an inferno burns, so "every cell with
// tick <= now" reconstructs the footprint at any playback time.
type FireCell struct {
	Tick int     `json:"tick"`
	X    float64 `json:"X"`
	Y    float64 `json:"Y"`
}

type GrenadeDetonate struct {
	Tick        int     `json:"tick"`
	X           float64 `json:"X"`
	Y           float64 `json:"Y"`
	Z           float64 `json:"Z"`
	ThrowerName string  `json:"thrower_name"`
}

type GrenadeTrajectory struct {
	Tick        int     `json:"tick"`
	GrenadeType string  `json:"grenade_type"`
	X           float64 `json:"X"`
	Y           float64 `json:"Y"`
	Z           float64 `json:"Z"`
	Thrower     string  `json:"thrower"`
	EntityID    int     `json:"entity_id"`
}

// BombEvent is one entry on the round's single bomb timeline. Event is one of
// the "where is the bomb" states (plant, defuse, pickup, drop, detonate) or one
// of the in-progress actions (plant_begin, plant_abort, defuse_start,
// defuse_abort) added 2026-09-10 so the 2D viewer can show a plant/defuse
// actually happening instead of only its outcome.
//
// HasKit is a pointer with omitempty on purpose: it is meaningful ONLY on
// defuse_start (5 s with a kit, 10 s without - the whole reason the frontend
// can draw a truthful countdown), and "false" and "this parse predates the
// field" must stay distinguishable. Same absent-is-not-zero rule the repo
// applies to numerics.
type BombEvent struct {
	Tick     int     `json:"tick"`
	Event    string  `json:"event"`
	X        float64 `json:"X"`
	Y        float64 `json:"Y"`
	Name     string  `json:"name"`
	Bombsite *string `json:"bombsite"`
	HasKit   *bool   `json:"has_kit,omitempty"`
}

// DroppedItem tracks a weapon/utility item dropped on the ground. ItemID
// (the equipment's UniqueID2) ties a "drop" to the later "pickup" of the
// same physical item, so the frontend can hide markers for anything that's
// been picked back up rather than trusting round-relative bomb-style logic.
type DroppedItem struct {
	Tick   int     `json:"tick"`
	Event  string  `json:"event"` // "drop" | "pickup"
	X      float64 `json:"X"`
	Y      float64 `json:"Y"`
	Z      float64 `json:"Z"`
	Weapon string  `json:"weapon"`
	Name   string  `json:"name"`
	ItemID string  `json:"item_id"`
}

// One defuse kit lying on the ground - see collector.groundKits.
type groundKit struct {
	x, y, z float64
}

// How close a player has to be to a tracked kit for gaining one to count as
// picking THAT kit up. CS2's pickup range is a touch trigger; 128 units is
// generous enough to survive the frame of latency between the pickup and the
// next FrameDone, and tight enough that two kits a room apart don't confuse.
const kitPickupRadius = 128.0

type PlayerTick struct {
	Tick          int      `json:"tick"`
	Name          string   `json:"name"`
	Side          string   `json:"side"`
	X             float64  `json:"X"`
	Y             float64  `json:"Y"`
	Z             float64  `json:"Z"`
	Health        int      `json:"health"`
	Yaw           float64  `json:"yaw"`
	Pitch         float64  `json:"pitch"`
	EyeZ          float64  `json:"eye_z"`
	FlashDuration float64  `json:"flash_duration"`
	Inventory     []string `json:"inventory"`
	HasC4         bool     `json:"has_c4"`
	ActiveWeapon  string   `json:"active_weapon"`
	RoundNum      int      `json:"round_num"`
	Money         int      `json:"money"`
	Armor         int      `json:"armor"`
	Helmet        bool     `json:"helmet"`
	DefuseKit     bool     `json:"defuse_kit"`
}

type ChunkEntry struct {
	File       string `json:"file"`
	Chunk      int    `json:"chunk"`
	RoundStart int    `json:"roundStart"`
	RoundEnd   int    `json:"roundEnd"`
	TickStart  int    `json:"tickStart"`
	TickEnd    int    `json:"tickEnd"`
}

// PlayerRank captures a player's competitive rank from the post-match rank
// reveal (events.RankUpdate) and the player entity. Only present for demos from
// official Valve servers; community/3rd-party demos report RankType 0 or -1.
//
//	RankType: 11 = Premier (Rank is a CS Rating, e.g. 15234)
//	          12 = Competitive (Rank is a skill group 0-18)
//	          7  = Wingman 2v2
//	          0/-1 = none / not a Valve server
type PlayerRank struct {
	Name       string `json:"name"`
	SteamID    string `json:"steam_id"` // string: 64-bit IDs exceed JS safe-integer range
	RankType   int    `json:"rank_type"`
	Rank       int    `json:"rank"` // new (post-match) rank
	RankOld    int    `json:"rank_old"`
	RankChange int    `json:"rank_change"`
	Wins       int    `json:"wins"`
}

type MatchOutput struct {
	MapName    string       `json:"mapName"`
	Mode       string       `json:"mode"`
	Timestamp  string       `json:"timestamp"`
	TickStep   int          `json:"tickStep"`
	ChunkIndex []ChunkEntry `json:"chunkIndex"`
	// v2 binary chunks (optional, -chunks-v2 flag - see chunks_v2.go)
	ChunkFormat  int                 `json:"chunkFormat,omitempty"`
	ChunkIndexV2 []ChunkEntry        `json:"chunkIndexV2,omitempty"`
	Rounds       []Round             `json:"rounds"`
	Kills        []Kill              `json:"kills"`
	Damage       []Damage            `json:"damage"`
	Blind        []Blind             `json:"blind"`
	Shots        []Shot              `json:"shots"`
	Smokes       []SmokeEvent        `json:"smokes"`
	Infernos     []SmokeEvent        `json:"infernos"`
	Flashes      []GrenadeDetonate   `json:"flashes"`
	HE           []GrenadeDetonate   `json:"he"`
	Grenades     []GrenadeTrajectory `json:"grenades"`
	Bomb         []BombEvent         `json:"bomb"`
	Items        []DroppedItem       `json:"items,omitempty"`
	Ranks        []PlayerRank        `json:"ranks"`
	// FACEIT room id, read from the .dem filename (see faceitMatchID). Only
	// set for a FACEIT demo whose original name survived; omitempty because
	// "not a FACEIT match" and "id unknown" are both nothing to link to.
	FaceitMatchID string `json:"faceitMatchId,omitempty"`
}

// ── Weapon name map (weapon_ prefix format viewer.js expects) ─────────────────
// Only include constants confirmed in demoinfocs-golang v3.3.0.
// Unknown weapons fall through to the string-based fallback in weaponName().

var equipToWeapon = map[common.EquipmentType]string{
	common.EqGlock:      "weapon_glock",
	common.EqUSP:        "weapon_usps",
	common.EqP2000:      "weapon_p2000",
	common.EqDeagle:     "weapon_deagle",
	common.EqRevolver:   "weapon_revolver",
	common.EqTec9:       "weapon_tec9",
	common.EqP250:       "weapon_p250",
	common.EqFiveSeven:  "weapon_fiveseven",
	common.EqCZ:         "weapon_cz75a",
	common.EqAK47:       "weapon_ak47",
	common.EqFamas:      "weapon_famas",
	common.EqGalil:      "weapon_galilar",
	common.EqM4A1:       "weapon_m4a1_silencer",
	common.EqM4A4:       "weapon_m4a4",
	common.EqSG556:      "weapon_sg556",
	common.EqAUG:        "weapon_aug",
	common.EqSSG08:      "weapon_ssg08",
	common.EqG3SG1:      "weapon_g3sg1",
	common.EqScar20:     "weapon_scar20",
	common.EqAWP:        "weapon_awp",
	common.EqXM1014:     "weapon_xm1014",
	common.EqNova:       "weapon_nova",
	common.EqSawedOff:   "weapon_sawedoff",
	common.EqMag7:       "weapon_mag7",
	common.EqM249:       "weapon_m249",
	common.EqNegev:      "weapon_negev",
	common.EqMP9:        "weapon_mp9",
	common.EqMP7:        "weapon_mp7",
	common.EqUMP:        "weapon_ump45",
	common.EqP90:        "weapon_p90",
	common.EqBizon:      "weapon_bizon",
	common.EqMac10:      "weapon_mac10",
	common.EqFlash:      "weapon_flashbang",
	common.EqSmoke:      "weapon_smokegrenade",
	common.EqHE:         "weapon_hegrenade",
	common.EqMolotov:    "weapon_molotov",
	common.EqIncendiary: "weapon_incgrenade",
	common.EqDecoy:      "weapon_decoy",
	common.EqBomb:       "weapon_c4",
	common.EqKnife:      "weapon_knife",
}

func weaponName(e *common.Equipment) string {
	if e == nil {
		return ""
	}
	if name, ok := equipToWeapon[e.Type]; ok {
		return name
	}
	s := strings.ToLower(e.String())
	s = strings.NewReplacer(" ", "_", "-", "").Replace(s)
	if s == "" || s == "unknown" {
		return ""
	}
	return "weapon_" + s
}

func grenadeTypeName(t common.EquipmentType) string {
	switch t {
	case common.EqFlash:
		return "Flashbang"
	case common.EqHE:
		return "HEGrenade"
	case common.EqSmoke:
		return "SmokeGrenade"
	case common.EqMolotov:
		return "Molotov"
	case common.EqIncendiary:
		return "Incendiary"
	case common.EqDecoy:
		return "Decoy"
	default:
		return "Unknown"
	}
}

// ── Helpers ───────────────────────────────────────────────────────────────────

func teamStr(t common.Team) string {
	switch t {
	case common.TeamCounterTerrorists:
		return "ct"
	case common.TeamTerrorists:
		return "t"
	default:
		return ""
	}
}

func pName(p *common.Player) string {
	if p == nil {
		return ""
	}
	return p.Name
}

func pSide(p *common.Player) string {
	if p == nil {
		return ""
	}
	return teamStr(p.Team)
}

func intPtr(i int) *int { v := i; return &v }

// roundEndReason converts to the string labels match.html expects.
// events.RoundEndReason has no .String() method in v3.3.0 - use a switch only.
func roundEndReason(r events.RoundEndReason) string {
	switch r {
	case events.RoundEndReasonTargetBombed:
		return "bomb_exploded"
	case events.RoundEndReasonBombDefused:
		return "bomb_defused"
	case events.RoundEndReasonCTWin:
		return "t_killed"
	case events.RoundEndReasonTerroristsWin:
		return "ct_killed"
	case events.RoundEndReasonTargetSaved:
		return "time_ran_out"
	default:
		return fmt.Sprintf("reason_%d", int(r))
	}
}

func bombSiteStr(site rune) (short string, full string) {
	switch site {
	case 'A':
		return "A", "bombsite_a"
	case 'B':
		return "B", "bombsite_b"
	default:
		return "", "not_planted"
	}
}

func safeScalar(v float64) float64 {
	if math.IsNaN(v) || math.IsInf(v, 0) {
		return 0
	}
	return v
}

// playerPos is a player's world position, NaN/Inf-scrubbed, or all zeroes for
// a nil player (a world kill has no attacker).
func playerPos(pl *common.Player) (float64, float64, float64) {
	if pl == nil {
		return 0, 0, 0
	}
	pos := pl.Position()
	x, y := safeXY(pos.X, pos.Y)
	z := pos.Z
	if math.IsNaN(z) || math.IsInf(z, 0) {
		z = 0
	}
	return x, y, z
}

func safeXY(x, y float64) (float64, float64) {
	if math.IsNaN(x) || math.IsInf(x, 0) {
		x = 0
	}
	if math.IsNaN(y) || math.IsInf(y, 0) {
		y = 0
	}
	return x, y
}

// infernoCenter returns the centroid of all fire positions in the inferno.
// Returns (0, 0) when no fires are registered yet (can happen at InfernoStart).
func infernoCenter(inf *common.Inferno) (float64, float64, float64) {
	fires := inf.Fires().List()
	if len(fires) == 0 {
		return 0, 0, 0
	}
	var sx, sy, sz float64
	for _, f := range fires {
		sx += f.X
		sy += f.Y
		sz += f.Z
	}
	n := float64(len(fires))
	x, y := safeXY(sx/n, sy/n)
	return x, y, safeScalar(sz / n)
}

// isCarryingBomb checks the player's weapon list for C4.
// common.Player has no IsCarryingC4() in v3.3.0.
func isCarryingBomb(p *common.Player) bool {
	if p == nil {
		return false
	}
	for _, w := range p.Weapons() {
		if w != nil && w.Type == common.EqBomb {
			return true
		}
	}
	return false
}

func playerInventory(p *common.Player) (inv []string, hasC4 bool, active string) {
	if p == nil {
		return
	}
	hasC4 = isCarryingBomb(p)
	if aw := p.ActiveWeapon(); aw != nil {
		active = weaponName(aw)
	}
	seen := map[string]bool{}
	for _, w := range p.Weapons() {
		if w == nil {
			continue
		}
		// Skip knives, bomb, and unknown
		switch w.Type {
		case common.EqKnife, common.EqBomb, common.EqUnknown:
			continue
		}
		n := weaponName(w)
		if n != "" && !seen[n] {
			inv = append(inv, n)
			seen[n] = true
		}
	}
	return
}

// ── Collector ─────────────────────────────────────────────────────────────────

type smokeState struct {
	startTick int
	x, y, z   float64
	thrower   string
	weapon    string         // infernos only: "Molotov" | "Incendiary"
	cells     []FireCell     // infernos only: ignition timeline
	cellSeen  map[int64]bool // quantized cell positions already recorded
}

type roundState struct {
	num       int
	start     int
	freezeEnd *int
	end       *int
	winner    string
	reason    string
	bombPlant *int
	bombSite  string
	economy   *Economy
}

type collector struct {
	rounds  []*roundState
	current *roundState
	roundN  int

	// Frame of the last MatchStart seen at 0-0 - where the real match begins.
	// 0 when the demo never fires one (ESL/pro demos don't). See
	// trimPreMatchRounds.
	matchStartFrame int

	// Known grenade entity IDs → type (populated in FrameDone)
	seenGrenades map[int]bool

	// thrower name → last fire-grenade type seen in flight ("Molotov" |
	// "Incendiary"); consumed by InfernoStart to tag the inferno's weapon.
	lastFireNade map[string]string

	kills    []Kill
	damage   []Damage
	blind    []Blind
	shots    []Shot
	smokes   []SmokeEvent
	infernos []SmokeEvent
	flashes  []GrenadeDetonate
	he       []GrenadeDetonate
	grenades []GrenadeTrajectory
	bomb     []BombEvent
	items    []DroppedItem

	// Ground-weapon tracking (FrameDone Owner-transition scan, see register()).
	groundWeapons   map[int]bool   // entityID -> currently tracked as on the ground
	lastWeaponOwner map[int]string // entityID -> most recently known owner name

	// Defuse kits on the ground. Tracked separately from groundWeapons and by
	// a completely different mechanism, because a CS2 demo has no entity for a
	// dropped kit at all: dumping every server class in a real match yields
	// CAK47, CKnife, CC4, CSmokeGrenade … and nothing defuser-shaped, while
	// the same match has five players holding kits and fifteen CTs dying with
	// one. The kit is only ever the pawn's m_bHasDefuser flag
	// (Player.HasDefuseKit()), so the ground state has to be derived:
	//   drop   = a player who had a kit dies, at the spot they died;
	//   pickup = a player who had no kit has one again, mid-round, standing
	//            near a kit we are tracking.
	// Cleared every round start, matching the round-scoped convention the
	// frontend's resolveGroundItems() already uses for weapons.
	groundKits map[string]*groundKit // synthetic item id -> position
	hadKit     map[string]bool       // player name -> had a kit last frame
	kitSeq     int

	tickData        map[int][]PlayerTick
	pendingSmokes   map[int]*smokeState
	pendingInfernos map[int]*smokeState
	// Set by RoundFreezetimeEnd, consumed by the next FrameDone - see the
	// economy snapshot comment on that handler for why it can't be taken
	// inline.
	econPending bool

	ranks    map[uint64]*PlayerRank // steamID64 -> rank
	steamIDs map[string]uint64      // name -> steamID64 (for v2 chunk player table)
}

func newCollector() *collector {
	return &collector{
		tickData:        make(map[int][]PlayerTick),
		pendingSmokes:   make(map[int]*smokeState),
		pendingInfernos: make(map[int]*smokeState),
		steamIDs:        make(map[string]uint64),
		seenGrenades:    make(map[int]bool),
		lastFireNade:    make(map[string]string),
		ranks:           make(map[uint64]*PlayerRank),
		groundWeapons:   make(map[int]bool),
		groundKits:      make(map[string]*groundKit),
		hadKit:          make(map[string]bool),
		lastWeaponOwner: make(map[int]string),
	}
}

// ── Event registration ────────────────────────────────────────────────────────

func (c *collector) register(p dem.Parser) {

	// ── Rounds ─────────────────────────────────────────────────────────────────
	p.RegisterEventHandler(func(e events.RoundStart) {
		c.roundN++
		tick := p.CurrentFrame()
		// Provisional freeze end: 15 s × 64 tick/s = 960 ticks after round
		// start. Overwritten below by the real RoundFreezetimeEnd tick - this
		// value only survives if that event never fires for the round.
		//
		// It survived on every round before this change, and 15 s is simply
		// not the freeze time in a Premier/MM match (20 s is), so the 2D
		// replayer's clock switched out of FREEZE five seconds early and was
		// already counting down when the round actually went live: the pistol
		// round read 1:45 at the moment it should have read 1:55.
		approxFreezeEnd := tick + 960
		rs := &roundState{
			num:       c.roundN,
			start:     tick,
			freezeEnd: intPtr(approxFreezeEnd),
			bombSite:  "not_planted",
		}
		c.rounds = append(c.rounds, rs)
		c.current = rs
		// Kits on the floor do not survive a round restart.
		c.groundKits = make(map[string]*groundKit)
		c.hadKit = make(map[string]bool)
	})

	// The real match start, which is NOT always the first round - see
	// trimPreMatchRounds. Only a MatchStart at 0-0 counts, so a mid-match
	// restart (a technical pause replayed from the scoreboard the teams
	// already had) can never throw away rounds that were played.
	p.RegisterEventHandler(func(e events.MatchStart) {
		gs := p.GameState()
		if gs.TeamCounterTerrorists().Score() == 0 && gs.TeamTerrorists().Score() == 0 {
			c.matchStartFrame = p.CurrentFrame()
		}
	})

	p.RegisterEventHandler(func(e events.RoundEnd) {
		if c.current != nil {
			c.current.end = intPtr(p.CurrentFrame())
			c.current.winner = teamStr(e.Winner)
			c.current.reason = roundEndReason(e.Reason)
		}
	})

	// Real freeze-time end: the round's freezeEnd tick, and the economy
	// snapshot - the latter TAKEN A FRAME LATER, in FrameDone.
	//
	// Two separate reasons the inline version was wrong (it reported a
	// second-pistol round with 33,500 of T equipment against a 4,000 team
	// buy):
	//
	//  1. Same class of bug as the entity-position gotcha above - entity
	//     properties are not final when an event dispatches.
	//     m_unFreezetimeEndEquipmentValue is latched by the game AT
	//     freeze-time end, i.e. in the same tick this event fires, so reading
	//     it here yields the PREVIOUS round's latch. Round 1 has no previous
	//     round, which is why it read a flat 0.
	//  2. It compounds across the halftime swap: the stale per-player values
	//     get re-summed under the players' NEW teams, so the second-pistol
	//     round inherits the other side's full-buy total.
	//
	// EquipmentValueCurrent() (m_unCurrentEquipmentValue) is live rather than
	// latched, so read one frame later it is the true "what each team is
	// holding as the round goes live", which is what the chart claims to
	// show. Money()/MoneySpentThisRound() were already correct at the event
	// and are equally correct a frame later (nothing can be bought in one
	// frame), so the whole snapshot moves together.
	p.RegisterEventHandler(func(e events.RoundFreezetimeEnd) {
		if c.current == nil {
			return
		}
		// The real freeze end, replacing the RoundStart+960 guess. Unlike the
		// equipment value below, a tick is a tick - there is no netvar to
		// latch, so it is correct at dispatch time.
		c.current.freezeEnd = intPtr(p.CurrentFrame())
		c.econPending = true
	})

	// ── Rank reveal (post-match) ─────────────────────────────────────────────────
	p.RegisterEventHandler(func(e events.RankUpdate) {
		id := e.SteamID64()
		r := c.ranks[id]
		if r == nil {
			r = &PlayerRank{SteamID: strconv.FormatUint(id, 10)}
			c.ranks[id] = r
		}
		r.Rank = e.RankNew
		r.RankOld = e.RankOld
		r.RankChange = int(math.Round(float64(e.RankChange)))
		r.Wins = e.WinCount
		if e.Player != nil {
			r.Name = e.Player.Name
			r.RankType = e.Player.RankType()
		}
	})

	// ── Kills ──────────────────────────────────────────────────────────────────
	p.RegisterEventHandler(func(e events.Kill) {
		if e.Victim == nil {
			return
		}
		attackerAir := false
		if e.Killer != nil {
			attackerAir = e.Killer.IsAirborne()
		}
		// Read at event time rather than deferred to FrameDone: unlike a
		// grenade detonation (whose entity isn't placed yet when the event
		// fires), both players are ordinary pawns standing where the duel
		// happened, and FrameDone is a frame LATER - by which point the victim
		// is a corpse and the attacker has moved on.
		ax, ay, az := playerPos(e.Killer)
		vx, vy, vz := playerPos(e.Victim)
		// A CT who dies with a defuse kit leaves it where they fell. Read here
		// for the same reason the coordinates above are: a frame later the
		// victim is a corpse.
		if e.Victim.HasDefuseKit() {
			c.kitSeq++
			id := fmt.Sprintf("kit-%d-%d", c.roundN, c.kitSeq)
			c.groundKits[id] = &groundKit{x: vx, y: vy, z: vz}
			c.hadKit[e.Victim.Name] = false
			c.items = append(c.items, DroppedItem{
				Tick: p.CurrentFrame(), Event: "drop",
				X: vx, Y: vy, Z: vz,
				Weapon: "item_defuser", Name: e.Victim.Name, ItemID: id,
			})
		}
		c.kills = append(c.kills, Kill{
			Tick:          p.CurrentFrame(),
			RoundNum:      c.roundN,
			AttackerName:  pName(e.Killer),
			AttackerSide:  pSide(e.Killer),
			VictimName:    e.Victim.Name,
			VictimSide:    teamStr(e.Victim.Team),
			AssisterName:  pName(e.Assister),
			AssistFlash:   e.Assister != nil && e.AssistedFlash,
			Weapon:        weaponName(e.Weapon),
			Headshot:      e.IsHeadshot,
			Wallbang:      e.IsWallBang(),
			ThroughSmoke:  e.ThroughSmoke,
			AttackerBlind: e.AttackerBlind,
			NoScope:       e.NoScope,
			AttackerAir:   attackerAir,
			Suicide:       e.Killer == nil || e.Killer == e.Victim,
			AttackerX:     ax,
			AttackerY:     ay,
			AttackerZ:     az,
			VictimX:       vx,
			VictimY:       vy,
			VictimZ:       vz,
		})
	})

	// ── Damage ─────────────────────────────────────────────────────────────────
	p.RegisterEventHandler(func(e events.PlayerHurt) {
		if e.Player == nil {
			return
		}
		c.damage = append(c.damage, Damage{
			Tick:         p.CurrentFrame(),
			RoundNum:     c.roundN,
			AttackerName: pName(e.Attacker),
			AttackerSide: pSide(e.Attacker),
			VictimName:   e.Player.Name,
			VictimSide:   teamStr(e.Player.Team),
			DmgHealth:    e.HealthDamage,
			Weapon:       weaponName(e.Weapon),
		})
	})

	// ── Shots ──────────────────────────────────────────────────────────────────
	p.RegisterEventHandler(func(e events.WeaponFire) {
		if e.Shooter == nil {
			return
		}
		pos := e.Shooter.Position()
		ex, ey := safeXY(pos.X, pos.Y)
		ez := safeScalar(pos.Z) + 64.09
		if eyes, ok := e.Shooter.PositionEyes(); ok {
			ex, ey = safeXY(eyes.X, eyes.Y)
			ez = safeScalar(eyes.Z)
		}
		c.shots = append(c.shots, Shot{
			Tick:       p.CurrentFrame(),
			PlayerName: e.Shooter.Name,
			PlayerSide: teamStr(e.Shooter.Team),
			Weapon:     weaponName(e.Weapon),
			Yaw:        safeScalar(float64(e.Shooter.ViewDirectionX())),
			Pitch:      safeScalar(float64(e.Shooter.ViewDirectionY())),
			EyeX:       ex,
			EyeY:       ey,
			EyeZ:       ez,
		})
	})

	// ── Blind ──────────────────────────────────────────────────────────────────
	// e.FlashDuration is func() time.Duration in v3.3.0 - call it before .Seconds()
	p.RegisterEventHandler(func(e events.PlayerFlashed) {
		if e.Player == nil || e.Attacker == nil {
			return
		}
		c.blind = append(c.blind, Blind{
			Tick:          p.CurrentFrame(),
			AttackerName:  e.Attacker.Name,
			AttackerSide:  teamStr(e.Attacker.Team),
			VictimName:    e.Player.Name,
			VictimSide:    teamStr(e.Player.Team),
			BlindDuration: e.FlashDuration().Seconds(),
		})
	})

	// ── Smokes ─────────────────────────────────────────────────────────────────
	p.RegisterEventHandler(func(e events.SmokeStart) {
		thrower := ""
		if e.Thrower != nil {
			thrower = e.Thrower.Name
		}
		x, y := safeXY(e.Position.X, e.Position.Y)
		c.pendingSmokes[e.GrenadeEntityID] = &smokeState{
			startTick: p.CurrentFrame(),
			x:         x,
			y:         y,
			z:         safeScalar(e.Position.Z),
			thrower:   thrower,
		}
	})

	p.RegisterEventHandler(func(e events.SmokeExpired) {
		if s, ok := c.pendingSmokes[e.GrenadeEntityID]; ok {
			c.smokes = append(c.smokes, SmokeEvent{
				StartTick:   s.startTick,
				EndTick:     p.CurrentFrame(),
				X:           s.x,
				Y:           s.y,
				Z:           s.z,
				ThrowerName: s.thrower,
			})
			delete(c.pendingSmokes, e.GrenadeEntityID)
		}
	})

	// ── Infernos ───────────────────────────────────────────────────────────────
	p.RegisterEventHandler(func(e events.InfernoStart) {
		if e.Inferno == nil {
			return
		}
		thrower := ""
		if t := e.Inferno.Thrower(); t != nil {
			thrower = t.Name
		}
		// Hull may be empty at InfernoStart (fire cells not yet registered);
		// InfernoExpired overrides with the fully-expanded hull centroid.
		cx, cy, cz := infernoCenter(e.Inferno)
		id := int(e.Inferno.UniqueID())
		c.pendingInfernos[id] = &smokeState{
			startTick: p.CurrentFrame(),
			x:         cx,
			y:         cy,
			z:         cz,
			thrower:   thrower,
			weapon:    c.lastFireNade[thrower], // "" when the throw predates parsing
			cellSeen:  make(map[int64]bool),
		}
	})

	p.RegisterEventHandler(func(e events.InfernoExpired) {
		if e.Inferno == nil {
			return
		}
		id := int(e.Inferno.UniqueID())
		if s, ok := c.pendingInfernos[id]; ok {
			// Override with hull centroid at expiry - this is the most reliable
			// position since the hull is fully expanded and stable by then.
			if cx, cy, cz := infernoCenter(e.Inferno); cx != 0 || cy != 0 {
				s.x, s.y, s.z = cx, cy, cz
			}
			c.infernos = append(c.infernos, SmokeEvent{
				StartTick:   s.startTick,
				EndTick:     p.CurrentFrame(),
				X:           s.x,
				Y:           s.y,
				Z:           s.z,
				ThrowerName: s.thrower,
				Weapon:      s.weapon,
				Cells:       s.cells,
			})
			delete(c.pendingInfernos, id)
		}
	})

	// ── Flash detonation ───────────────────────────────────────────────────────
	p.RegisterEventHandler(func(e events.FlashExplode) {
		thrower := ""
		if e.Thrower != nil {
			thrower = e.Thrower.Name
		}
		x, y := safeXY(e.Position.X, e.Position.Y)
		c.flashes = append(c.flashes, GrenadeDetonate{
			Tick: p.CurrentFrame(), X: x, Y: y, Z: safeScalar(e.Position.Z), ThrowerName: thrower,
		})
	})

	// ── HE detonation ──────────────────────────────────────────────────────────
	p.RegisterEventHandler(func(e events.HeExplode) {
		thrower := ""
		if e.Thrower != nil {
			thrower = e.Thrower.Name
		}
		x, y := safeXY(e.Position.X, e.Position.Y)
		c.he = append(c.he, GrenadeDetonate{
			Tick: p.CurrentFrame(), X: x, Y: y, Z: safeScalar(e.Position.Z), ThrowerName: thrower,
		})
	})

	// ── Bomb ───────────────────────────────────────────────────────────────────
	p.RegisterEventHandler(func(e events.BombPlanted) {
		tick := p.CurrentFrame()
		name := pName(e.Player)
		short, full := bombSiteStr(rune(e.Site))
		if c.current != nil {
			c.current.bombPlant = intPtr(tick)
			c.current.bombSite = full
		}
		bx, by := 0.0, 0.0
		if b := p.GameState().Bomb(); b != nil {
			pos := b.Position()
			bx, by = safeXY(pos.X, pos.Y)
		}
		var sitePtr *string
		if short != "" {
			sitePtr = &short
		}
		c.bomb = append(c.bomb, BombEvent{
			Tick: tick, Event: "plant",
			X: bx, Y: by, Name: name, Bombsite: sitePtr,
		})
	})

	p.RegisterEventHandler(func(e events.BombDefused) {
		tick := p.CurrentFrame()
		bx, by := 0.0, 0.0
		if b := p.GameState().Bomb(); b != nil {
			pos := b.Position()
			bx, by = safeXY(pos.X, pos.Y)
		}
		c.bomb = append(c.bomb, BombEvent{
			Tick: tick, Event: "defuse",
			X: bx, Y: by, Name: pName(e.Player),
		})
	})

	p.RegisterEventHandler(func(e events.BombPickup) {
		tick := p.CurrentFrame()
		bx, by := 0.0, 0.0
		if b := p.GameState().Bomb(); b != nil {
			pos := b.Position()
			bx, by = safeXY(pos.X, pos.Y)
		}
		c.bomb = append(c.bomb, BombEvent{
			Tick: tick, Event: "pickup",
			X: bx, Y: by, Name: pName(e.Player),
		})
	})

	// events.BombExplode DOES exist in v5. The comment that used to sit here
	// said otherwise and inferred detonation from RoundEnd's TargetBombed
	// reason - but it named `BombExploded`, which is not the identifier. The
	// cost was a real one: viewer.js's clock has a `case 'detonate'` branch
	// that could never fire, because nothing ever emitted that event.
	p.RegisterEventHandler(func(e events.BombExplode) {
		tick := p.CurrentFrame()
		bx, by := 0.0, 0.0
		if b := p.GameState().Bomb(); b != nil {
			pos := b.Position()
			bx, by = safeXY(pos.X, pos.Y)
		}
		c.bomb = append(c.bomb, BombEvent{
			Tick: tick, Event: "detonate",
			X: bx, Y: by, Name: pName(e.Player),
		})
	})

	// ── In-progress plant/defuse ──────────────────────────────────────────
	//
	// These four are what let the viewer draw an action while it is happening
	// rather than only its result. They carry the START tick, which is the
	// half a per-tick `IsPlanting`/`IsDefusing` bool cannot give without
	// scanning backwards for a leading edge - and BombDefuseStart also
	// carries HasKit, i.e. whether this defuse takes 5 s or 10 s. That single
	// bool is what makes "will the defuse beat the fuse?" answerable.
	//
	// Position is the bomb's, not the player's: for a defuse they are within
	// arm's reach of each other, and the bomb is the thing the action is
	// about. Plant-begin has no bomb on the ground yet, so it reads the
	// carrier's position, which Bomb.Position() already resolves.
	p.RegisterEventHandler(func(e events.BombPlantBegin) {
		tick := p.CurrentFrame()
		bx, by := 0.0, 0.0
		if b := p.GameState().Bomb(); b != nil {
			pos := b.Position()
			bx, by = safeXY(pos.X, pos.Y)
		}
		short, _ := bombSiteStr(rune(e.Site))
		var sitePtr *string
		if short != "" {
			sitePtr = &short
		}
		c.bomb = append(c.bomb, BombEvent{
			Tick: tick, Event: "plant_begin",
			X: bx, Y: by, Name: pName(e.Player), Bombsite: sitePtr,
		})
	})

	p.RegisterEventHandler(func(e events.BombPlantAborted) {
		c.bomb = append(c.bomb, BombEvent{
			Tick: p.CurrentFrame(), Event: "plant_abort",
			Name: pName(e.Player),
		})
	})

	p.RegisterEventHandler(func(e events.BombDefuseStart) {
		tick := p.CurrentFrame()
		bx, by := 0.0, 0.0
		if b := p.GameState().Bomb(); b != nil {
			pos := b.Position()
			bx, by = safeXY(pos.X, pos.Y)
		}
		hasKit := e.HasKit
		c.bomb = append(c.bomb, BombEvent{
			Tick: tick, Event: "defuse_start",
			X: bx, Y: by, Name: pName(e.Player), HasKit: &hasKit,
		})
	})

	p.RegisterEventHandler(func(e events.BombDefuseAborted) {
		c.bomb = append(c.bomb, BombEvent{
			Tick: p.CurrentFrame(), Event: "defuse_abort",
			Name: pName(e.Player),
		})
	})

	p.RegisterEventHandler(func(e events.BombDropped) {
		tick := p.CurrentFrame()
		bx, by := 0.0, 0.0
		if b := p.GameState().Bomb(); b != nil {
			pos := b.Position()
			bx, by = safeXY(pos.X, pos.Y)
		}
		c.bomb = append(c.bomb, BombEvent{
			Tick: tick, Event: "drop",
			X: bx, Y: by, Name: pName(e.Player),
		})
	})

	// ── Per-frame: player snapshots, grenade trails, freeze detection ──────────
	// events.GrenadeProjectileCreate absent in v3.3.0 - detect new grenades here.
	p.RegisterEventHandler(func(_ events.FrameDone) {
		gs := p.GameState()
		if gs.IsWarmupPeriod() || !gs.IsMatchStarted() {
			return
		}

		tick := p.CurrentFrame()

		// Deferred economy snapshot - see the RoundFreezetimeEnd handler.
		if c.econPending && c.current != nil {
			c.econPending = false
			econ := &Economy{}
			for _, pl := range gs.Participants().Playing() {
				if pl == nil {
					continue
				}
				switch pl.Team {
				case common.TeamCounterTerrorists:
					econ.CTEquip += pl.EquipmentValueCurrent()
					econ.CTCash += pl.Money()
					econ.CTSpent += pl.MoneySpentThisRound()
				case common.TeamTerrorists:
					econ.TEquip += pl.EquipmentValueCurrent()
					econ.TCash += pl.Money()
					econ.TSpent += pl.MoneySpentThisRound()
				}
			}
			c.current.economy = econ
		}

		// Grenade trajectories
		for id, g := range gs.GrenadeProjectiles() {
			if g == nil {
				continue
			}
			pos := g.Position()
			x, y := safeXY(pos.X, pos.Y)
			thrower := ""
			if g.Thrower != nil {
				thrower = g.Thrower.Name
			}
			gtype := grenadeTypeName(g.WeaponInstance.Type)
			if gtype == "Molotov" || gtype == "Incendiary" {
				// remembered so InfernoStart can tag its weapon (the inferno
				// entity itself doesn't know which grenade lit it)
				c.lastFireNade[thrower] = gtype
			}
			c.grenades = append(c.grenades, GrenadeTrajectory{
				Tick:        tick,
				GrenadeType: gtype,
				X:           x,
				Y:           y,
				Z:           safeScalar(pos.Z),
				Thrower:     thrower,
				EntityID:    id,
			})
		}

		// Dropped weapons/utility on the ground: events.ItemDrop/ItemPickup
		// ("not available in all demos" per demoinfocs docs) don't actually
		// fire on this fork's CS2 demos - observed empirically: ItemPickup
		// fires for ordinary buys, ItemDrop never fires at all. Instead, diff
		// each weapon entity's Owner across frames: bindWeapon (datatables.go)
		// sets Owner nil whenever m_hOwnerEntity can't resolve to a live
		// player, which is exactly the on-the-ground state (manual drop key
		// or a death-drop) - same mechanism already used for the bomb, just
		// applied to every weapon entity instead of the one bomb singleton.
		// A thrown grenade's Equipment entity is destroyed (removed from
		// gs.Weapons()) once it becomes a GrenadeProjectile, so this doesn't
		// spuriously fire for every throw - only for a genuinely dropped nade.
		for id, w := range gs.Weapons() {
			if w == nil || w.Entity == nil || w.Type == common.EqBomb || w.Type == common.EqKnife {
				continue
			}
			if w.Owner != nil {
				c.lastWeaponOwner[id] = pName(w.Owner)
			}
			onGround := w.Owner == nil
			if onGround == c.groundWeapons[id] {
				continue
			}
			pos := w.Entity.Position()
			wx, wy := safeXY(pos.X, pos.Y)
			if onGround {
				c.items = append(c.items, DroppedItem{
					Tick: tick, Event: "drop",
					X: wx, Y: wy, Z: safeScalar(pos.Z),
					Weapon: weaponName(w), Name: c.lastWeaponOwner[id],
					ItemID: w.UniqueID2().String(),
				})
				c.groundWeapons[id] = true
			} else {
				c.items = append(c.items, DroppedItem{
					Tick: tick, Event: "pickup",
					X: wx, Y: wy, Z: safeScalar(pos.Z),
					Weapon: weaponName(w), Name: pName(w.Owner),
					ItemID: w.UniqueID2().String(),
				})
				delete(c.groundWeapons, id)
			}
		}

		// Inferno fire-cell ignition timeline: record each burning cell the
		// first frame it appears (quantized to a 32-unit grid), capped so a
		// pathological demo can't bloat the JSON. Cells only accumulate, so
		// the viewer reconstructs the spreading footprint from tick order.
		for _, inf := range gs.Infernos() {
			if inf == nil {
				continue
			}
			s, ok := c.pendingInfernos[int(inf.UniqueID())]
			if !ok || len(s.cells) >= 96 {
				continue
			}
			for _, f := range inf.Fires().Active().List() {
				fx, fy := safeXY(f.X, f.Y)
				key := (int64(fx/32)+1<<20)<<21 | (int64(fy/32) + 1<<20)
				if s.cellSeen[key] {
					continue
				}
				s.cellSeen[key] = true
				s.cells = append(s.cells, FireCell{Tick: tick, X: fx, Y: fy})
				if len(s.cells) >= 96 {
					break
				}
			}
		}

		// Player snapshots
		for _, pl := range gs.Participants().Playing() {
			if pl == nil {
				continue
			}
			side := teamStr(pl.Team)
			if side == "" {
				continue
			}
			pos := pl.Position()
			px, py := safeXY(pos.X, pos.Y)
			pz := safeScalar(pos.Z)
			c.steamIDs[pl.Name] = pl.SteamID64
			// Eye height above the feet origin tracks crouch state. PositionEyes
			// may report invalid data on some frames (second return false) - fall
			// back to standing eye height.
			eyeZ := pz + 64.09
			if eyes, ok := pl.PositionEyes(); ok {
				eyeZ = safeScalar(eyes.Z)
			}
			inv, hasC4, activeWep := playerInventory(pl)

			// Defuse-kit pickup: this player has a kit now and did not last
			// frame. Buying one in the freeze time also flips the flag, so
			// only a gain while a kit we are tracking is within reach counts -
			// which also correctly ignores a CT who buys a second kit while
			// standing on a dead teammate's.
			hasKit := pl.HasDefuseKit()
			if hasKit && !c.hadKit[pl.Name] {
				bestID, bestD := "", kitPickupRadius
				for id, k := range c.groundKits {
					d := math.Hypot(px-k.x, py-k.y)
					if d < bestD {
						bestD, bestID = d, id
					}
				}
				if bestID != "" {
					// X/Y/Z on a pickup are meaningless by convention (the
					// frontend only uses the event to retire a marker by
					// item_id) - same as the weapon path above.
					c.items = append(c.items, DroppedItem{
						Tick: tick, Event: "pickup",
						Weapon: "item_defuser", Name: pl.Name, ItemID: bestID,
					})
					delete(c.groundKits, bestID)
				}
			}
			c.hadKit[pl.Name] = hasKit

			fd := 0.0
			if dur := pl.FlashDurationTime(); dur > 0 {
				fd = dur.Seconds()
			}

			if c.tickData[tick] == nil {
				c.tickData[tick] = []PlayerTick{}
			}
			c.tickData[tick] = append(c.tickData[tick], PlayerTick{
				Tick:          tick,
				Name:          pl.Name,
				Side:          side,
				X:             px,
				Y:             py,
				Z:             pz,
				Health:        pl.Health(),
				Yaw:           float64(pl.ViewDirectionX()),
				Pitch:         safeScalar(float64(pl.ViewDirectionY())),
				EyeZ:          eyeZ,
				FlashDuration: fd,
				Inventory:     inv,
				HasC4:         hasC4,
				ActiveWeapon:  activeWep,
				RoundNum:      c.roundN,
				Money:         pl.Money(),
				Armor:         pl.Armor(),
				Helmet:        pl.HasHelmet(),
				DefuseKit:     hasKit,
			})
		}
	})
}

// ── Pre-match rounds ──────────────────────────────────────────────────────────

// trimPreMatchRounds drops everything the demo recorded before the match
// actually started and renumbers what is left from 1. Returns how many rounds
// went.
//
// FACEIT plays a KNIFE ROUND for side choice, then a warmup round while the
// winners pick, and only then starts the match - so the reference demo's
// "round 3" is round 1, its 26 rounds are a 24-round match, and the halftime
// swap lands two rounds late in every stat, moment and highlight that counts
// from the round number. The knife round also carries real kills, which would
// otherwise be scored as if they were part of the match.
//
// `events.MatchStart` is the marker, and the rule is general rather than
// FACEIT-specific: measured against this repo's corpus, a Valve MM demo fires
// it at the same frame as its first RoundStart (nothing is dropped) and an
// ESL/pro demo never fires it at all (nothing is dropped). A knife round is a
// knife round wherever it is played, so nothing here tests the mode.
//
// Events are dropped by TICK rather than by round number: only Kill, Damage
// and PlayerTick carry a round, while shots, utility, bomb and item rows carry
// only a tick, and a pre-match event is not part of the match on either
// reading. The tick data goes with them, which is what keeps a knife round out
// of the chunk files.
func (c *collector) trimPreMatchRounds() int {
	start := c.matchStartFrame
	if start <= 0 {
		return 0
	}

	kept := c.rounds[:0:0]
	for _, rs := range c.rounds {
		if rs.start < start {
			continue
		}
		rs.num = len(kept) + 1
		kept = append(kept, rs)
	}
	dropped := len(c.rounds) - len(kept)
	if dropped == 0 {
		return 0
	}
	c.rounds = kept
	c.roundN = len(kept)

	c.kills = trimRows(c.kills, start, dropped, func(k *Kill) (*int, *int) { return &k.Tick, &k.RoundNum })
	c.damage = trimRows(c.damage, start, dropped, func(d *Damage) (*int, *int) { return &d.Tick, &d.RoundNum })
	c.blind = trimRows(c.blind, start, dropped, func(b *Blind) (*int, *int) { return &b.Tick, nil })
	c.shots = trimRows(c.shots, start, dropped, func(x *Shot) (*int, *int) { return &x.Tick, nil })
	c.flashes = trimRows(c.flashes, start, dropped, func(g *GrenadeDetonate) (*int, *int) { return &g.Tick, nil })
	c.he = trimRows(c.he, start, dropped, func(g *GrenadeDetonate) (*int, *int) { return &g.Tick, nil })
	c.grenades = trimRows(c.grenades, start, dropped, func(g *GrenadeTrajectory) (*int, *int) { return &g.Tick, nil })
	c.bomb = trimRows(c.bomb, start, dropped, func(b *BombEvent) (*int, *int) { return &b.Tick, nil })
	c.items = trimRows(c.items, start, dropped, func(i *DroppedItem) (*int, *int) { return &i.Tick, nil })
	c.smokes = trimRows(c.smokes, start, dropped, func(sm *SmokeEvent) (*int, *int) { return &sm.StartTick, nil })
	c.infernos = trimRows(c.infernos, start, dropped, func(sm *SmokeEvent) (*int, *int) { return &sm.StartTick, nil })

	for tick := range c.tickData {
		if tick < start {
			delete(c.tickData, tick)
			continue
		}
		for i := range c.tickData[tick] {
			c.tickData[tick][i].RoundNum -= dropped
		}
	}
	return dropped
}

// trimRows keeps the rows at or after `start` and, for rows that carry a round
// number, shifts it down by the rounds that were dropped. `fields` returns
// pointers to the row's tick and to its round number (nil when it has none),
// which is what lets one pass cover eleven differently-shaped slices.
func trimRows[T any](rows []T, start, dropped int, fields func(*T) (*int, *int)) []T {
	out := rows[:0]
	for i := range rows {
		tick, round := fields(&rows[i])
		if *tick < start {
			continue
		}
		if round != nil {
			*round -= dropped
		}
		out = append(out, rows[i])
	}
	return out
}

// ── Chunk writing ─────────────────────────────────────────────────────────────

func (c *collector) roundForTick(tick int) int {
	for i := len(c.rounds) - 1; i >= 0; i-- {
		if c.rounds[i].start <= tick {
			return c.rounds[i].num
		}
	}
	return 0
}

func writeChunks(c *collector, chunksDir string) ([]ChunkEntry, error) {
	if err := os.MkdirAll(chunksDir, 0o755); err != nil {
		return nil, err
	}

	ticks := make([]int, 0, len(c.tickData))
	for t := range c.tickData {
		ticks = append(ticks, t)
	}
	sort.Ints(ticks)

	type roundTicks struct {
		num   int
		ticks []int
	}
	roundMap := map[int]*roundTicks{}
	var roundOrder []int
	for _, t := range ticks {
		rn := c.roundForTick(t)
		if rn == 0 {
			continue
		}
		if _, ok := roundMap[rn]; !ok {
			roundMap[rn] = &roundTicks{num: rn}
			roundOrder = append(roundOrder, rn)
		}
		roundMap[rn].ticks = append(roundMap[rn].ticks, t)
	}
	sort.Ints(roundOrder)

	var entries []ChunkEntry
	chunkIdx := 0

	for i := 0; i < len(roundOrder); i += roundsPerChunk {
		end := i + roundsPerChunk
		if end > len(roundOrder) {
			end = len(roundOrder)
		}
		group := roundOrder[i:end]

		chunkData := map[string][]PlayerTick{}
		minTick, maxTick := 0, 0
		first := true

		for _, rn := range group {
			for _, t := range roundMap[rn].ticks {
				key := fmt.Sprintf("%d", t)
				chunkData[key] = c.tickData[t]
				if first || t < minTick {
					minTick = t
				}
				if first || t > maxTick {
					maxTick = t
				}
				first = false
			}
		}

		filename := fmt.Sprintf("ticks_chunk_%03d.json", chunkIdx)
		path := filepath.Join(chunksDir, filename)
		f, err := os.Create(path)
		if err != nil {
			return nil, fmt.Errorf("create chunk: %w", err)
		}
		if err := json.NewEncoder(f).Encode(chunkData); err != nil {
			f.Close()
			return nil, fmt.Errorf("encode chunk: %w", err)
		}
		f.Close()

		entries = append(entries, ChunkEntry{
			File: filename, Chunk: chunkIdx,
			RoundStart: group[0], RoundEnd: group[len(group)-1],
			TickStart: minTick, TickEnd: maxTick,
		})
		chunkIdx++
	}
	return entries, nil
}

// ── Metadata helpers ──────────────────────────────────────────────────────────

// mapNamePrefixes are the map-name prefixes searched for in a demo header, in
// the order they are tried.
var mapNamePrefixes = []string{"de_", "cs_", "ar_"}

// mapsEndingInDigit lists maps whose real name ends in a digit, so the
// no-length-prefix fallback in trimTrailingDigits does not trim it away.
var mapsEndingInDigit = map[string]bool{
	"de_dust2": true,
}

// parseMapName pulls the map name out of a demo file header, or returns "" if
// the header holds nothing map-shaped.
//
// In a CS2 PBDEMS2 header the map is a length-prefixed protobuf string: the
// byte immediately before the name holds its exact length, as in
// "\x08de_dust2". Reading that prefix is what separates the name from the
// framing bytes that follow it, because those bytes are often themselves
// printable ASCII digits - the byte after "de_dust2" is 0x32, the character
// '2'. Scanning naively for a run of [a-z0-9_] therefore yields "de_dust22",
// and trimming that run's trailing digits yields "de_dust", a different map:
// de_dust (Dust) and de_dust2 (Dust II) both exist and ship separate radars
// and icons.
//
// Older CS:GO headers carry no usable length prefix, so those fall back to
// trimming trailing digits off the raw run.
func parseMapName(content string) string {
	for _, prefix := range mapNamePrefixes {
		idx := strings.Index(content, prefix)
		if idx < 0 {
			continue
		}
		end := idx + len(prefix)
		for end < len(content) {
			c := content[end]
			if (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_' {
				end++
			} else {
				break
			}
		}
		run := content[idx:end]
		if len(run) <= len(prefix) {
			continue
		}
		if name := trustedLengthPrefix(content, idx, run, len(prefix)); name != "" {
			return name
		}
		if name := trimTrailingDigits(run, len(prefix)); name != "" {
			return name
		}
	}
	return ""
}

// trustedLengthPrefix returns run truncated to the length declared by the byte
// preceding it, or "" when that byte is not a believable length for this run.
//
// The prefix is only trusted when its cut lands on a boundary that is not
// mid-word: the character it would cut before must not be a letter or an
// underscore. Framing bytes are digits, so a letter there means the preceding
// byte was ordinary header noise that happened to look like a length, and
// honouring it would truncate a legitimate name ("de_mirage" → "de_mirag").
func trustedLengthPrefix(content string, idx int, run string, minLen int) string {
	if idx == 0 {
		return ""
	}
	n := int(content[idx-1])
	if n <= minLen || n >= len(run) {
		return "" // no room to cut, or the cut leaves less than a bare prefix
	}
	if c := run[n]; (c >= 'a' && c <= 'z') || c == '_' {
		return ""
	}
	return run[:n]
}

// trimTrailingDigits strips trailing version/framing digits off a raw scanned
// run, leaving maps whose name genuinely ends in a digit intact. Returns ""
// when nothing but the bare prefix survives.
func trimTrailingDigits(candidate string, minLen int) string {
	for len(candidate) > minLen && !mapsEndingInDigit[candidate] &&
		candidate[len(candidate)-1] >= '0' && candidate[len(candidate)-1] <= '9' {
		candidate = candidate[:len(candidate)-1]
	}
	if len(candidate) <= minLen {
		return ""
	}
	return candidate
}

// readDemoMeta scans the first 4 KB of the .dem file for the map name and
// game mode. CS2 PBDEMS2 demos embed plaintext server/map info near the start.
func readDemoMeta(demoPath string) (mapName, mode string) {
	mode = "mm"
	mapName = "unknown"

	f, err := os.Open(demoPath)
	if err != nil {
		return
	}
	defer f.Close()

	buf := make([]byte, 4096)
	n, _ := f.Read(buf)
	content := string(buf[:n])

	if name := parseMapName(content); name != "" {
		mapName = name
	}

	if m := modeFromHeader(content); m != "" {
		mode = m
	}
	return
}

// modeFromHeader reads the mode out of a demo header's server name, the one
// thing a demo states about where it was played. Measured across this repo's
// corpus:
//
//	Valve MM       "Valve Counter-Strike 2 eu_west Server (srcds423-fra2…)"
//	Valve Premier  the same, carrying the word "premier"
//	FACEIT         " FACEIT.com register to play here"
//	ESL / pro      "ESL Match Server #2"
//
// Returns "" when nothing is recognised, leaving the caller's default ("mm")
// - there is no "pro" mode, so an ESL demo is an ordinary match here.
//
// FACEIT is tested first. A FACEIT server name cannot contain "premier"
// today, but if one ever did, WHERE the match was played is the stronger fact:
// a FACEIT match is not a Valve Premier match whatever its name says.
func modeFromHeader(content string) string {
	lower := strings.ToLower(content)
	switch {
	case strings.Contains(lower, "faceit"):
		return "faceit"
	case strings.Contains(lower, "premier"):
		return "premier"
	}
	return ""
}

// faceitMatchID pulls the FACEIT room id out of a demo's filename.
//
// FACEIT hands out `1-<uuid>-<map>-<half>.dem`, and the room id - what
// faceit.com/…/room/<id> takes - is the `1-<uuid>` prefix. The elo the room
// page shows is NOT in the demo: a FACEIT demo carries no rank fields (every
// player is rank_type -1), no elo convar and no elo in the FACEIT bot's own
// chat lines. The id is what lets the app point at where that number lives
// rather than invent one.
//
// Returns "" for any other name - a renamed or hand-saved demo simply has no
// id, which the frontend renders as no link.
func faceitMatchID(demoPath string) string {
	base := filepath.Base(demoPath)
	m := faceitIDRe.FindStringSubmatch(base)
	if m == nil {
		return ""
	}
	return strings.ToLower(m[1])
}

var faceitIDRe = regexp.MustCompile(
	`^(1-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\b`)

func extractTimestamp(demoPath string) string {
	info, err := os.Stat(demoPath)
	if err != nil {
		return time.Now().UTC().Format("20060102_1504")
	}
	return info.ModTime().UTC().Format("20060102_1504")
}

// ── Main ──────────────────────────────────────────────────────────────────────

// ── Progress reporting ────────────────────────────────────────────────────────
//
// app.py reads these lines off the parser's stdout to drive the dashboard's
// per-match progress bar. Format, one per line:
//
//	PROGRESS <stage> <0..1>
//
// Bytes consumed from the .dem is the measure, NOT Parser.Progress(): a CS2
// demo's header carries PlaybackFrames == 0 until the parse is over (the
// library backfills it in ensurePlaybackValuesAreSet), so Progress() returns a
// flat 0 for the whole run. The file offset is the only honest mid-parse
// signal. It runs slightly ahead of the work done because the bit reader buffers,
// which is harmless for a bar and for an ETA.
//
// Anything that does not match the prefix stays ordinary stdout, so a caller
// that ignores progress (a terminal run) is unaffected.

const progressInterval = 250 * time.Millisecond

func reportProgress(stage string, frac float64) {
	if frac < 0 {
		frac = 0
	} else if frac > 1 {
		frac = 1
	}
	fmt.Printf("PROGRESS %s %.4f\n", stage, frac)
	os.Stdout.Sync()
}

// countingReader counts the bytes handed to the parser. Read is called from the
// parse goroutine and the count is read by the ticker, hence the atomic.
type countingReader struct {
	r io.Reader
	n atomic.Int64
}

func (c *countingReader) Read(p []byte) (int, error) {
	n, err := c.r.Read(p)
	if n > 0 {
		c.n.Add(int64(n))
	}
	return n, err
}

// trackProgress emits "PROGRESS parse <frac>" until stop is closed. With an
// unknown size (stat failed) it emits nothing rather than a made-up fraction -
// the client then shows an indeterminate bar instead of a lying one.
func trackProgress(cr *countingReader, size int64, stop <-chan struct{}) {
	if size <= 0 {
		return
	}
	t := time.NewTicker(progressInterval)
	defer t.Stop()
	for {
		select {
		case <-stop:
			return
		case <-t.C:
			reportProgress("parse", float64(cr.n.Load())/float64(size))
		}
	}
}

func main() {
	demoPath := flag.String("demo", "", "path to the .dem file")
	outPath := flag.String("out", "", "output master JSON path")
	chunksDir := flag.String("chunks", "", "directory for tick chunk files")
	chunksV2 := flag.Bool("chunks-v2", false, "also emit v2 binary tick chunks (the app's tick format - see chunks_v2.go)")
	noJSONChunks := flag.Bool("no-json-chunks", false, "skip the legacy JSON tick chunks (requires -chunks-v2; all viewers read v2)")
	flag.Parse()

	if *noJSONChunks && !*chunksV2 {
		log.Fatal("-no-json-chunks requires -chunks-v2 (there would be no tick data at all)")
	}

	if *demoPath == "" || *outPath == "" || *chunksDir == "" {
		flag.Usage()
		os.Exit(1)
	}

	f, err := os.Open(*demoPath)
	if err != nil {
		log.Fatalf("open demo: %v", err)
	}
	defer f.Close()

	var demoSize int64
	if st, statErr := f.Stat(); statErr == nil {
		demoSize = st.Size()
	}
	counted := &countingReader{r: f}

	parser := dem.NewParser(counted)

	col := newCollector()
	col.register(parser)

	reportProgress("parse", 0)
	stopProgress := make(chan struct{})
	go trackProgress(counted, demoSize, stopProgress)

	if err := parser.ParseToEnd(); err != nil {
		log.Printf("parse warning (non-fatal): %v", err)
	}
	close(stopProgress)
	reportProgress("parse", 1)

	// Flush unfinished smokes with approximate end tick
	for id, s := range col.pendingSmokes {
		col.smokes = append(col.smokes, SmokeEvent{
			StartTick: s.startTick, EndTick: s.startTick + smokeDuration,
			X: s.x, Y: s.y, ThrowerName: s.thrower,
		})
		delete(col.pendingSmokes, id)
	}
	for id, s := range col.pendingInfernos {
		col.infernos = append(col.infernos, SmokeEvent{
			StartTick: s.startTick, EndTick: s.startTick + fireDuration,
			X: s.x, Y: s.y, ThrowerName: s.thrower,
			Weapon: s.weapon, Cells: s.cells,
		})
		delete(col.pendingInfernos, id)
	}

	// Fill in ranks from the final player entities. Covers players whose rank
	// is in the entity state even when no RankUpdate reveal fired, and supplies
	// RankType/name for any captured purely from the event.
	for _, pl := range parser.GameState().Participants().Playing() {
		if pl == nil || pl.SteamID64 == 0 {
			continue
		}
		r := col.ranks[pl.SteamID64]
		if r == nil {
			r = &PlayerRank{SteamID: strconv.FormatUint(pl.SteamID64, 10)}
			col.ranks[pl.SteamID64] = r
		}
		if r.Name == "" {
			r.Name = pl.Name
		}
		if r.RankType == 0 {
			r.RankType = pl.RankType()
		}
		if r.Rank == 0 {
			r.Rank = pl.Rank()
		}
		if r.Wins == 0 {
			r.Wins = pl.CompetitiveWins()
		}
	}

	// Before any chunk is written: the chunk index carries round numbers, so a
	// renumber afterwards would leave the index and rounds[] disagreeing.
	if n := col.trimPreMatchRounds(); n > 0 {
		log.Printf("dropped %d pre-match round(s) (knife round / warmup) before "+
			"the match start at frame %d", n, col.matchStartFrame)
	}

	reportProgress("chunks", 0)
	var chunkEntries []ChunkEntry
	if !*noJSONChunks {
		chunkEntries, err = writeChunks(col, *chunksDir)
		if err != nil {
			log.Fatalf("write chunks: %v", err)
		}
	}

	var chunkEntriesV2 []ChunkEntry
	if *chunksV2 {
		chunkEntriesV2, err = writeChunksV2(col, *chunksDir)
		if err != nil {
			log.Fatalf("write v2 chunks: %v", err)
		}
	}

	var rounds []Round
	for _, rs := range col.rounds {
		rounds = append(rounds, Round{
			RoundNum:  rs.num,
			Start:     rs.start,
			End:       rs.end,
			FreezeEnd: rs.freezeEnd,
			Winner:    rs.winner,
			Reason:    rs.reason,
			BombPlant: rs.bombPlant,
			BombSite:  rs.bombSite,
			Economy:   rs.economy,
		})
	}

	// Ensure slices serialise as [] not null
	nilSafe := func(s interface{}) interface{} { return s }
	_ = nilSafe
	if col.kills == nil {
		col.kills = []Kill{}
	}
	if col.damage == nil {
		col.damage = []Damage{}
	}
	if col.blind == nil {
		col.blind = []Blind{}
	}
	if col.shots == nil {
		col.shots = []Shot{}
	}
	if col.smokes == nil {
		col.smokes = []SmokeEvent{}
	}
	if col.infernos == nil {
		col.infernos = []SmokeEvent{}
	}
	if col.flashes == nil {
		col.flashes = []GrenadeDetonate{}
	}
	if col.he == nil {
		col.he = []GrenadeDetonate{}
	}
	if col.grenades == nil {
		col.grenades = []GrenadeTrajectory{}
	}
	if col.bomb == nil {
		col.bomb = []BombEvent{}
	}
	if rounds == nil {
		rounds = []Round{}
	}
	if chunkEntries == nil {
		chunkEntries = []ChunkEntry{}
	}

	chunkFormat := 0
	if len(chunkEntriesV2) > 0 {
		chunkFormat = 2
	}

	ranks := make([]PlayerRank, 0, len(col.ranks))
	for _, r := range col.ranks {
		ranks = append(ranks, *r)
	}
	sort.Slice(ranks, func(i, j int) bool {
		if ranks[i].RankType != ranks[j].RankType {
			return ranks[i].RankType > ranks[j].RankType
		}
		return ranks[i].Rank > ranks[j].Rank
	})

	mapName, mode := readDemoMeta(*demoPath)
	timestamp := extractTimestamp(*demoPath)

	out := MatchOutput{
		MapName: mapName, Mode: mode, Timestamp: timestamp,
		TickStep: tickStep, ChunkIndex: chunkEntries,
		ChunkFormat: chunkFormat, ChunkIndexV2: chunkEntriesV2,
		Rounds: rounds, Kills: col.kills, Damage: col.damage,
		Blind: col.blind, Shots: col.shots,
		Smokes: col.smokes, Infernos: col.infernos,
		Flashes: col.flashes, HE: col.he,
		Grenades: col.grenades, Bomb: col.bomb,
		Items:         col.items,
		Ranks:         ranks,
		FaceitMatchID: faceitMatchID(*demoPath),
	}

	reportProgress("encode", 0)
	outFile, err := os.Create(*outPath)
	if err != nil {
		log.Fatalf("create output: %v", err)
	}
	defer outFile.Close()

	if err := json.NewEncoder(outFile).Encode(out); err != nil {
		log.Fatalf("encode output: %v", err)
	}

	fmt.Printf("OK mapName=%s mode=%s timestamp=%s rounds=%d kills=%d chunks=%d chunksV2=%d ranks=%d\n",
		mapName, mode, timestamp, len(rounds), len(col.kills), len(chunkEntries), len(chunkEntriesV2), len(ranks))
}
