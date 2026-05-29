"""Pure scoring rules. No I/O — operates on values so it stays trivially testable.

Emotional model: a trigger adds `emotion_floor_step` to a per-memory floor
(capped at `emotion_bonus_max`) that decay can never erode, and bumps the live
score by the same step. The floor is the durable-synapse analogue from
Izquierdo's "A arte de esquecer": emotional memories settle at a permanent
baseline instead of fading to the global floor.
"""

from __future__ import annotations

import math

from sincron_brain.config import ScoreConfig


def apply_emotion_trigger(score: int, emotion_floor: int, cfg: ScoreConfig) -> tuple[int, int]:
    """Return (new_score, new_emotion_floor) after one emotional trigger."""
    new_floor = min(emotion_floor + cfg.emotion_floor_step, cfg.emotion_bonus_max)
    new_score = min(cfg.initial, score + cfg.emotion_floor_step)
    return max(new_score, new_floor), new_floor


def decayed_score(score: int, emotion_floor: int, days: float, cfg: ScoreConfig) -> int:
    """Apply temporal decay, floored at max(global floor, emotion floor)."""
    effective_floor = max(cfg.floor, emotion_floor)
    decayed = math.floor(score - cfg.decay_per_day * days)
    return max(effective_floor, decayed)
