# autopa - "Sweep" pressure-advance estimator (K-sweep, force-tracking)
#
# Copyright (C) 2026  autopa contributors
# Motion shape + analysis derived from PrusaPATuner by CNCKitchen (AGPL-3.0):
# https://github.com/CNCKitchen/PrusaPATuner -- "Sweep" is autopa's name for
# this approach.
#
# This file may be distributed under the terms of the GNU AGPLv3 (or later)
# license.
#
# AUTOPA_SWEEP drives an asymmetric slow/fast square-wave extrusion in air at a
# grid of pressure-advance K values and measures how well the load-cell force
# tracks the commanded flow. The estimator (ported numpy-only in
# sweep_analysis.py, output-identical to the upstream scipy original) is the
# bd step-response cost minimum -> K_opt. (The phase-lag and integral-area
# estimators were tried during R&D and dropped -- noise on this load cell; the
# port keeps only the bd path.)
#
# AXIS WOBBLE (required for any signal): Klipper only applies pressure advance
# to a move that has X or Y motion -- kinematics/extruder.py sets
# `can_pressure_advance = axis_r > 0 and (axes_d[0] or axes_d[1])`. A pure-E
# sweep therefore leaves PA *inert*: every K produces identical motion and the
# estimators see nothing but noise/priming drift. So each leg couples a tiny
# move on one axis (WOBBLEAXIS, default Y; X also works -- Z does NOT, it is not
# gated in) by WOBBLE mm. The slow legs sit at base+WOBBLE, the fast legs return
# to base, so every slow<->fast transition is an axis reversal that flips the PA
# gate on. The wobble is quasi-static (e.g. 0.05mm over a 0.25-1.0s leg =
# 0.05-0.2 mm/s) -- it exists only to satisfy the firmware gate, not to move the
# toolhead meaningfully, and on a bed-slinger a Y wobble doesn't touch the head
# at all. Exact per-cycle transition times come from print-time lookahead
# callbacks -- no marker pulse / clock-recovery needed.
#
# Because the leg is now a composite XY+E move, Klipper enforces
# `max_extrude_cross_section` on it (a large E over a tiny XY move = a high
# extrude ratio). We pre-check and, if the configured wobble would trip it, tell
# the operator the exact value to set. The leg's E-velocity step sharpness (what
# PA actually acts on) is governed by the toolhead accel here, not
# max_extrude_only_accel, so ACCEL is applied via SET_VELOCITY_LIMIT and
# restored afterwards.
#
# FUTURE (drop the wobble): the cleaner fix is upstream -- a Klipper gcode/config
# knob to force `can_pressure_advance` on for extrude-only moves (e.g. a
# SET_PRESSURE_ADVANCE flag or a per-move override). With that, an in-air PA
# measurement needs no axis motion at all and WOBBLE can go to 0. Intend to PR
# this to Klipper; until it lands (and on older Klipper), the wobble is required.
# Setting WOBBLE=0 already selects the pure-E path for that future / for testing,
# but on current Klipper it measures nothing.
import logging


class SweepMixin:
    def _register_sweep_commands(self):
        self.gcode.register_command('AUTOPA_SWEEP', self.cmd_AUTOPA_SWEEP,
                                    desc=self.cmd_AUTOPA_SWEEP_help)

    cmd_AUTOPA_SWEEP_help = (
        "Calibrate pressure advance by sweeping K with an in-air asymmetric "
        "slow/fast extrusion square wave (the recommended method). Reports optimal "
        "K from force-tracking (bd step-response), applies it live when a valid "
        "K is found (APPLY=0 to skip), and prints a paste-able "
        "SET_PRESSURE_ADVANCE line. Args: SLOW FAST TSLOW TFAST CYCLES KSTART "
        "KEND KSTEP WARMUP MAXFILAMENT WOBBLEAXIS WOBBLE ACCEL APPLY.")
    def cmd_AUTOPA_SWEEP(self, gcmd):
        import numpy as np
        lc = self._get_load_cell(gcmd)
        toolhead = self.printer.lookup_object('toolhead')
        self._check_extrude_temp(gcmd)
        # mm/s filament feed. Defaults ~0.83 / 7.5 mm/s == 2 / 18 mm³/s for
        # 1.75 mm filament (the web UI presents these as volumetric mm³/s). The
        # fast leg is intentionally high to create a strong flow step for PA to
        # act on; 18 mm³/s is brief (per fast leg) so even modest extruders
        # reach it. The web UI shows the volumetric equivalents.
        slow = gcmd.get_float('SLOW', 0.83, above=0.)
        fast = gcmd.get_float('FAST', 7.5, above=0.)
        if fast <= slow:
            raise gcmd.error("autopa sweep: FAST must be > SLOW")
        tslow = gcmd.get_float('TSLOW', 1.0, above=0.1, maxval=10.)
        tfast = gcmd.get_float('TFAST', 0.25, above=0.05, maxval=10.)
        cycles = gcmd.get_int('CYCLES', 8, minval=3, maxval=40)
        # K grid bounds. Default 0.01-0.08 brackets the common PLA/PETG range
        # (~0.02-0.05) with headroom; KSTART=0 wastes a step (K=0 is just the
        # no-PA baseline). If the cost minimum lands on an edge, the report
        # warns to widen the range (the true optimum may be outside it).
        kstart = gcmd.get_float('KSTART', 0.01, minval=0.)
        kend = gcmd.get_float('KEND', 0.08, minval=0.)
        kstep = gcmd.get_float('KSTEP', 0.01, above=0.)
        if kend < kstart:
            raise gcmd.error("autopa sweep: KEND must be >= KSTART")
        # warm-up multiplier on the very first slow leg of the whole sweep, so
        # the melt reaches steady pressure before the first measured cycle.
        warmup = gcmd.get_float('WARMUP', 4.0, minval=1., maxval=30.)
        maxmm = gcmd.get_float('MAXFILAMENT', 400., above=0.)
        # PA-gate wobble (see module header): which axis to jiggle and by how
        # much. Only X or Y flip Klipper's can_pressure_advance gate; Z does
        # not. Default Y (a bed-slinger's Y move doesn't touch the toolhead).
        # WOBBLE=0 selects the pure-E path (PA inert on current Klipper -- only
        # useful once the upstream gate override lands).
        wobble_axis = gcmd.get('WOBBLEAXIS', 'Y').strip().upper()
        if wobble_axis not in ('X', 'Y'):
            raise gcmd.error("autopa sweep: WOBBLEAXIS must be X or Y (Z does "
                             "not trigger Klipper pressure advance)")
        wobble = gcmd.get_float('WOBBLE', 0.05, minval=0.)
        # Toolhead accel during the sweep, applied via SET_VELOCITY_LIMIT and
        # restored in the finally block. Default deliberately LOW (1000): the
        # composite move multiplies accel by the extrude ratio (E-accel =
        # accel*axis_r, axis_r~40 at the default wobble), so even 1000 gives a
        # sub-millisecond E velocity step. And Klipper smooths PA over
        # pressure_advance_smooth_time (~40ms) regardless, so the force shape is
        # set by that smoothing, not the step sharpness -- accel only has to
        # clear "transition << smooth_time", which 1000 does easily. (Unlike
        # PrusaPATuner's accel-limited pure-E Buddy moves, which needed 5000.)
        # 1000 is also within reach of slow machines (e.g. Ender-3 max ~4000).
        accel = gcmd.get_float('ACCEL', 1000., above=0.)
        # Apply the found K live on success (default on, like AUTOPA_DECAY).
        # Sweep is experimental and frequently finds no valid optimum, so the
        # report only applies when K_opt is a real non-negative value.
        apply = bool(gcmd.get_int('APPLY', 1, minval=0, maxval=1))
        axis_idx = {'X': 0, 'Y': 1}[wobble_axis]

        slow_half = tslow
        fast_half = tfast
        period = slow_half + fast_half
        ks, k = [], kstart
        while k <= kend + 1e-9:
            ks.append(round(k, 6))
            k += kstep
        # filament budget: per K = leading slow + cycles*(fast leg + slow leg).
        slow_mm = slow * tslow
        fast_mm = fast * tfast
        per_k = slow_mm + cycles * (fast_mm + slow_mm)
        warmup_extra = (warmup - 1.0) * slow_mm     # first K's extended leg
        total_mm = per_k * len(ks) + warmup_extra
        if total_mm > maxmm:
            raise gcmd.error(
                "autopa sweep would extrude %.0fmm over %d K values "
                "(> MAXFILAMENT %.0f); reduce CYCLES / widen KSTEP / lower "
                "speeds" % (total_mm, len(ks), maxmm))

        # Composite-move pre-checks (only when wobbling). A wobble leg is a
        # G1 with the axis AND E moving, so it needs the axis homed and must
        # clear the extruder's max_extrude_cross_section (a big E over a tiny
        # axis move = a high extrude ratio). Surface the exact fix up front
        # rather than aborting mid-sweep with PA left changed.
        if wobble > 0.:
            est = toolhead.get_status(self.reactor.monotonic())
            if wobble_axis.lower() not in est.get('homed_axes', ''):
                raise gcmd.error(
                    "autopa sweep: %s axis not homed -- home it before "
                    "AUTOPA_SWEEP (the PA-gate wobble moves %s), or pass "
                    "WOBBLE=0 to use the pure-E path (no pressure advance on "
                    "current Klipper)" % (wobble_axis, wobble_axis))
            extruder = toolhead.get_extruder()
            # Warm-up slow leg is the longest single-leg extrusion.
            max_leg_e = max(slow * tslow * warmup, slow * tslow, fast * tfast)
            need_ratio = max_leg_e / wobble
            max_ratio = getattr(extruder, 'max_extrude_ratio', None)
            fil_area = getattr(extruder, 'filament_area', None)
            if (max_ratio is not None and fil_area is not None
                    and need_ratio > max_ratio):
                raise gcmd.error(
                    "autopa sweep: a %.3fmm %s wobble with up to %.2fmm of "
                    "filament per leg exceeds this extruder's "
                    "max_extrude_cross_section (extrude ratio %.1f > %.3f). "
                    "Add 'max_extrude_cross_section: %.1f' (or higher) to your "
                    "[extruder] and restart, or raise WOBBLE."
                    % (wobble, wobble_axis, max_leg_e, need_ratio, max_ratio,
                       need_ratio * fil_area * 1.05))

        orig_pa = self._get_pa()
        # per-K: (t_lo, t_hi) window + per-cycle rising/falling transition
        # print-times, all absolute print-time; converted to sweep-relative
        # (minus t0) for analysis.
        windows = []
        transitions = []
        collector = lc.get_collector()
        t0 = toolhead.get_last_move_time()
        collector.start_collecting(min_time=t0)
        wobbling = wobble > 0.
        old_accel = toolhead.get_status(
            self.reactor.monotonic()).get('max_accel') if wobbling else None

        def _leg(e_amt, dur, target):
            # One extrusion leg. When wobbling, it is a composite axis+E move:
            # F is the AXIS travel speed (mm/min) chosen so the leg lasts `dur`
            # while the axis covers `wobble`, and E is slaved to extrude `e_amt`
            # over that same duration (E velocity == e_amt/dur). The slow<->fast
            # E step at each leg boundary is the axis reversal that flips PA on.
            # When not wobbling, it's a pure-E move (PA inert) and F is the E
            # feedrate directly.
            if wobbling:
                f = (wobble / dur) * 60.
                self.gcode.run_script_from_command(
                    "G1 %s%.4f E%.4f F%.2f"
                    % (wobble_axis, target, e_amt, f))
            else:
                self.gcode.run_script_from_command(
                    "G1 E%.4f F%.0f" % (e_amt, (e_amt / dur) * 60.))

        # whole queued sequence: baseline dwell + per-K legs + warmup extra
        self._set_busy('sweep', 0.5 + len(ks) * (tslow + cycles
                       * (tfast + tslow)) + (warmup - 1.) * tslow)
        try:
            toolhead.dwell(0.5)                 # baseline before first burst
            self.gcode.run_script_from_command("G90")   # absolute XYZ
            self.gcode.run_script_from_command("M83")   # relative E
            if wobbling:
                self.gcode.run_script_from_command(
                    "SET_VELOCITY_LIMIT ACCEL=%.0f" % accel)
            base = toolhead.get_position()[axis_idx] if wobbling else 0.
            hi_pos = base + wobble
            lo_pos = base
            # Every leg must FLIP the wobble axis so each transition is a real
            # reversal (a zero-distance axis move degenerates back to pure-E and
            # turns the PA gate off). We toggle hi/lo on each leg regardless of
            # slow/fast -- the slow/fast labelling lives in the rising/falling
            # callbacks, not the physical wobble direction. Toolhead starts at
            # base (=lo), so the first leg flips to hi; toggling continues
            # unbroken across K boundaries.
            wob = [False]      # mutable so the closure can flip it

            def _next_target():
                wob[0] = not wob[0]
                return hi_pos if wob[0] else lo_pos

            for ki, kv in enumerate(ks):
                self._set_pa(kv)
                t_k0 = toolhead.get_last_move_time()
                rising, falling = [], []
                # leading slow leg (extended on the very first K only)
                lead = tslow * (warmup if ki == 0 else 1.0)
                _leg(slow * lead, lead, _next_target())
                for c in range(cycles):
                    # end of the preceding slow leg == slow->fast (rising)
                    toolhead.register_lookahead_callback(
                        lambda pt, a=rising: a.append(pt))
                    _leg(fast * tfast, tfast, _next_target())
                    # end of the fast leg == fast->slow (falling)
                    toolhead.register_lookahead_callback(
                        lambda pt, a=falling: a.append(pt))
                    _leg(slow * tslow, tslow, _next_target())
                t_k1 = toolhead.get_last_move_time()
                windows.append((t_k0, t_k1))
                transitions.append((rising, falling))
            t_end = toolhead.get_last_move_time()
            samples, errs = collector.collect_until(t_end)
        finally:
            self._clear_busy()
            try:
                self._set_pa(orig_pa)             # always restore PA
            except Exception:
                logging.exception("autopa sweep: failed to restore PA")
            if wobbling and old_accel:
                try:
                    self.gcode.run_script_from_command(
                        "SET_VELOCITY_LIMIT ACCEL=%.0f" % old_accel)
                except Exception:
                    logging.exception("autopa sweep: failed to restore accel")

        meta = self._base_meta(lc, 'sweep', gcmd)
        meta.update({'slow': slow, 'fast': fast, 'tslow': tslow, 'tfast': tfast,
                     'cycles': cycles, 'orig_pa': orig_pa, 'ks': ks,
                     'warmup': warmup, 't0': t0, 'errs': errs,
                     'wobble': wobble, 'wobble_axis': wobble_axis,
                     'accel': accel, 'apply': apply,
                     'windows': [(a - t0, b - t0) for a, b in windows],
                     'transitions': [([r - t0 for r in rs], [f - t0 for f in fs])
                                     for rs, fs in transitions]})
        self._report_sweep(gcmd, np.asarray(samples, dtype=float), meta)

    def _sweep_analyse(self, arr, meta):
        # Replay saved/captured samples through the ported analysis. Shared by
        # the live report and the capture_detail webhook so a saved sweep renders
        # in the UI with the exact same code that produced it.
        from . import sweep_analysis as sa
        t_rel = arr[:, 0] - meta['t0']
        force = -(arr[:, 2] - arr[:, 3])     # push -> larger; raw counts
        return sa.analyse_sweep_segments(
            t_rel, force, meta['ks'], meta['windows'], meta['transitions'],
            slow_v=meta['slow'], fast_v=meta['fast'],
            slow_half_s=meta['tslow'], fast_half_s=meta['tfast'],
            cycle_period_s=meta['tslow'] + meta['tfast'])

    @staticmethod
    def _sweep_per_k(res):
        # Per-K diagnostic table as JSON-safe scalars (NaN -> None) for
        # get_status / capture_detail; mirrors the console table columns.
        def _f(v):
            return float(v) if v == v else None
        return [{'k': float(bd.k),
                 'segs_inc': int(bd.n_segments_included),
                 'segs_tot': int(bd.n_segments_total),
                 'overshoot': _f(bd.medians.get('overshoot', float('nan'))),
                 'undershoot': _f(bd.medians.get('undershoot', float('nan')))}
                for bd in res.bd_per_k]

    def _report_sweep(self, gcmd, arr, meta):
        import numpy as np
        if not len(arr):
            raise gcmd.error("autopa sweep: no samples captured (errors=%s)"
                             % (meta['errs'],))
        res = self._sweep_analyse(arr, meta)

        wob = meta.get('wobble', 0.)
        wob_desc = ("%s-wobble %.3fmm" % (meta.get('wobble_axis', '?'), wob)
                    if wob > 0. else "PURE-E (no PA!)")
        lines = ["autopa sweep: slow=%.2f fast=%.2f mm/s, %dx(%.2f/%.2fs), "
                 "%d K, %s, %.0f SPS, errors=%s"
                 % (meta['slow'], meta['fast'], meta['cycles'], meta['tslow'],
                    meta['tfast'], len(meta['ks']), wob_desc, meta['sps'],
                    meta['errs'])]
        if wob <= 0.:
            lines.append("  WARNING: WOBBLE=0 -> moves are extrude-only, so "
                         "Klipper applied NO pressure advance; K_opt is "
                         "meaningless on current firmware (see module header).")
        # Sweep's single estimator is the bd step-response cost. The per-K
        # overshoot/undershoot medians are the diagnostic: overshoot should be
        # ~0 up to K_opt then climb (PA over-firing) -- if it stays flat across
        # K, PA isn't being applied (check the wobble) and K_opt is meaningless.
        lines.append("  K        segs    overshoot   undershoot")

        def _num(v):
            return "%10.1f" % v if v == v else "       nan"   # nan-safe
        for bd in res.bd_per_k:
            nseg = "%d/%d" % (bd.n_segments_included, bd.n_segments_total)
            lines.append("  %-7.4f  %-6s %s  %s"
                         % (bd.k, nseg, _num(bd.medians.get('overshoot',
                            float('nan'))),
                            _num(bd.medians.get('undershoot', float('nan')))))
        k_opt = res.bd_k_opt
        if k_opt is not None:
            lines.append("  Sweep K_opt = %.4f  =>  pressure advance = %.4f  "
                         "(bd step-response valley)" % (k_opt, k_opt))
            # Edge check: a minimum sitting on the first/last swept K means the
            # true optimum is probably outside the range -- the valley wasn't
            # bracketed, so K_opt is a clipped boundary, not a real minimum.
            ks = meta['ks']
            edge = kstep * 0.5
            if k_opt <= ks[0] + edge:
                lines.append("  WARNING: K_opt is at the LOW edge of the swept "
                             "range [%.3f, %.3f]; the true optimum may be below "
                             "KSTART -- lower KSTART and re-run." % (ks[0],
                                                                     ks[-1]))
            elif k_opt >= ks[-1] - edge:
                lines.append("  WARNING: K_opt is at the HIGH edge of the swept "
                             "range [%.3f, %.3f]; the true optimum may be above "
                             "KEND -- raise KEND and re-run." % (ks[0], ks[-1]))
        else:
            lines.append("  Sweep K_opt: none -- no K passed the segment-quality "
                         "gate (need a finer K grid through the optimum, or "
                         "stronger signal)")
        for n in res.notes:
            lines.append("  note: %s" % n)
        # apply live (default) + paste line, only for a real non-negative K and
        # only when PA was actually exercised (wobble on).
        if k_opt is not None and k_opt >= 0. and wob > 0.:
            self._report_applied_pa(lines, k_opt, meta.get('apply', True))
        elif meta.get('apply', True) and k_opt is not None:
            lines.append("  not applied: K_opt=%.4f is not a usable PA "
                         "(experimental method)" % k_opt)

        result = {'k_opt': res.bd_k_opt, 'bd_k_opt': res.bd_k_opt,
                  'sample_rate_hz': res.sample_rate_hz}
        if self.save_captures:
            path = self._save_capture(arr, meta, dict(kind='sweep', **result))
            if path:
                lines.append("capture saved: %s" % path)
        # per_k is UI-only (kept out of the saved-capture stats, like decay's plot)
        self._last = {'sweep': dict(result, per_k=self._sweep_per_k(res))}
        gcmd.respond_info("\n".join(lines))
