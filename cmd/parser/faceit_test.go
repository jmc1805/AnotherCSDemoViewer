package main

import "testing"

// The header samples are the real server names from this repo's corpus - the
// only place a demo says where it was played.
func TestModeFromHeader(t *testing.T) {
	cases := []struct {
		name    string
		content string
		want    string
	}{
		{"faceit", `PBDEMS2 FACEIT.com register to play here"SourceTV Demo*de_dust22`, "faceit"},
		{"faceit lowercased", "faceit.com register to play here", "faceit"},
		{"valve premier", `Valve Counter-Strike 2 Premier eu_west Server`, "premier"},
		{"valve mm", `Valve Counter-Strike 2 eu_west Server (srcds423-fra2.274.180)`, ""},
		{"esl pro", `ESL Match Server #2`, ""},
		{"empty", "", ""},

		// Where the match was played beats what the name calls it.
		{"faceit wins over premier", "FACEIT premier league server", "faceit"},
	}
	for _, c := range cases {
		if got := modeFromHeader(c.content); got != c.want {
			t.Errorf("%s: modeFromHeader(%q) = %q, want %q", c.name, c.content, got, c.want)
		}
	}
}

func TestFaceitMatchID(t *testing.T) {
	cases := []struct {
		name string
		path string
		want string
	}{
		{"the reference demo",
			`C:\data\demos\1-8556a3f2-3a9e-4f07-84dc-9a2a99f60cb4-1-1.dem`,
			"1-8556a3f2-3a9e-4f07-84dc-9a2a99f60cb4"},
		{"posix path",
			"/data/demos/1-8556a3f2-3a9e-4f07-84dc-9a2a99f60cb4-1-1.dem",
			"1-8556a3f2-3a9e-4f07-84dc-9a2a99f60cb4"},
		{"second map of a bo3",
			"1-8556a3f2-3a9e-4f07-84dc-9a2a99f60cb4-2-1.dem",
			"1-8556a3f2-3a9e-4f07-84dc-9a2a99f60cb4"},
		{"uppercase hex is folded, since the room URL is lowercase",
			"1-8556A3F2-3A9E-4F07-84DC-9A2A99F60CB4-1-1.dem",
			"1-8556a3f2-3a9e-4f07-84dc-9a2a99f60cb4"},

		// No id rather than a wrong one: the frontend renders "" as no link.
		{"a Valve match", "match730_003825870392405262944_1533378074_274.dem", ""},
		{"renamed by hand", "faceit dust2 good game.dem", ""},
		{"truncated uuid", "1-8556a3f2-3a9e-4f07-84dc-1-1.dem", ""},
		{"not hex", "1-zzzzzzzz-3a9e-4f07-84dc-9a2a99f60cb4-1-1.dem", ""},
		{"empty", "", ""},
	}
	for _, c := range cases {
		if got := faceitMatchID(c.path); got != c.want {
			t.Errorf("%s: faceitMatchID(%q) = %q, want %q", c.name, c.path, got, c.want)
		}
	}
}

// The trim is what turns FACEIT's knife round + warmup into a match that
// starts at round 1. Built as a bare collector rather than through a parse, so
// the numbering rule is pinned without a .dem.
func TestTrimPreMatchRounds(t *testing.T) {
	// Frames mirror the reference demo: knife at 15, warmup at 3169, the
	// match at 3566.
	newCol := func(matchStart int) *collector {
		c := &collector{
			matchStartFrame: matchStart,
			rounds: []*roundState{
				{num: 1, start: 15}, {num: 2, start: 3169},
				{num: 3, start: 3566}, {num: 4, start: 8229},
			},
			roundN: 4,
			kills: []Kill{
				{Tick: 1300, RoundNum: 1}, {Tick: 3000, RoundNum: 1},
				{Tick: 5741, RoundNum: 3}, {Tick: 9000, RoundNum: 4},
			},
			damage:   []Damage{{Tick: 1290, RoundNum: 1}, {Tick: 5740, RoundNum: 3}},
			shots:    []Shot{{Tick: 1200}, {Tick: 5314}},
			smokes:   []SmokeEvent{{StartTick: 900}, {StartTick: 10088}},
			tickData: map[int][]PlayerTick{100: {{RoundNum: 1}}, 5000: {{RoundNum: 3}}},
		}
		return c
	}

	c := newCol(3566)
	if got := c.trimPreMatchRounds(); got != 2 {
		t.Fatalf("dropped = %d, want 2", got)
	}
	if len(c.rounds) != 2 || c.rounds[0].num != 1 || c.rounds[1].num != 2 {
		t.Errorf("rounds = %d numbered %v, want 2 numbered 1,2",
			len(c.rounds), []int{c.rounds[0].num, c.rounds[len(c.rounds)-1].num})
	}
	if c.rounds[0].start != 3566 {
		t.Errorf("round 1 starts at %d, want the match start 3566", c.rounds[0].start)
	}
	if c.roundN != 2 {
		t.Errorf("roundN = %d, want 2", c.roundN)
	}
	// Knife-round kills are gone, and what remains counts from round 1.
	if len(c.kills) != 2 || c.kills[0].RoundNum != 1 || c.kills[1].RoundNum != 2 {
		t.Errorf("kills = %+v, want the two match kills renumbered 1,2", c.kills)
	}
	if len(c.damage) != 1 || c.damage[0].RoundNum != 1 {
		t.Errorf("damage = %+v, want one row in round 1", c.damage)
	}
	// Rows with no round number are dropped by tick alone.
	if len(c.shots) != 1 || c.shots[0].Tick != 5314 {
		t.Errorf("shots = %+v, want only the in-match shot", c.shots)
	}
	if len(c.smokes) != 1 || c.smokes[0].StartTick != 10088 {
		t.Errorf("smokes = %+v, want only the in-match smoke", c.smokes)
	}
	// Tick data too, or the knife round would still be in the chunk files.
	if _, ok := c.tickData[100]; ok {
		t.Error("pre-match tick data survived the trim")
	}
	if pt := c.tickData[5000]; len(pt) != 1 || pt[0].RoundNum != 1 {
		t.Errorf("tickData[5000] = %+v, want round 1", pt)
	}

	// A demo that never fires MatchStart (ESL/pro) keeps everything.
	c = newCol(0)
	if got := c.trimPreMatchRounds(); got != 0 {
		t.Errorf("no match start: dropped = %d, want 0", got)
	}
	if len(c.rounds) != 4 || len(c.kills) != 4 {
		t.Errorf("no match start: %d rounds / %d kills, want 4/4", len(c.rounds), len(c.kills))
	}

	// A Valve MM demo fires MatchStart on the first round's own frame.
	c = newCol(15)
	if got := c.trimPreMatchRounds(); got != 0 {
		t.Errorf("match start on round 1: dropped = %d, want 0", got)
	}
	if len(c.rounds) != 4 || c.rounds[0].num != 1 {
		t.Errorf("match start on round 1: rounds changed (%d)", len(c.rounds))
	}
}
