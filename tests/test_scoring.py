"""Behavior of the cognitive scoring rules.

The emotional model (decided with the human partner, grounded in Izquierdo's
"A arte de esquecer"): feedback/correction about the AI raises a per-memory
floor using a decreasing impact table. Positive and negative feedback have the
same priority; narrated emotion is content, not reinforcement.
"""

from sincron_brain import scoring
from sincron_brain.config import ScoreConfig


def cfg(**overrides) -> ScoreConfig:
    return ScoreConfig(**overrides)


def test_emotion_trigger_raises_floor_by_step():
    _, floor = scoring.apply_emotion_trigger(score=100, emotion_floor=0, cfg=cfg())
    assert floor == 40


def test_emotion_trigger_uses_decreasing_impact_table():
    score, floor = 100, 0
    score, floor = scoring.apply_emotion_trigger(score, floor, cfg())
    score, floor = scoring.apply_emotion_trigger(score, floor, cfg())
    score, floor = scoring.apply_emotion_trigger(score, floor, cfg())
    assert floor == 70


def test_emotion_floor_caps_at_emotion_bonus_max():
    score, floor = 100, 0
    for _ in range(10):
        score, floor = scoring.apply_emotion_trigger(score, floor, cfg())
    assert floor == 80


def test_emotion_trigger_bumps_score_but_clamps_at_initial():
    score, _ = scoring.apply_emotion_trigger(score=100, emotion_floor=0, cfg=cfg())
    assert score == 100


def test_emotion_trigger_lifts_a_decayed_memory():
    score, floor = scoring.apply_emotion_trigger(score=2, emotion_floor=0, cfg=cfg())
    assert score == 42
    assert floor == 40


def test_decay_reduces_score_over_days():
    new = scoring.decayed_score(score=100, emotion_floor=0, days=2.0, cfg=cfg())
    assert new == 97  # 100 - 1.5*2


def test_decay_never_drops_below_emotion_floor():
    new = scoring.decayed_score(score=30, emotion_floor=30, days=1000.0, cfg=cfg())
    assert new == 30


def test_decay_respects_global_floor_when_no_emotion():
    new = scoring.decayed_score(score=50, emotion_floor=0, days=1000.0, cfg=cfg())
    assert new == 1  # ScoreConfig.floor default


def test_decay_effective_floor_is_max_of_global_and_emotion():
    new = scoring.decayed_score(score=5, emotion_floor=20, days=1000.0, cfg=cfg())
    assert new == 20
