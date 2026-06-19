# autopa - decay-tau pressure-advance estimator (autopa's own method)
#
# Copyright (C) 2026  autopa contributors
#
# This file may be distributed under the terms of the GNU AGPLv3 (or later)
# license.
#
# optimal PA = melt time constant tau. Extrude a pulse then STOP; the force
# relaxes ~exp(-t/tau) as residual pressure bleeds out the nozzle. Done at
# PA=0 so no injection confound. Many pulses are folded (synchronous-average)
# to beat the noise floor and average over hysteresis.
#
# The decay loop's timing is delicate; treat edits here as high-risk and
# validate against saved captures before changing anything.
import logging


class DecayMixin:
    def _register_decay_commands(self):
        self.gcode.register_command('AUTOPA_DECAY', self.cmd_AUTOPA_DECAY,
                                    desc=self.cmd_AUTOPA_DECAY_help)

    cmd_AUTOPA_DECAY_help = (
        "Calibrate pressure advance from post-stop melt-force decay (autopa's "
        "own method; EXPERIMENTAL). Pulse+stop at PA=0, fit the melt "
        "time-constant tau "
        "(= optimal PA), robust-median over pulses. Reports tau, applies it "
        "live (APPLY=0 to skip), and prints a paste-able SET_PRESSURE_ADVANCE "
        "line. VFR is the volumetric flow (mm^3/s). Args: VFR PULSE OFF PULSES "
        "PA PRIME WARMUP WINDOW SNRMIN MAXFILAMENT RETRACT APPLY.")
    def _decay_params(self, gcmd):
        # parsing for AUTOPA_DECAY
        vfr = gcmd.get_float('VFR', 18.0, above=0.)        # mm^3/s volumetric flow
        flow = self._vol_to_lin(vfr)                       # mm/s linear feed (internal)
        # Excitation duration (s) = PULSE/flow. EXCITATION holds it at ~0.27s so
        # varying VFR does NOT silently change how fully the melt charges: PULSE
        # auto-scales with the linear feed. Pass PULSE to override the coupling.
        # The exact value is not critical -- it only has to exceed ~5*tau (see
        # below); 0.27 sits comfortably above that, so rounding it off the old
        # 2.0/7.5 = 0.2667 does not change the result.
        excit = gcmd.get_float('EXCITATION', 0.27, above=0.)
        pulse = gcmd.get_float('PULSE', None, above=0.)
        if pulse is None:
            pulse = excit * flow
        p = {
            # VFR=18 mm³/s (the volumetric default; == 7.5 mm/s linear for 1.75 mm
            # filament, and matches the sweep calibration leg). PULSE auto = ~2 mm.
            #
            # Excitation duration = PULSE/flow ~= 0.27s. This is NOT arbitrary:
            # the load cell reads melt pressure, which at constant flow rises
            # toward steady state as a 1st-order system with time constant tau --
            # and that tau IS pressure advance (Klipper PA models exactly this
            # pressure-vs-flow lag). A 1st-order system reaches ~99% of steady
            # state in ~5 tau; with tau ~= 0.045s for PLA/PETG, 5 tau ~= 0.225s,
            # so ~0.27s exists to FULLY develop the melt pressure PA compensates,
            # making the post-stop decay reflect the same fully-charged compliance
            # a steady print sees. It also explains the unavoidable slow ooze tail:
            # the slow mode's constant (tau_slow ~= 0.13s) is SHORTER than 5*tau,
            # so the fast mode can't be fully charged without partly charging the
            # slow one -- which is why a single-exp tau is window-dependent (see
            # WINDOW) and a bi-exp "fast-mode extraction" fails (it discards the
            # very signal sweep's optimum responds to).
            #
            # Held fixed (not exposed): excitation and window are the same
            # tau-coupled knob. PULSE auto-scales with the linear feed so the
            # duration is VFR-invariant by construction. Revisit tau-adaptive
            # excitation only for materials whose tau leaves the band where
            # 5*tau ~= 0.2-0.3s.
            'vfr': vfr,                                  # mm^3/s (the user knob)
            'flow': flow,                  # mm/s linear feed (internal; from VFR)
            'pulse': pulse,            # mm per pulse (auto = EXCITATION * lin feed)
            'off': gcmd.get_float('OFF', 0.5, above=0.1, maxval=5.),
            'pulses': gcmd.get_int('PULSES', 20, minval=3, maxval=200),
            'pa': gcmd.get_float('PA', 0.0, minval=0.),
            # Early-window exp fit length (s). The post-stop force is bi-modal:
            # a FAST melt-pressure relaxation (= Klipper's 1st-order PA tau) plus
            # a SLOW ooze/thermal tail. A full-window single-exp gets dragged up
            # by the tail (tau 0.04->0.066 run-to-run); fitting only the first
            # ~3 tau isolates the PA-relevant fast mode and ~halves the spread.
            # WINDOW is a FIXED convention (the defining cut for the reported tau):
            # the sv2 multi-material suite showed no fit-free observable reproduces
            # sweep's cross-material spread, so an auto/adaptive window was rejected
            # (see memory decay-tau-area-rejected). 0.14 matches sweep for stiff
            # PLA/PETG-CF; oozy PETG reads lower but neither method is ground truth.
            'window': gcmd.get_float('WINDOW', 0.14, above=0., maxval=0.5),
            'snrmin': gcmd.get_float('SNRMIN', 4.0, minval=0.),
            # Two-stage prime so the first MEASURED pulse already sits in the
            # pulsed quasi-steady-state. (1) PRIME: a continuous bulk fill that
            # clears the end-of-print retract and saturates the melt -- fighting a
            # multi-mm retract with 2mm pulses is backwards, the early pulses just
            # refill instead of measuring. (2) WARMUP: unrecorded pulses at the
            # EXACT measurement cadence (pulse+off). A continuous prime fills a
            # different state than the 50%-duty pulse train, and the old settle
            # dwell (~16 tau) discharged the melt -- both biased the first pulses
            # low (amplitude ramped over ~8-10 pulses). Warmup-at-cadence removes
            # the ramp; PRIME alone cannot.
            # PRIME=10 still ramped on a cold first-run-after-retract (warmup
            # can't fix insufficient bulk fill); 20 flattened it from pulse 1.
            'prime': gcmd.get_float('PRIME', 20.0, minval=0.),  # mm filament
            'warmup': gcmd.get_int('WARMUP', 10, minval=0, maxval=100),
            'maxmm': gcmd.get_float('MAXFILAMENT', 250., above=0.),
            # Post-calibration retract (mm filament): the run ends with a charged,
            # molten nozzle that oozes a blob in-air. Pull the melt back when done
            # so the toolhead is left clean. 6mm clears a typical melt zone (2mm
            # barely dents the ooze on most hotends). 0 disables.
            'retract': gcmd.get_float('RETRACT', 6.0, minval=0.),
            # Apply the measured PA live on success (default on): a mid-print
            # run can then be ignored and the print just continues with the
            # calibrated value. APPLY=0 measures without touching printer state.
            'apply': bool(gcmd.get_int('APPLY', 1, minval=0, maxval=1)),
        }
        total = (p['pulses'] + p['warmup']) * p['pulse'] + p['prime']
        if total > p['maxmm']:
            raise gcmd.error("autopa decay would extrude %.0fmm (> MAXFILAMENT "
                             "%.0f)" % (total, p['maxmm']))
        return p

    @staticmethod
    def _decay_expected_s(p):
        # wall time of the queued move sequence (prime + warmup + measured
        # pulses); the UI renders progress as elapsed/expected (see _set_busy)
        return (p['prime'] / p['flow']
                + (p['warmup'] + p['pulses']) * (p['pulse'] / p['flow']
                                                 + p['off']))

    def _capture_decay(self, lc, toolhead, p, gcmd):
        # run the pulse+stop sequence at PA=0 and collect the force trace.
        import numpy as np
        orig_pa = self._get_pa()
        stops = []
        collector = None
        self._save_gcode_state()        # restore G90/G91 + M82/M83 afterwards
        try:
            self._set_pa(p['pa'])
            collector = lc.get_collector()
            t0 = toolhead.get_last_move_time()
            collector.start_collecting(min_time=t0)
            self.gcode.run_script_from_command("M83")
            # stage 1 -- continuous bulk prime: clear the end-of-print retract
            # and saturate the melt from a cold/relaxed start
            if p['prime'] > 0:
                self.gcode.run_script_from_command(
                    "G1 E%.4f F%.0f" % (p['prime'], p['flow'] * 60.))
            # stage 2 -- warmup pulses: run the EXACT measurement cadence
            # (pulse+off) UNRECORDED so the melt reaches the pulsed quasi-steady-
            # state before the first recorded pulse. No settle dwell (it would
            # discharge the melt). See _decay_params for the rationale.
            for _ in range(p['warmup']):
                self.gcode.run_script_from_command(
                    "G1 E%.4f F%.0f" % (p['pulse'], p['flow'] * 60.))
                toolhead.dwell(p['off'])
            for i in range(p['pulses']):
                self.gcode.run_script_from_command(
                    "G1 E%.4f F%.0f" % (p['pulse'], p['flow'] * 60.))
                toolhead.register_lookahead_callback(
                    lambda pt: stops.append(pt))   # fires at end of the pulse
                toolhead.dwell(p['off'])
            t_end = toolhead.get_last_move_time()
            samples, errs = collector.collect_until(t_end)
            # post-calibration retract: pull the charged melt back so the run
            # doesn't leave an oozing blob in-air (unrecorded; after collection).
            if p['retract'] > 0:
                self.gcode.run_script_from_command(
                    "G1 E-%.4f F%.0f" % (p['retract'], p['flow'] * 60.))
        finally:
            # Release the load-cell collector even if the run aborts before
            # collect_until returns: a collector left "started" stays subscribed
            # and buffers every sample forever (reactor load that builds across
            # runs -> "Timer too close"). collect_until clears is_started on the
            # normal path; this is the abort guard.
            if collector is not None:
                try:
                    collector.is_started = False
                except Exception:
                    logging.exception("autopa decay: collector release failed")
            try:
                self._set_pa(orig_pa)
            except Exception:
                logging.exception("autopa decay: PA restore failed")
            try:
                # never moves XY (relative-E only) and may run un-homed -> no MOVE
                self._restore_gcode_state(move=False)
            except Exception:
                logging.exception("autopa decay: gcode-state restore failed")
        stops.sort()
        meta = self._base_meta(lc, 'decay', gcmd)
        # VFR-native schema: 'vfr' (mm^3/s) is the print-relevant flow the user set;
        # excitation_s = pulse/lin-feed is the melt-pressure charge time. The linear
        # feed is a runtime detail (filament_area in meta lets any tool re-derive it),
        # so it is NOT stored. pulse is in mm filament (geometry, not a rate).
        meta.update({'vfr': p['vfr'],
                     'excitation_s': p['pulse'] / p['flow'],
                     'pulse': p['pulse'], 'off': p['off'],
                     'pulses': p['pulses'], 'pa': p['pa'], 'prime': p['prime'],
                     'warmup': p['warmup'], 'window': p['window'],
                     'retract': p['retract'], 'stops': stops, 'apply': p['apply'],
                     'snrmin': p['snrmin'], 'tailskip': 0.0, 't0': t0,
                     'errs': errs})
        return np.asarray(samples, dtype=float), meta

    def cmd_AUTOPA_DECAY(self, gcmd):
        lc = self._get_load_cell(gcmd)
        toolhead = self.printer.lookup_object('toolhead')
        self._check_extrude_temp(gcmd)
        p = self._decay_params(gcmd)
        # Collect garbage now, while the toolhead is still idle, so a full GC
        # pass doesn't fall in the middle of the timed pulse train below (a GC
        # pause on the reactor thread starves the MCU step queue -> "Timer too
        # close"). The measured run allocates a large sample array.
        import gc
        gc.collect()
        self._set_busy('decay', self._decay_expected_s(p))
        try:
            arr, meta = self._capture_decay(lc, toolhead, p, gcmd)
            # heavy work (fit + capture write) off the reactor (see
            # _run_off_reactor); the report below stays on the reactor thread.
            res, saved = self._run_off_reactor(self._decay_compute, arr, meta)
            if res is None:
                raise gcmd.error("autopa decay: insufficient/unfittable data "
                                 "(errors=%s)" % (meta['errs'],))
            self._report_decay(gcmd, meta, res, saved)
        finally:
            self._clear_busy()

    def _fit_tau(self, te, fe, window, taumax=0.25):
        # fit fe ~ A*exp(-t/tau) + C on the early window te<=`window`; grid tau,
        # linear lstsq for A,C. Returns (tau, amp, rms) of the best positive-
        # amplitude fit. The bounded window suppresses the slow ooze/thermal
        # tail so tau tracks only the fast (PA-relevant) melt relaxation.
        import numpy as np
        m = te <= window
        te, fe = te[m], fe[m]
        if len(te) < 6:
            return None
        best = None
        for tau in np.linspace(0.005, taumax, 300):
            B = np.vstack([np.exp(-te / tau), np.ones_like(te)]).T
            coef, _, _, _ = np.linalg.lstsq(B, fe, rcond=None)
            ss = float(np.sum((fe - B @ coef) ** 2))
            if coef[0] > 0 and (best is None or ss < best[0]):
                best = (ss, float(tau), float(coef[0]))
        if best is None:
            return None
        ss, tau, amp = best
        return tau, amp, (ss / len(fe)) ** 0.5

    def _fold_decay(self, stops, t, force, off, nb=140):
        # Synchronous-average the post-stop decay window over `stops` onto an
        # elapsed-time grid (each pulse zeroed to its own settled tail). Folds
        # from t=0 (stop instant) -- the first ~15ms IS melt relaxation.
        import numpy as np
        edges = np.linspace(0.0, off, nb + 1)
        acc = np.zeros(nb)
        cnt = np.zeros(nb)
        for st in stops:
            m = (t >= st) & (t < st + off)
            if not m.any():
                continue
            tail = (t >= st + off - 0.1) & (t < st + off)
            base = float(force[tail].mean()) if tail.any() else 0.
            idx = np.clip(np.searchsorted(edges, t[m] - st) - 1, 0, nb - 1)
            np.add.at(acc, idx, force[m] - base)
            np.add.at(cnt, idx, 1.)
        g = cnt > 0
        return (0.5 * (edges[:-1] + edges[1:]))[g], acc[g] / cnt[g]

    # Fixed fit window (s) -- the defining convention for the reported tau.
    # An adaptive/auto window was investigated against the sv2 multi-material suite
    # and REJECTED: no fit-free observable reproduces sweep's cross-material spread,
    # and a tail-weighted estimator (tau_area) compressed contrast and broke
    # VFR-monotonicity on oozy PETG (see memory decay-tau-area-rejected). So the
    # window is a fixed value; WINDOW overrides it for A/B testing.
    DEFAULT_WINDOW = 0.14

    def _estimate_decay(self, arr, meta):
        # Pure analysis (no gcmd): MAD-reject bad pulses, fold, early-window
        # exp fit, slow-tail slack, group-spread repeatability, confidence.
        # Returns a result dict or None.
        import numpy as np
        stops, off = meta['stops'], meta['off']
        if len(arr) < 20 or len(stops) < 4:
            return None
        t = arr[:, 0]
        force = -(arr[:, 2] - arr[:, 3])
        # per-pulse decay height -> reject blob-touches / dead pulses (MAD)
        amps = []
        for st in stops:
            e = (t >= st) & (t < st + 0.03)
            l = (t >= st + off - 0.1) & (t < st + off)
            amps.append(float(force[e].mean() - force[l].mean())
                        if e.any() and l.any() else np.nan)
        amps = np.array(amps)
        med = np.nanmedian(amps)
        mad = np.nanmedian(np.abs(amps - med)) + 1e-9
        keep = (np.abs(amps - med) <= 3. * 1.4826 * mad) & (amps > 0) \
            & ~np.isnan(amps)
        good = [stops[i] for i in range(len(stops)) if keep[i]]
        if len(good) < 4:
            return None
        # headline tau: fit the fold of all good pulses (best SNR) on the fixed
        # window (meta['window'] holds the value used, for reproducible replay).
        te, fe = self._fold_decay(good, t, force, off)
        window = meta.get('window') or self.DEFAULT_WINDOW
        r = self._fit_tau(te, fe, window)
        if r is None:
            return None
        tau, amp, rms = r
        snr = amp / rms if rms > 0 else 0.
        # slow-tail slack: residual fold energy just past the fit window,
        # normalized by amp. ~0 for a clean 1st-order decay; >0 => ooze/blob.
        a, b = window, min(window + 0.15, off - 0.05)
        shelf = fe[(te >= a) & (te <= b)]
        slack = (float(np.clip(shelf, 0, None).mean() / amp)
                 if len(shelf) and amp > 0 else 0.)
        # repeatability: split good pulses into groups, fit tau on each fold
        taus = []
        ng = 4 if len(good) >= 8 else 2
        for gi in range(ng):
            grp = good[gi::ng]
            if len(grp) >= 2:
                tg, fg = self._fold_decay(grp, t, force, off)
                rg = self._fit_tau(tg, fg, window)
                if rg is not None and 0.004 < rg[0] < 0.2:
                    taus.append(rg[0])
        spread = float(np.std(taus)) if len(taus) >= 2 else float('nan')
        if snr >= 10 and spread < 0.006 and slack < 0.12:
            conf = "HIGH"
        elif snr >= 5 and (np.isnan(spread) or spread < 0.012) and slack < 0.20:
            conf = "MED"
        else:
            conf = "LOW"
        # Plot-ready fold + fit overlay for the UI. Display-only and additive:
        # the A,C here are the same lstsq _fit_tau solved at the chosen tau
        # (re-run once to recover C, which _fit_tau doesn't return). Callers
        # exclude 'plot' from the saved stats so the schema-v1 layout is kept.
        mw = te <= window
        B = np.vstack([np.exp(-te[mw] / tau), np.ones(int(mw.sum()))]).T
        coef, _, _, _ = np.linalg.lstsq(B, fe[mw], rcond=None)
        plot = {'fold_t': te, 'fold_f': fe, 'tau': float(tau),
                'amp': float(coef[0]), 'c': float(coef[1]),
                'window': float(window)}
        return {'tau': float(tau), 'snr': float(snr), 'spread': spread,
                'slack': float(slack), 'n_used': len(good), 'conf': conf,
                'window_used': float(window), 'plot': plot}

    def _decay_compute(self, arr, meta):
        # Reactor-free: fit + (optionally) write the capture. Runs on the
        # offload worker so the fit grid + savez never block the reactor (see
        # _run_off_reactor). Returns (fit-result-or-None, saved-or-None). The
        # saved stats exclude the UI-only 'plot' to keep the schema-v1 layout;
        # 'plot' stays on the returned result for _report_decay / _last.
        res = self._estimate_decay(arr, meta)
        if res is None:
            return None, None
        saved = None
        if self.save_captures:
            stats = {k: v for k, v in res.items() if k != 'plot'}
            saved = self._write_capture(arr, meta, dict(stats, kind='decay'))
        return res, saved

    def _report_decay(self, gcmd, meta, res, saved):
        plot = res.pop('plot', None)   # UI-only; keep saved stats schema-v1
        tau, snr, spread = res['tau'], res['snr'], res['spread']
        conf, slack = res['conf'], res['slack']
        lines = [
            "autopa decay: %d pulses, %d usable, VFR=%.1f mm³/s, "
            "PA-during=%.3f, errors=%s"
            % (meta['pulses'], res['n_used'], meta['vfr'],
               meta['pa'], meta['errs']),
            "  folded SNR=%.1f, group spread=+/-%.4f, slow-tail slack=%.2f"
            % (snr, spread, slack),
            "  tau = %.4f s  =>  pressure advance = %.4f  [%s confidence]"
            % (tau, tau, conf)]
        if conf == "LOW":
            lines.append("  LOW confidence: raise PULSES/OFF or check for a "
                         "hanging blob / weak signal before trusting this value")
        elif slack >= 0.12:
            lines.append("  note: elevated slow-tail slack -- possible nozzle "
                         "ooze/blob may be inflating tau")
        # apply live (default) + always show the paste-able command
        self._report_applied_pa(lines, tau, meta.get('apply', True))
        path = self._register_capture(saved)
        if path:
            lines.append("capture saved: %s" % path)
        # coerce to native python now (the plot holds numpy arrays); get_status
        # is polled ~4x/s and re-coercing these on every poll -- including during
        # the next run's motion -- is needless reactor work
        self._last = self._native({'decay': {
            'tau': tau, 'spread': spread, 'slack': slack,
            'n_used': res['n_used'], 'confidence': conf,
            'snr': snr, 'plot': plot}})
        self._invalidate_status()
        gcmd.respond_info("\n".join(lines))
