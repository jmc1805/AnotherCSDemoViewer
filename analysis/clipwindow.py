"""Shared clip tick-window sizing for Overwatch moments and player highlights.

Mirrors cmd/overwatch/main.go's OWWindow constants (windowBefore/windowAfter,
~lines 367-384) - that Go extractor already defines a kill-context window of
224 ticks before to 32 ticks after (3.5s pre-kill to 0.5s post-kill at 64Hz),
which happens to double as a natural in/out point for "the play in question".
Kept here as a single Python-side source of truth so overwatch.py and
highlights.py don't each hardcode the same numbers.
"""

KILL_CLIP_BEFORE_TICKS = 224
KILL_CLIP_AFTER_TICKS = 32


def clip_window_for_kill(tick, round_start=None):
    """Return (start_tick, end_tick) for a single-kill clip, clamped to the
    round's start tick when known (round_start=None skips clamping)."""
    start = tick - KILL_CLIP_BEFORE_TICKS
    if round_start is not None:
        start = max(start, round_start)
    end = tick + KILL_CLIP_AFTER_TICKS
    return start, end


def clip_window_for_kill_span(first_tick, last_tick, round_start=None, round_end=None):
    """Return (start_tick, end_tick) for a clip spanning multiple related
    kills (multi-kill rounds, clutch sequences) - same before/after buffer
    as a single kill, but anchored to the first and last kill in the span."""
    start = first_tick - KILL_CLIP_BEFORE_TICKS
    if round_start is not None:
        start = max(start, round_start)
    end = last_tick + KILL_CLIP_AFTER_TICKS
    if round_end is not None:
        end = min(end, round_end + KILL_CLIP_AFTER_TICKS)
    return start, end
