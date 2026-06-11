"""Pure scoring rules. No I/O — operates on values so it stays trivially testable.

Emotional model: a feedback/correction trigger raises a per-memory floor with a
decreasing impact table. Positive and negative feedback are both priority
signals: the floor is the durable-synapse analogue from Izquierdo's "A arte de
esquecer", so memories the user reacted to settle at a permanent baseline
instead of fading to the global floor.
"""

from __future__ import annotations

import math

from sincron_brain.config import ScoreConfig


def apply_emotion_trigger(score: int, emotion_floor: int, cfg: ScoreConfig) -> tuple[int, int]:
    """Return (new_score, new_emotion_floor) after one emotional trigger."""
    increment = _next_emotion_increment(emotion_floor, cfg)
    new_floor = min(emotion_floor + increment, cfg.emotion_bonus_max)
    new_score = min(cfg.initial, score + increment)
    return max(new_score, new_floor), new_floor


def _next_emotion_increment(emotion_floor: int, cfg: ScoreConfig) -> int:
    accumulated = 0
    for increment in cfg.emotion_floor_increments:
        accumulated += increment
        if emotion_floor < accumulated:
            return increment
    return cfg.emotion_floor_increments[-1] if cfg.emotion_floor_increments else 0


def decayed_score(score: int, emotion_floor: int, days: float, cfg: ScoreConfig) -> int:
    """Apply temporal decay, floored at max(global floor, emotion floor)."""
    effective_floor = max(cfg.floor, emotion_floor)
    decayed = math.floor(score - cfg.decay_per_day * days)
    return max(effective_floor, decayed)
