"""Unit tests for app/pulse_rules.py (pure logic: no Supabase, no FastAPI).

The pulse sensor isn't wired yet, so these use synthetic signals shaped like
a real fingertip pulse: a sharp rise, a slower fall, and a small second bump
(the dicrotic notch), sitting on a slowly drifting baseline, with noise.
"""

import math
import random

import pytest

from app.pulse_rules import (
    analyze_pulse,
    filter_signal,
    find_peaks,
    moving_average,
    percentile,
    periodicity,
    refine_peak,
)

RATE = 50  # Hz, what the ESP32 will sample at


def pulse_shape(phase: float) -> float:
    """One heartbeat, phase 0..1: main peak + smaller dicrotic bump."""
    main = math.exp(-((phase - 0.15) / 0.06) ** 2)
    notch = 0.35 * math.exp(-((phase - 0.45) / 0.07) ** 2)
    return main + notch


def make_signal(bpm=72.0, seconds=10.0, rate=RATE, amplitude=300, center=2000,
                noise=0.0, drift=0.0, seed=490, skip_beats=(), jitter=0.0,
                adc_max=4095):
    """Raw ADC samples (ints, clamped to 0..adc_max) for a synthetic pulse."""
    rng = random.Random(seed)
    n = int(seconds * rate)
    beat_times, t, k = [], 0.0, 0
    while t < seconds + 2:
        beat_times.append((k, t))
        period = 60 / bpm
        t += period * (1 + rng.uniform(-jitter, jitter))
        k += 1
    out = []
    for i in range(n):
        t = i / rate
        v = 0.0
        for k, start in beat_times:
            if k in skip_beats:
                continue
            phase = (t - start) / (60 / bpm)
            if 0 <= phase < 1:
                v += pulse_shape(phase)
        s = center + amplitude * v
        s += drift * math.sin(2 * math.pi * 0.15 * t)      # breathing / slow drift
        s += rng.gauss(0, noise) if noise else 0
        out.append(max(0, min(adc_max, round(s))))
    return out


# ---- helpers ----

def test_moving_average_keeps_length_and_flattens_constant():
    assert moving_average([5, 5, 5, 5, 5], 3) == [5, 5, 5, 5, 5]
    assert len(moving_average(list(range(10)), 4)) == 10


def test_moving_average_window_one_is_identity():
    assert moving_average([1, 2, 3], 1) == [1, 2, 3]


def test_percentile_basic():
    assert percentile([1, 2, 3, 4, 5], 50) == 3
    assert percentile([1, 2, 3, 4, 5], 0) == 1
    assert percentile([1, 2, 3, 4, 5], 100) == 5


def test_filter_removes_dc_level():
    flat = [2000] * 500
    assert max(abs(v) for v in filter_signal(flat, RATE)) < 1e-9


def test_find_peaks_respects_min_gap():
    # Two spikes 0.2 s apart (= 300 BPM, impossible): only the taller one counts.
    sig = [0.0] * 200
    sig[50], sig[60] = 10.0, 12.0
    sig[150] = 10.0
    assert find_peaks(sig, RATE) == [60, 150]


def test_refine_peak_finds_true_peak_between_samples():
    # Samples of y = -(x - 10.3)^2: the true peak is at 10.3.
    sig = [-(x - 10.3) ** 2 for x in range(20)]
    assert abs(refine_peak(sig, 10) - 10.3) < 1e-9


def test_periodicity_high_for_repeating_low_for_noise():
    wave = [math.sin(2 * math.pi * i / 40) for i in range(400)]
    assert periodicity(wave, 40) > 0.9
    rng = random.Random(3)
    noise = [rng.gauss(0, 1) for _ in range(400)]
    assert periodicity(noise, 40) < 0.3


# ---- BPM accuracy ----

@pytest.mark.parametrize("bpm", [45, 60, 72, 90, 120, 150, 175])
def test_clean_signal_bpm_within_2(bpm):
    r = analyze_pulse(make_signal(bpm=bpm), RATE)
    assert r["quality"] == "good"
    assert abs(r["bpm"] - bpm) <= 2


def test_fast_pulse_with_noise_is_precise():
    # At 50 Hz a 170 BPM beat is only ~17.6 samples long; sub sample peak
    # positions keep the BPM exact instead of jumping between 158/167/176.
    r = analyze_pulse(make_signal(bpm=170, noise=30), RATE)
    assert r["quality"] == "good"
    assert abs(r["bpm"] - 170) <= 2


def test_dicrotic_notch_not_counted_as_extra_beats():
    # 72 BPM over 10 s is ~12 beats; double counting would give ~24.
    r = analyze_pulse(make_signal(bpm=72), RATE)
    assert 11 <= r["beats"] <= 13


@pytest.mark.parametrize("rate", [25, 100, 200])
def test_other_sample_rates(rate):
    r = analyze_pulse(make_signal(bpm=80, rate=rate), rate)
    assert r["quality"] == "good"
    assert abs(r["bpm"] - 80) <= 2


def test_baseline_drift_is_removed():
    # Drift twice as big as the pulse itself (finger pressure, breathing).
    r = analyze_pulse(make_signal(bpm=75, drift=600), RATE)
    assert r["quality"] in ("good", "fair")
    assert abs(r["bpm"] - 75) <= 3


def test_moderate_noise_still_reads():
    r = analyze_pulse(make_signal(bpm=68, noise=40), RATE)
    assert r["quality"] in ("good", "fair")
    assert abs(r["bpm"] - 68) <= 3


def test_small_but_real_pulse():
    r = analyze_pulse(make_signal(bpm=70, amplitude=60, noise=5), RATE)
    assert r["quality"] in ("good", "fair")
    assert abs(r["bpm"] - 70) <= 3


def test_normal_heart_rate_variability_is_good():
    # +/-4% beat-to-beat changes, like a resting person breathing.
    r = analyze_pulse(make_signal(bpm=66, jitter=0.04), RATE)
    assert r["quality"] == "good"
    assert abs(r["bpm"] - 66) <= 3


def test_one_missed_beat_does_not_change_bpm():
    # A dropped beat makes one long interval; the median ignores it.
    r = analyze_pulse(make_signal(bpm=72, skip_beats=(5,)), RATE)
    assert r["quality"] in ("good", "fair")
    assert abs(r["bpm"] - 72) <= 2


def test_longer_reading_works():
    r = analyze_pulse(make_signal(bpm=72, seconds=30), RATE)
    assert r["quality"] == "good"
    assert abs(r["bpm"] - 72) <= 2


# ---- bad signals ----

def test_flat_line_is_no_signal():
    r = analyze_pulse([2048] * 500, RATE)
    assert r["quality"] == "no_signal"
    assert r["bpm"] is None


def test_tiny_noise_without_finger_is_no_signal():
    rng = random.Random(1)
    r = analyze_pulse([2048 + round(rng.gauss(0, 3)) for _ in range(500)], RATE)
    assert r["quality"] == "no_signal"
    assert r["bpm"] is None


def test_pure_noise_gives_no_bpm():
    rng = random.Random(2)
    r = analyze_pulse([2048 + round(rng.gauss(0, 200)) for _ in range(500)], RATE)
    assert r["quality"] == "poor"
    assert r["bpm"] is None


@pytest.mark.parametrize("seed", range(30))
def test_noise_never_produces_a_bpm(seed):
    rng = random.Random(seed)
    r = analyze_pulse([2048 + round(rng.gauss(0, 200)) for _ in range(500)], RATE)
    assert r["bpm"] is None


def test_wandering_noise_gives_no_bpm():
    # Finger sliding around: random walk plus noise, no heartbeat.
    rng = random.Random(4)
    w, sig = 0.0, []
    for _ in range(500):
        w += rng.gauss(0, 30)
        sig.append(max(0, min(4095, 2048 + round(w + rng.gauss(0, 50)))))
    assert analyze_pulse(sig, RATE)["bpm"] is None


def test_very_irregular_beats_are_poor():
    r = analyze_pulse(make_signal(bpm=70, jitter=0.45, seed=7), RATE)
    assert r["quality"] == "poor"
    assert r["bpm"] is None


def test_clipped_signal_is_poor():
    # Pressing hard: the signal sits at the top of the ADC range.
    r = analyze_pulse(make_signal(bpm=72, center=3950, amplitude=400), RATE)
    assert r["quality"] == "poor"
    assert r["bpm"] is None
    assert "clipped" in r["reason"].lower()


def test_result_has_all_fields():
    r = analyze_pulse(make_signal(), RATE)
    assert set(r) == {"bpm", "quality", "reason", "beats", "variation",
                      "periodicity", "amplitude", "peaks"}
    assert r["amplitude"] > 0
    assert len(r["peaks"]) == r["beats"]


# ---- bad input ----

def test_too_short_raises():
    with pytest.raises(ValueError):
        analyze_pulse(make_signal(seconds=3), RATE)


@pytest.mark.parametrize("rate", [5, 1000])
def test_bad_sample_rate_raises(rate):
    with pytest.raises(ValueError):
        analyze_pulse([2000] * 10000, rate)
    