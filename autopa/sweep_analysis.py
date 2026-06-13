# autopa - "Sweep" calibration analysis (numpy-only)
#
# Copyright (C) 2026  autopa contributors
# Algorithm derived from PrusaPATuner by CNCKitchen (AGPL-3.0,
# src/prusa_pa_tuner/analysis.py): https://github.com/CNCKitchen/PrusaPATuner
# "Sweep" is autopa's name for this approach.
#
# This file may be distributed under the terms of the GNU AGPLv3 (or later)
# license.
#
# Sweep's ONE estimator is the bd_pressure step-response cost (PrusaPATuner's
# intended primary). This module is a numpy-only port of just that path:
#   1. numpy-only: the printer's klippy-env has numpy but NO scipy.
#   2. output-identical: the bd functions here match the scipy original
#      numerically, proven by an offline np.allclose A/B harness (run under a
#      scipy venv against the original).
# The bd functions were already pure numpy in the original, so they're copied
# VERBATIM; the only non-bd helpers kept are the ones the orchestrator needs
# (_resample_uniform, _square_wave_at_transitions for the UI overlay,
# _merge_transitions, _argmin_with_parabolic).
#
# REMOVED from the port (alternative calibrators we don't use -- see
# analyse_sweep_segments): the phase-lag cross-correlation (_phase_lag_ms,
# _detrend, _parabolic_peak), both integral-area forms (_integral_area*), the
# linear-fit zero-crossing (_linear_fit_zero_crossing, FitResult), and
# _build_command_wave. Their equivalence checks were dropped from
# sweep_port_equiv.py too.
#
# NEVER PORTED (Buddy clock-recovery; irrelevant on Klipper, which supplies
# exact print-time segment boundaries via lookahead callbacks): the anchor /
# slice / detect cascade of the reference's analyse_sweep. The Klipper-native
# orchestrator (analyse_sweep_segments) builds the per-K windows/transitions
# directly, so the bd output is unchanged for a given (force, boundaries) input.
from dataclasses import dataclass, field

import numpy as np


# ============================ result dataclasses ============================
# Copied verbatim from analysis.py (docstrings trimmed; see the reference for
# the full field-by-field rationale).

@dataclass
class KResult:
    k: float
    n_samples: int
    force_mean: float
    force_std: float
    coverage: float = 1.0
    dropouts: int = 0


@dataclass
class KWindow:
    k: float
    t: list
    force: list
    command: list
    ground_truth_force: list = field(default_factory=list)
    dropout_t: list = field(default_factory=list)


@dataclass
class Baseline:
    mean: float
    std: float
    drift: float
    n_samples: int
    t_start: float
    t_end: float


@dataclass
class ForceBaselines:
    slow_plateau: float
    fast_plateau: float
    n_slow: int
    n_fast: int


BD_METRIC_NAMES = (
    "baseline_median",
    "baseline_noise_std",
    "rise_delay",
    "rise_error_area",
    "rise_slope",
    "overshoot",
    "high_level",
    "plateau_slope",
    "plateau_creep",
    "fall_delay",
    "fall_error_area",
    "undershoot",
    "tail_area",
    "settling_time",
)

BD_DEFAULT_WEIGHTS = {
    "rise_error_area": 1.0,
    "overshoot": 2.0,
    "undershoot": 2.0,
    "tail_area": 1.0,
    "plateau_slope": 0.5,
    "rise_delay": 1.0,
    "fall_delay": 1.0,
    "settling_time": 0.5,
}


@dataclass
class BdSegment:
    k: float
    seg_idx: int
    t_start: float
    t_rise: float
    t_fall: float
    t_end: float
    t_lo_display: float = 0.0
    t_hi_display: float = 0.0
    t_rise_end: float = None
    t_fall_start: float = None
    t_fall_end: float = None
    t_peak: float = None
    t_trough: float = None
    n_samples: int = 0
    metrics: dict = field(default_factory=dict)
    excluded: bool = False
    exclusion_reasons: list = field(default_factory=list)


@dataclass
class BdKResult:
    k: float
    n_segments_total: int
    n_segments_included: int
    medians: dict = field(default_factory=dict)
    normalised: dict = field(default_factory=dict)
    mads: dict = field(default_factory=dict)
    iqrs: dict = field(default_factory=dict)


@dataclass
class SweepAnalysis:
    per_k: list
    sample_rate_hz: float
    baseline: Baseline = None
    notes: list = field(default_factory=list)
    windows: list = field(default_factory=list)
    force_baselines: ForceBaselines = None
    bd_segments: list = field(default_factory=list)
    bd_per_k: list = field(default_factory=list)
    bd_k_opt: float = None
    bd_default_weights: dict = field(
        default_factory=lambda: dict(BD_DEFAULT_WEIGHTS))


# ============================ numpy primitives ==============================

def _resample_uniform(t, y, dt):
    """Linear-interpolate (t, y) onto a uniform grid with spacing dt."""
    if len(t) < 2:
        return np.array([]), np.array([])
    t0 = t[0]
    t1 = t[-1]
    n = max(2, int(np.floor((t1 - t0) / dt)) + 1)
    grid = t0 + np.arange(n) * dt
    return grid, np.interp(grid, t, y)


# ============================ command wave ==================================

def _square_wave_at_transitions(t_grid, transitions_t, transitions_dirs,
                               low_val, high_val):
    """Piecewise-constant square wave on `t_grid` with edges at the supplied
    transition timestamps. +1 dir = rise (low->high), -1 = fall. Verbatim."""
    out = np.empty_like(t_grid, dtype=float)
    if len(transitions_t) == 0:
        out[:] = low_val
        return out
    initial = low_val if float(transitions_dirs[0]) > 0 else high_val
    out[:] = initial
    for t_x, d in zip(transitions_t, transitions_dirs):
        mask = t_grid >= float(t_x)
        out[mask] = high_val if float(d) > 0 else low_val
    return out


# ============================ bd-segment metrics ============================

def _nan_metrics():
    return {name: float("nan") for name in BD_METRIC_NAMES}


def _bd_segment_metrics(force_t, force_y, k, seg_idx, t_start, t_rise, t_fall,
                       t_end, slow_half_s, fast_half_s, dropout_t):
    """Compute the 14 region metrics for one low-high-low step segment.
    Verbatim from the reference (the docstring there documents R1..R8 and the
    auto-exclusion gates)."""
    metrics = _nan_metrics()
    excluded = False
    reasons = []

    display_margin = 0.10 * slow_half_s
    t_lo_display = t_start + display_margin
    t_hi_display = t_end - display_margin

    mask = (force_t >= t_start) & (force_t <= t_end)
    t = force_t[mask]
    y = force_y[mask]
    n = int(len(t))

    if n < 20:
        excluded = True
        reasons.append(f"only {n} samples in segment window")
        return BdSegment(
            k=k, seg_idx=seg_idx, t_start=t_start, t_rise=t_rise,
            t_fall=t_fall, t_end=t_end,
            t_lo_display=t_lo_display, t_hi_display=t_hi_display,
            t_rise_end=None, t_fall_start=None, t_fall_end=None,
            t_peak=None, t_trough=None,
            n_samples=n, metrics=metrics, excluded=excluded,
            exclusion_reasons=reasons,
        )

    if len(dropout_t):
        in_seg = (dropout_t >= t_start) & (dropout_t <= t_end)
        if in_seg.any():
            crit_lo = t_rise - 0.1 * slow_half_s
            crit_hi = t_fall + 0.5 * slow_half_s
            crit_mask = in_seg & (dropout_t >= crit_lo) & (dropout_t <= crit_hi)
            if crit_mask.any():
                t_first = float(dropout_t[crit_mask][0])
                reasons.append(
                    f"dropout at t={t_first - t_start:.2f}s (segment-rel) "
                    f"in critical region")
                excluded = True

    seg_duration = max(t_end - t_start, 1e-9)
    seg_rate_hz = n / seg_duration
    if seg_rate_hz < 40.0:
        reasons.append(f"low sample rate {seg_rate_hz:.0f}Hz (expected >=40Hz)")
        excluded = True

    t0_r1 = t_start + 0.15 * slow_half_s
    t1_r1 = t_rise - 0.05 * slow_half_s
    r1_mask = (t >= t0_r1) & (t <= t1_r1)
    if int(r1_mask.sum()) < 4:
        reasons.append("baseline window too narrow")
        excluded = True
        return BdSegment(
            k=k, seg_idx=seg_idx, t_start=t_start, t_rise=t_rise,
            t_fall=t_fall, t_end=t_end,
            t_lo_display=t_lo_display, t_hi_display=t_hi_display,
            t_rise_end=None, t_fall_start=None, t_fall_end=None,
            t_peak=None, t_trough=None,
            n_samples=n, metrics=metrics, excluded=excluded,
            exclusion_reasons=reasons,
        )
    baseline_y = y[r1_mask]
    baseline_median = float(np.median(baseline_y))
    baseline_noise_std = float(np.std(baseline_y))
    metrics["baseline_median"] = baseline_median
    metrics["baseline_noise_std"] = baseline_noise_std

    y_tared = y - baseline_median

    coarse_high_lo = t_rise + 0.5 * fast_half_s
    coarse_high_hi = t_fall - 0.05 * fast_half_s
    coarse_high_mask = (t >= coarse_high_lo) & (t <= coarse_high_hi)
    coarse_high_tared = (
        float(np.median(y_tared[coarse_high_mask]))
        if int(coarse_high_mask.sum()) >= 3 else float("nan"))

    RISE_COMPLETION_FRAC = 0.90
    FALL_COMPLETION_FRAC = 0.10
    FALL_SUSTAIN_S = 0.05
    t_rise_end = None
    t_fall_start = None
    t_fall_end = None
    if np.isfinite(coarse_high_tared) and coarse_high_tared > 0:
        rise_thr = RISE_COMPLETION_FRAC * coarse_high_tared
        fall_thr = FALL_COMPLETION_FRAC * coarse_high_tared
        rise_window = np.where(
            (t >= t_rise) & (t <= t_rise + 0.5 * fast_half_s))[0]
        for idx in rise_window:
            if y_tared[idx] >= rise_thr:
                t_rise_end = float(t[idx])
                break
        fall_start_search_lo = max(
            t_rise_end + 0.05 if t_rise_end is not None
            else t_rise + 0.3 * fast_half_s,
            t_fall - 0.3 * fast_half_s)
        fall_start_search_hi = t_fall + 0.1 * fast_half_s
        fall_start_window = np.where(
            (t >= fall_start_search_lo) & (t <= fall_start_search_hi))[0]
        for i_idx, idx in enumerate(fall_start_window):
            if y_tared[idx] >= rise_thr:
                continue
            sustain_end_t = float(t[idx]) + FALL_SUSTAIN_S
            future_mask = (t > t[idx]) & (t <= sustain_end_t)
            future_y = y_tared[future_mask]
            if len(future_y) == 0 or not np.any(future_y >= rise_thr):
                t_fall_start = float(t[idx])
                break
        fall_end_anchor = t_fall_start if t_fall_start is not None else t_fall
        fall_window = np.where(
            (t >= fall_end_anchor) & (t <= fall_end_anchor + 0.5 * slow_half_s))[0]
        for idx in fall_window:
            if y_tared[idx] <= fall_thr:
                t_fall_end = float(t[idx])
                break

    peak_search_lo = t_rise
    peak_search_hi = (
        (t_rise_end + 0.10 * fast_half_s) if t_rise_end is not None
        else (t_rise + 0.20 * fast_half_s))
    peak_mask = (t >= peak_search_lo) & (t <= peak_search_hi)
    t_peak = None
    peak_idx_global = None
    if int(peak_mask.sum()) >= 2:
        local = np.where(peak_mask)[0]
        peak_local_off = int(np.argmax(y_tared[local]))
        peak_idx_global = int(local[peak_local_off])
        t_peak = float(t[peak_idx_global])

    plateau_settle_margin = max(0.030, 0.10 * fast_half_s)
    plateau_pre_fall_margin = max(0.020, 0.10 * fast_half_s)
    if t_rise_end is not None:
        plateau_t_lo = t_rise_end + plateau_settle_margin
    elif t_peak is not None:
        plateau_t_lo = max(t_peak, t_rise) + plateau_settle_margin
    else:
        plateau_t_lo = t_rise + 0.5 * fast_half_s
    if t_fall_start is not None:
        plateau_t_hi = t_fall_start - plateau_pre_fall_margin
    else:
        plateau_t_hi = t_fall - plateau_pre_fall_margin
    plateau_mask = (t >= plateau_t_lo) & (t <= plateau_t_hi)
    n_plat = int(plateau_mask.sum())
    high_level_tared = float("nan")
    if n_plat >= 3:
        high_level_tared = float(np.median(y_tared[plateau_mask]))
        metrics["high_level"] = high_level_tared + baseline_median
        plateau_t = t[plateau_mask]
        plateau_y = y_tared[plateau_mask]
        if n_plat >= 4 and (plateau_t[-1] - plateau_t[0]) > 1e-6:
            slope, _intercept = np.polyfit(plateau_t, plateau_y, 1)
            metrics["plateau_slope"] = float(slope)
            metrics["plateau_creep"] = float(
                abs(slope) * (plateau_t[-1] - plateau_t[0]))

    if t_peak is not None and np.isfinite(high_level_tared):
        peak_value = float(y_tared[peak_idx_global])
        plateau_noise_std = baseline_noise_std
        if n_plat >= 6:
            plateau_residual = y_tared[plateau_mask] - high_level_tared
            plateau_noise_std = float(np.std(plateau_residual))
        noise_floor = 2.0 * max(plateau_noise_std, baseline_noise_std)
        metrics["overshoot"] = max(
            0.0, peak_value - high_level_tared - noise_floor)

    if t_peak is not None:
        metrics["rise_delay"] = max(0.0, float(t_peak) - t_rise)
        denom = max(float(t_peak) - t_rise, 1e-9)
        peak_value = float(y_tared[peak_idx_global])
        metrics["rise_slope"] = peak_value / denom
    if np.isfinite(high_level_tared):
        rise_window_hi = t_fall_start if t_fall_start is not None else t_fall
        rise_window_mask = (t >= t_rise) & (t <= rise_window_hi)
        if int(rise_window_mask.sum()) >= 2:
            rw_t = t[rise_window_mask]
            rw_y = y_tared[rise_window_mask]
            err = np.abs(high_level_tared - rw_y)
            metrics["rise_error_area"] = float(np.trapezoid(err, rw_t))

    trough_search_lo = t_fall_start if t_fall_start is not None else t_fall
    trough_search_hi = (
        (t_fall_end + 0.30 * slow_half_s) if t_fall_end is not None
        else (t_fall + 0.5 * slow_half_s))
    trough_mask = (t >= trough_search_lo) & (t <= trough_search_hi)
    t_trough = None
    trough_idx_global = None
    if int(trough_mask.sum()) >= 2:
        local = np.where(trough_mask)[0]
        trough_local_off = int(np.argmin(y_tared[local]))
        trough_idx_global = int(local[trough_local_off])
        t_trough = float(t[trough_idx_global])

    fall_anchor = t_fall_start if t_fall_start is not None else t_fall
    if t_trough is not None:
        metrics["fall_delay"] = max(0.0, float(t_trough) - fall_anchor)
    fall_window_mask = (t >= fall_anchor) & (t <= t_end)
    if int(fall_window_mask.sum()) >= 2:
        fw_t = t[fall_window_mask]
        fw_y = y_tared[fall_window_mask]
        err = np.abs(fw_y)
        metrics["fall_error_area"] = float(np.trapezoid(err, fw_t))

    if t_trough is not None:
        trough_value = float(y_tared[trough_idx_global])
        metrics["undershoot"] = max(0.0, -trough_value)

    recov_settle_margin = max(0.030, 0.05 * slow_half_s)
    if t_fall_end is not None:
        recov_t_lo = t_fall_end + recov_settle_margin
    elif t_trough is not None:
        recov_t_lo = float(t_trough) + 0.10 * slow_half_s
    else:
        recov_t_lo = float("inf")
    if np.isfinite(recov_t_lo):
        recov_t_hi = t_end - 0.05 * slow_half_s
        recov_mask = (t >= recov_t_lo) & (t <= recov_t_hi)
        if int(recov_mask.sum()) >= 2:
            rt = t[recov_mask]
            ry = y_tared[recov_mask]
            metrics["tail_area"] = float(np.trapezoid(np.abs(ry), rt))
            tol = max(3.0 * baseline_noise_std, 1e-9)
            settle_t = float("nan")
            settle_anchor = (
                float(t_trough) if t_trough is not None
                else (t_fall_end if t_fall_end is not None else t_fall))
            post_mask = (t >= settle_anchor) & (t <= t_end)
            pt = t[post_mask]
            py = y_tared[post_mask]
            if len(pt) >= 2:
                ok = np.abs(py) < tol
                start_idx = None
                for i in range(len(pt)):
                    if not ok[i]:
                        start_idx = None
                        continue
                    if start_idx is None:
                        start_idx = i
                    if pt[i] - pt[start_idx] >= 0.10:
                        settle_t = float(pt[start_idx]) - t_fall
                        break
            metrics["settling_time"] = settle_t

    if np.isfinite(high_level_tared) and baseline_noise_std > 0:
        leg_delta = high_level_tared
        if leg_delta < 3.0 * baseline_noise_std:
            reasons.append(
                f"rise {leg_delta:.1f} below 3*noise {3*baseline_noise_std:.1f}")
            excluded = True
        if leg_delta > 0 and baseline_noise_std > 0.30 * leg_delta:
            reasons.append(
                f"baseline noise {baseline_noise_std:.1f} > 30% of leg "
                f"delta {leg_delta:.1f}")
            excluded = True
    if t_trough is not None and baseline_noise_std > 0:
        trough_value = float(y_tared[trough_idx_global])
        if (np.isfinite(high_level_tared)
                and high_level_tared > 3.0 * baseline_noise_std
                and abs(trough_value) > 5.0 * high_level_tared):
            reasons.append(
                f"fall to {trough_value:.1f} more than 5x leg delta — "
                f"likely noise spike")
            excluded = True

    return BdSegment(
        k=k, seg_idx=seg_idx,
        t_start=t_start, t_rise=t_rise, t_fall=t_fall, t_end=t_end,
        t_lo_display=t_lo_display, t_hi_display=t_hi_display,
        t_rise_end=t_rise_end, t_fall_start=t_fall_start, t_fall_end=t_fall_end,
        t_peak=t_peak, t_trough=t_trough,
        n_samples=n, metrics=metrics,
        excluded=excluded, exclusion_reasons=reasons,
    )


def _bd_aggregate_per_k(segments_by_k):
    """Median over included segments -> per-K aggregates. Verbatim."""
    out = []
    for k, segs in segments_by_k.items():
        included = [s for s in segs if not s.excluded]
        medians = {}
        mads = {}
        iqrs = {}
        for name in BD_METRIC_NAMES:
            vals = [s.metrics.get(name, float("nan")) for s in included]
            if not vals:
                medians[name] = float("nan")
                mads[name] = float("nan")
                iqrs[name] = float("nan")
                continue
            arr = np.asarray(vals, dtype=float)
            if np.isnan(arr).all():
                medians[name] = float("nan")
                mads[name] = float("nan")
                iqrs[name] = float("nan")
                continue
            med = float(np.nanmedian(arr))
            medians[name] = med
            finite = arr[np.isfinite(arr)]
            if len(finite) >= 2:
                mads[name] = float(1.4826 * np.median(np.abs(finite - med)))
                q75, q25 = np.percentile(finite, [75, 25])
                iqrs[name] = float(q75 - q25)
            else:
                mads[name] = float("nan")
                iqrs[name] = float("nan")
        out.append(BdKResult(
            k=float(k),
            n_segments_total=len(segs),
            n_segments_included=len(included),
            medians=medians,
            mads=mads,
            iqrs=iqrs,
        ))
    return out


def _bd_compute_normalised(per_k):
    """In-place: fill `normalised` on each BdKResult. Verbatim."""
    if not per_k:
        return
    for name in BD_METRIC_NAMES:
        vals = np.asarray(
            [r.medians.get(name, float("nan")) for r in per_k], dtype=float)
        finite_abs = np.abs(vals[np.isfinite(vals)])
        if len(finite_abs) == 0:
            denom = 0.0
        else:
            denom = float(finite_abs.max())
        for r, v in zip(per_k, vals):
            if denom > 0 and np.isfinite(v):
                r.normalised[name] = float(v / denom)
            else:
                r.normalised[name] = float("nan")


def _bd_compute_cost(per_k, weights):
    """Composite cost per K over normalised metrics. NaN-safe. Verbatim."""
    cost = np.zeros(len(per_k), dtype=float)
    for i, r in enumerate(per_k):
        total = 0.0
        any_nan = False
        for name, w in weights.items():
            v = r.normalised.get(name, float("nan"))
            if not np.isfinite(v):
                any_nan = True
                break
            if name in ("overshoot", "undershoot"):
                v = max(0.0, v)
            total += w * v
        cost[i] = float("nan") if any_nan else total
    return cost


def _argmin_with_parabolic(k_values, cost):
    """K at the minimum of cost(K) with sub-step parabolic interp. Verbatim."""
    if len(k_values) == 0 or len(cost) == 0:
        return None
    finite = np.isfinite(cost)
    if not finite.any():
        return None
    k_f = k_values[finite].astype(float)
    c_f = cost[finite].astype(float)
    i = int(np.argmin(c_f))
    if len(c_f) < 3 or i == 0 or i == len(c_f) - 1:
        return float(k_f[i])
    x0, x1, x2 = float(k_f[i - 1]), float(k_f[i]), float(k_f[i + 1])
    y0, y1, y2 = float(c_f[i - 1]), float(c_f[i]), float(c_f[i + 1])
    denom = (x0 - x1) * (x0 - x2) * (x1 - x2)
    if denom == 0:
        return float(x1)
    a = (x2 * (y1 - y0) + x1 * (y0 - y2) + x0 * (y2 - y1)) / denom
    b = (x2 * x2 * (y0 - y1) + x1 * x1 * (y2 - y0)
         + x0 * x0 * (y1 - y2)) / denom
    if a <= 0:
        return float(x1)
    vertex = -b / (2.0 * a)
    return float(max(min(vertex, x2), x0))


# ====================== Klipper-native orchestrator ========================
# Equivalent of the reference analyse_sweep's per-K loop + post-loop fits,
# but fed EXACT per-K window bounds and per-cycle transition times (from
# Klipper print-time lookahead callbacks) instead of the Buddy clock-recovery
# front-end. The estimator math below is the same as the reference; only the
# boundary discovery differs (exact vs reverse-engineered), so K_opt outputs
# match for a given (force, boundaries) input. Gates/thresholds mirror the
# reference (MIN_COVERAGE_FOR_FIT, MAX_DROPOUT_FRAC, MIN_INCLUDED_SEGS,
# bd_half_win=min(0.075, 0.5*fast_half)); on clean ~488 SPS in-air captures
# they are no-ops, but they are kept so behaviour tracks the original.

def _merge_transitions(rising, falling, lo, hi):
    """Merge per-cycle rising(+1)/falling(-1) times into sorted (t, dir)
    arrays restricted to [lo, hi]. Mirrors how the reference builds
    seg_trans_t_rel / seg_trans_dirs from detected transitions."""
    rising = np.asarray(rising, dtype=float)
    falling = np.asarray(falling, dtype=float)
    t = np.concatenate([rising, falling])
    d = np.concatenate([np.ones(len(rising)), -np.ones(len(falling))])
    order = np.argsort(t, kind="mergesort")
    t, d = t[order], d[order]
    in_win = (t >= lo) & (t <= hi)
    return t[in_win], d[in_win]


def analyse_sweep_segments(t_rel, force_y, k_values, windows, transitions,
                           slow_v, fast_v, slow_half_s, fast_half_s,
                           cycle_period_s, resample_hz=1000.0):
    """Run Sweep's bd-segment step-response estimator across a K sweep given
    EXACT boundaries, and return the K at the cost-valley minimum (bd_k_opt).

    Sweep's single calibrator is the bd_pressure step-response cost (normalised
    overshoot/undershoot/rise-/fall-delay/tail composite, argmin over K) --
    PrusaPATuner's intended primary, validated on our hardware (a reference PLA
    sweep gave a clean cost valley at ~0.033, matching decay).
    The old phase-lag / integral-area / integral-legacy zero-crossing estimators
    were removed from Sweep entirely (code + tests): integral locked onto the
    wrong feature (0.014 at a deceptively high R^2) and phase-lag was a second,
    grid-dependent calibrator -- decay is the independent cross-check, so Sweep
    stays ONE method. Only the bd estimator and the helpers it needs remain.

    Parameters
    ----------
    t_rel, force_y : sweep-relative sample times (s) and raw force values.
    k_values       : list of K (pressure-advance) values, one per segment.
    windows[i]     : (t_lo, t_hi) sweep-relative force window for segment i.
    transitions[i] : (rising_t, falling_t) sweep-relative per-cycle slow->fast
                     and fast->slow times for segment i.
    slow_v, fast_v : commanded slow/fast filament feed (mm/s).
    slow_half_s, fast_half_s, cycle_period_s : motion geometry.

    Returns a SweepAnalysis (per_k, bd_per_k, bd_k_opt, windows, ...).
    """
    notes = []
    t_rel = np.asarray(t_rel, dtype=float)
    force_y = np.asarray(force_y, dtype=float)
    finite = np.isfinite(t_rel) & np.isfinite(force_y)
    if not finite.all():
        t_rel, force_y = t_rel[finite], force_y[finite]
    if len(t_rel) < 10:
        return SweepAnalysis(per_k=[], sample_rate_hz=0.0,
                             notes=["too few samples"])
    dt = 1.0 / resample_hz
    in_rate = (len(t_rel) - 1) / (t_rel[-1] - t_rel[0])

    per_k = []
    windows_out = []
    coverages = []
    bd_segments = []
    bd_segments_by_k = {}
    plateau_settle_s = 0.15
    plateau_slow_values = []
    plateau_fast_values = []
    MIN_COVERAGE_FOR_FIT = 0.5

    for seg_idx, k in enumerate(k_values):
        t_lo, t_hi = windows[seg_idx]
        rising, falling = transitions[seg_idx]
        rising = np.asarray(rising, dtype=float)
        falling = np.asarray(falling, dtype=float)
        mask = (t_rel >= t_lo) & (t_rel <= t_hi)
        seg_t = t_rel[mask]
        seg_y = force_y[mask]
        expected_n = max(1.0, (t_hi - t_lo) * max(in_rate, 1.0))
        coverage = min(1.0, len(seg_t) / expected_n)

        # Dropout detection (verbatim from the reference per-K loop).
        dropout_t_list = []
        if len(seg_t) >= 10:
            dt_intervals = np.diff(seg_t)
            median_dt = float(np.median(dt_intervals))
            time_gap_thresh = max(10.0 * median_dt, 0.050)
            time_gap_mask = dt_intervals > time_gap_thresh
            seg_p10 = float(np.percentile(seg_y, 10))
            seg_p90 = float(np.percentile(seg_y, 90))
            plateau_spread = seg_p90 - seg_p10
            if plateau_spread > 50.0:
                force_jumps = np.abs(np.diff(seg_y)) > 0.5 * plateau_spread
                dropout_mask = time_gap_mask & force_jumps
            else:
                dropout_mask = np.zeros_like(time_gap_mask)
            drop_idx = np.where(dropout_mask)[0] + 1
            dropout_t_list = [float(seg_t[i]) for i in drop_idx]
        dropout_abs = np.asarray(dropout_t_list, dtype=float)

        if len(seg_t) < 8:
            notes.append("K=%.4f: only %d samples, skipping" % (k, len(seg_t)))
            per_k.append(KResult(k=k, n_samples=len(seg_t),
                                 force_mean=float("nan"),
                                 force_std=float("nan"), coverage=coverage))
            windows_out.append(KWindow(k=k, t=[], force=[], command=[]))
            coverages.append(coverage)
            bd_segments_by_k[k] = []
            continue

        grid, force = _resample_uniform(seg_t, seg_y, dt)
        trans_t, trans_d = _merge_transitions(rising, falling, grid[0],
                                              grid[-1])
        if len(trans_t) >= 2:
            command = _square_wave_at_transitions(grid, trans_t, trans_d,
                                                  low_val=slow_v,
                                                  high_val=fast_v)
        else:
            command = np.full_like(grid, slow_v)

        # Plateau samples for slow/fast force baselines (UI overlay).
        if len(trans_t) >= 2:
            idx = np.clip(np.round((trans_t - grid[0]) / dt).astype(int),
                          0, len(grid) - 1)
            uniq, keep = np.unique(idx, return_index=True)
            seg_trans_idx = uniq
            seg_trans_dirs = trans_d[keep]
            settle_n = max(1, int(round(plateau_settle_s / dt)))
            for j in range(len(seg_trans_idx) - 1):
                a = int(seg_trans_idx[j]) + settle_n
                b = int(seg_trans_idx[j + 1])
                if b - a < 4:
                    continue
                med = float(np.median(force[a:b]))
                if float(seg_trans_dirs[j]) > 0:
                    plateau_fast_values.append(med)
                else:
                    plateau_slow_values.append(med)

        per_k.append(KResult(k=k, n_samples=int(len(seg_t)),
                             force_mean=float(np.mean(seg_y)),
                             force_std=float(np.std(seg_y)),
                             coverage=coverage, dropouts=len(dropout_t_list)))

        # bd_pressure step-response segments from the exact transitions.
        bd_segs = []
        n_pairs = min(len(rising), len(falling))
        for c in range(n_pairs):
            t_rise = float(rising[c])
            t_fall = float(falling[c])
            if t_fall <= t_rise:
                continue
            bd_segs.append(_bd_segment_metrics(
                force_t=t_rel, force_y=force_y, k=float(k), seg_idx=c,
                t_start=t_rise - slow_half_s, t_rise=t_rise, t_fall=t_fall,
                t_end=t_fall + slow_half_s, slow_half_s=float(slow_half_s),
                fast_half_s=float(fast_half_s), dropout_t=dropout_abs))
        bd_segments_by_k[k] = bd_segs
        bd_segments.extend(bd_segs)

        command_ui = (command if len(trans_t) >= 2
                      else np.full_like(seg_t, slow_v))
        windows_out.append(KWindow(
            k=k, t=[float(x) for x in seg_t], force=[float(x) for x in seg_y],
            command=[float(x) for x in (_square_wave_at_transitions(
                seg_t, trans_t, trans_d, slow_v, fast_v)
                if len(trans_t) >= 2 else command_ui)],
            dropout_t=dropout_t_list))
        coverages.append(coverage)

    # ---- force baselines (UI overlay; does not affect K_opt) -------------
    force_baselines = None
    if len(plateau_slow_values) >= 4 and len(plateau_fast_values) >= 4:
        force_baselines = ForceBaselines(
            slow_plateau=float(np.median(plateau_slow_values)),
            fast_plateau=float(np.median(plateau_fast_values)),
            n_slow=len(plateau_slow_values), n_fast=len(plateau_fast_values))

    # ---- quality gate (coverage + dropout fraction) ----------------------
    cov_arr = np.asarray(coverages, dtype=float)
    dropout_arr = np.asarray([r.dropouts for r in per_k], dtype=float)
    nsamp_arr = np.asarray([max(1, r.n_samples) for r in per_k], dtype=float)
    drop_frac = dropout_arr / nsamp_arr
    MAX_DROPOUT_FRAC = 0.05
    quality_mask = (cov_arr >= MIN_COVERAGE_FOR_FIT) & \
        (drop_frac <= MAX_DROPOUT_FRAC)

    # ---- bd_pressure aggregate -> normalise -> cost -> argmin ------------
    MIN_INCLUDED_SEGS = 4
    bd_per_k = _bd_aggregate_per_k(bd_segments_by_k)
    bd_by_k = {r.k: r for r in bd_per_k}
    bd_per_k = [bd_by_k.get(k, BdKResult(
        k=k, n_segments_total=0, n_segments_included=0,
        medians={n: float("nan") for n in BD_METRIC_NAMES}))
        for k in k_values]
    _bd_compute_normalised(bd_per_k)
    bd_quality_mask = np.array(
        [quality_mask[i] and bd_per_k[i].n_segments_included >= MIN_INCLUDED_SEGS
         for i in range(len(bd_per_k))], dtype=bool)
    bd_cost = _bd_compute_cost(bd_per_k, BD_DEFAULT_WEIGHTS)
    if bd_quality_mask.any():
        ks_bd = np.asarray([r.k for r in bd_per_k], dtype=float)[bd_quality_mask]
        bd_k_opt = _argmin_with_parabolic(ks_bd, bd_cost[bd_quality_mask])
    else:
        bd_k_opt = None

    total_segs = sum(r.n_segments_total for r in bd_per_k)
    total_incl = sum(r.n_segments_included for r in bd_per_k)
    if total_segs:
        notes.append("bd_pressure: %d/%d segments included across %d K values"
                     % (total_incl, total_segs, len(bd_per_k)))
    if in_rate < 100:
        notes.append("incoming sample rate only %.0f Hz -- analysis may be "
                     "noisy" % in_rate)

    return SweepAnalysis(
        per_k=per_k, sample_rate_hz=float(in_rate),
        notes=notes, windows=windows_out, force_baselines=force_baselines,
        bd_segments=bd_segments, bd_per_k=bd_per_k, bd_k_opt=bd_k_opt,
        bd_default_weights=dict(BD_DEFAULT_WEIGHTS))
