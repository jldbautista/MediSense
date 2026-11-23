"""Pulse rules: turn ~10 s of raw pulse sensor samples into a heart rate (BPM)
and a signal quality rating. Pure: no Supabase, no FastAPI, no numpy.

Steps: check the raw signal (finger present? clipped?) -> remove the slow
baseline drift -> smooth out fast noise -> find the heartbeat peaks ->
BPM from the average time between steady peaks -> rate how steady the beats are.
"""

from statistics import median

ADC_MAX = 4095            # ESP32 ADC is 12-bit (0-4095)
MIN_BPM = 40
MAX_BPM = 180
MIN_SECONDS = 6.0         # need at least this much signal
MIN_RATE_HZ = 20
MAX_RATE_HZ = 500
BASELINE_SECONDS = 1.0    # moving average window for the drift (subtracted)
SMOOTH_SECONDS = 0.1      # moving average window for the noise
MIN_AMPLITUDE = 20        # filtered peak-to-peak below this = no pulse (no finger)
CLIP_FRACTION = 0.10      # more than 10% of samples at 0 or ADC_MAX = clipped
MIN_INTERVALS = 4         # need at least 4 beat-to-beat intervals for a BPM
GOOD_VARIATION = 0.08     # beat timing variation for "good"
FAIR_VARIATION = 0.15     # beat timing variation for "fair"
GOOD_STEADY = 0.9         # share of steady intervals (within 25% of the median) for "good"
FAIR_STEADY = 0.7         # ... for "fair"
PEAK_MIN_GAP = 0.25       # s; first pass: peaks closer than this are one beat
GAP_OF_TYPICAL = 0.6      # second pass: peaks closer than 60% of the typical beat are one beat
PEAK_THRESHOLD = 0.5      # peaks must reach halfway from the median to the top
GOOD_PERIODICITY = 0.6    # signal must repeat itself one beat later (autocorrelation)
FAIR_PERIODICITY = 0.5

QUALITIES = ("good", "fair", "poor", "no_signal")


def moving_average(values: list[float], window: int) -> list[float]:
    """Centered moving average. Near the ends the window shrinks so the
    output has the same length as the input."""
    n = len(values)
    if window <= 1 or n == 0:
        return list(values)
    half = window // 2
    prefix = [0.0]
    for v in values:
        prefix.append(prefix[-1] + v)
    out = []
    for i in range(n):
        lo, hi = max(0, i - half), min(n, i + half + 1)
        out.append((prefix[hi] - prefix[lo]) / (hi - lo))
    return out


def filter_signal(samples: list[float], rate_hz: float) -> list[float]:
    """Band-pass the raw samples: subtract the ~1 s baseline (removes drift
    and the DC level), then smooth with a ~0.1 s average (removes noise)."""
    base = moving_average(samples, max(1, round(BASELINE_SECONDS * rate_hz)))
    detrended = [s - b for s, b in zip(samples, base)]
    return moving_average(detrended, max(1, round(SMOOTH_SECONDS * rate_hz)))


def percentile(values: list[float], p: float) -> float:
    """p-th percentile (0-100), linear between the nearest ranks."""
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def find_peaks(signal: list[float], rate_hz: float, min_gap_s: float = PEAK_MIN_GAP) -> list[int]:
    """Indexes of heartbeat peaks: local maxima above a threshold set from the
    signal's own size, at least `min_gap_s` apart. When two candidates are
    closer than that, the taller one wins (drops the smaller second bump of
    each beat, the dicrotic notch)."""
    if len(signal) < 3:
        return []
    mid = percentile(signal, 50)
    top = percentile(signal, 98)
    threshold = mid + PEAK_THRESHOLD * (top - mid)
    min_gap = rate_hz * min_gap_s

    peaks: list[int] = []
    for i in range(1, len(signal) - 1):
        v = signal[i]
        if v < threshold or not (v > signal[i - 1] and v >= signal[i + 1]):
            continue
        if peaks and i - peaks[-1] < min_gap:
            if v > signal[peaks[-1]]:
                peaks[-1] = i
            continue
        peaks.append(i)
    return peaks


def refine_peak(signal: list[float], i: int) -> float:
    """Peak position between samples: fit a parabola through the peak sample
    and its two neighbours. Makes BPM precise even at 50 Hz sampling."""
    if i <= 0 or i >= len(signal) - 1:
        return float(i)
    a, b, c = signal[i - 1], signal[i], signal[i + 1]
    denom = a - 2 * b + c
    return i + (0.5 * (a - c) / denom if denom else 0.0)


def periodicity(signal: list[float], lag: int) -> float:
    """How much the signal repeats itself `lag` samples later: normalized
    autocorrelation (1 = identical every beat, ~0 = random noise). Checks lags
    within 10% of `lag` and returns the best."""
    n = len(signal)
    mean = sum(signal) / n
    x = [v - mean for v in signal]
    energy = sum(v * v for v in x)
    if energy == 0 or lag <= 0:
        return 0.0
    best = 0.0
    for L in range(max(1, int(lag * 0.9)), min(n - 1, int(lag * 1.1) + 1) + 1):
        c = sum(x[i] * x[i + L] for i in range(n - L)) / energy * n / (n - L)
        best = max(best, c)
    return best


def analyze_pulse(samples: list[float], rate_hz: float, adc_max: int = ADC_MAX) -> dict:
    """Analyze one reading. Returns:
      bpm        int or None (None unless quality is good or fair)
      quality    "good" | "fair" | "poor" | "no_signal"
      reason     short explanation (shown on the dashboard)
      beats      number of peaks found
      variation  beat timing variation (MAD / median of intervals), or None
      periodicity  how much the signal repeats each beat (0-1), or None
      amplitude  filtered peak-to-peak size in ADC counts
      peaks      peak indexes (for drawing the waveform)
    Raises ValueError for a bad sample rate or too little data."""
    if not (MIN_RATE_HZ <= rate_hz <= MAX_RATE_HZ):
        raise ValueError(f"sample rate must be {MIN_RATE_HZ}-{MAX_RATE_HZ} Hz")
    seconds = len(samples) / rate_hz
    if seconds < MIN_SECONDS:
        raise ValueError(f"need at least {MIN_SECONDS:g} s of samples, got {seconds:.1f} s")

    result = {"bpm": None, "quality": "poor", "reason": "", "beats": 0,
              "variation": None, "periodicity": None, "amplitude": 0, "peaks": []}

    clipped = sum(1 for s in samples if s <= 2 or s >= adc_max - 2) / len(samples)
    filtered = filter_signal(samples, rate_hz)
    amplitude = percentile(filtered, 95) - percentile(filtered, 5)
    result["amplitude"] = round(amplitude)

    if amplitude < MIN_AMPLITUDE:
        result.update(quality="no_signal", reason="No pulse detected (is a finger on the sensor?)")
        return result
    if clipped > CLIP_FRACTION:
        result["reason"] = "Signal clipped (pressing too hard or sensor saturated)"
        return result

    # Two passes: a rough look gives the typical beat length, then peaks closer
    # than 60% of it are merged (stops noise splitting one beat into two).
    rough = find_peaks(filtered, rate_hz)
    rough_iv = [(b - a) / rate_hz for a, b in zip(rough, rough[1:])]
    if rough_iv:
        rough = find_peaks(filtered, rate_hz, max(PEAK_MIN_GAP, GAP_OF_TYPICAL * median(rough_iv)))
    peaks = rough
    result["peaks"] = peaks
    result["beats"] = len(peaks)

    times = [refine_peak(filtered, i) / rate_hz for i in peaks]
    intervals = [b - a for a, b in zip(times, times[1:])]
    if len(intervals) < MIN_INTERVALS:
        result["reason"] = "Not enough steady beats found"
        return result

    # "Steady" intervals are within 25% of the median; a missed or extra beat
    # makes an interval that isn't, and is left out of the BPM.
    typical = median(intervals)
    steady = [t for t in intervals if abs(t - typical) <= 0.25 * typical]
    if len(steady) < MIN_INTERVALS:
        result["reason"] = "Not enough steady beats found"
        return result

    variation = median(abs(t - typical) for t in intervals) / typical
    steady_share = len(steady) / len(intervals)
    repeat = periodicity(filtered, round(typical * rate_hz))
    bpm = 60 * len(steady) / sum(steady)
    result["variation"] = round(variation, 3)
    result["periodicity"] = round(repeat, 3)

    if not (MIN_BPM <= bpm <= MAX_BPM):
        result["reason"] = f"Rate outside {MIN_BPM}-{MAX_BPM} BPM (likely noise)"
        return result
    if (variation <= GOOD_VARIATION and steady_share >= GOOD_STEADY
            and repeat >= GOOD_PERIODICITY):
        quality, reason = "good", "Steady pulse"
    elif (variation <= FAIR_VARIATION and steady_share >= FAIR_STEADY
            and repeat >= FAIR_PERIODICITY):
        quality, reason = "fair", "Usable pulse, some irregular beats or noise"
    else:
        result["reason"] = "Beats too irregular (movement or noise)"
        return result

    result.update(bpm=round(bpm), quality=quality, reason=reason)
    return result