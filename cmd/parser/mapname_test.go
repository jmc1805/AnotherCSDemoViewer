package main

import "testing"

// Header samples are the shape a real CS2 PBDEMS2 header has around the map
// name: a protobuf field tag, a one-byte length, the name, then the next
// field's tag - which for these demos is 0x32, the printable character '2'.
func TestParseMapName(t *testing.T) {
	cases := []struct {
		name    string
		content string
		want    string
	}{
		// The case this function exists for: the framing byte after the name
		// is '2', so a naive scan reads "de_dust22" and a digit-trim would
		// turn Dust II into Dust.
		{"dust2 followed by framing digit",
			"SourceTV Demo*\x08de_dust22!/opt/srcds/cs2/csgo", "de_dust2"},
		{"mirage followed by framing digit",
			"SourceTV Demo*\x09de_mirage2!/opt/srcds/cs2/csgo", "de_mirage"},
		{"ancient followed by framing digit",
			"SourceTV Demo*\x0ade_ancient2!/opt/srcds", "de_ancient"},
		{"name ends at a non-word byte, no framing digit",
			"SourceTV Demo*\x09de_inferno\x00rest", "de_inferno"},
		{"cs_ prefix", "hdr*\x09cs_office2!x", "cs_office"},
		{"ar_ prefix", "hdr*\x0aar_baggage2!x", "ar_baggage"},

		// No usable length prefix (older CS:GO headers): fall back to
		// trimming trailing digits, sparing maps that really end in one.
		{"no length prefix, plain name", "map de_inferno ", "de_inferno"},
		{"no length prefix, dust2 kept whole", "map de_dust2 ", "de_dust2"},
		{"no length prefix, version digits trimmed", "map de_cbble2 ", "de_cbble"},

		// A preceding byte that merely looks like a length must not cut a
		// legitimate name in half.
		{"implausible length byte is ignored",
			"x\x05de_mirage\x00", "de_mirage"},
		{"length byte pointing mid-word is ignored",
			"x\x07de_inferno\x00", "de_inferno"},

		{"nothing map-shaped", "SourceTV Demo no map here", ""},
		{"bare prefix only", "x\x03de_\x00", ""},
	}

	for _, c := range cases {
		if got := parseMapName(c.content); got != c.want {
			t.Errorf("%s: parseMapName(%q) = %q, want %q", c.name, c.content, got, c.want)
		}
	}
}

// The real header bytes taken from a Dust II demo in the corpus, verbatim.
func TestParseMapNameRealHeader(t *testing.T) {
	const header = "srcds1026-lhr1.390.195)\"\rSourceTV Demo*\x08de_dust22!" +
		"/opt/srcds/cs2/csgo_v2000837/csgo8\x02@\x01H\x01R\x00Z\x0cvalve_d"
	if got := parseMapName(header); got != "de_dust2" {
		t.Errorf("parseMapName(real dust2 header) = %q, want de_dust2", got)
	}
}
