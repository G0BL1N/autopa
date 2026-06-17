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
        "line. Args: FLOW PULSE OFF PULSES PA PRIME WARMUP WINDOW SNRMIN "
        "MAXFILAMENT "
        "APPLY.")
    def _decay_params(self, gcmd):
        # parsing for AUTOPA_DECAY
        p = {
            # FLOW=7.5 mm/s (== 18 mm³/s for 1.75 mm filament, the web UI's
            # volumetric default; matches the sweep fast leg), PULSE=2 mm
            # (~0.25s extrusion) is the canonical point: short, print-like
            # excitation makes the measured tau track sweep (and the true PA)
            # across materials/temps. Longer extrusion (lower flow or longer
            # pulse) over-excites the slow ooze mode and inflates tau -- see
            # docs/CALIBRATION.md.
            'flow': gcmd.get_float('FLOW', 7.5, above=0.),      # mm/s filament
            'pulse': gcmd.get_float('PULSE', 2.0, above=0.),    # mm per pulse
            'off': gcmd.get_float('OFF', 0.5, above=0.1, maxval=5.),
            'pulses': gcmd.get_int('PULSES', 20, minval=3, maxval=200),
            'pa': gcmd.get_float('PA', 0.0, minval=0.),
            # early-window exp fit length (s). The post-stop force is bi-modal:
            # a FAST melt-pressure relaxation (= Klipper's 1st-order PA tau) plus
            # a SLOW ooze/thermal tail. A full-window single-exp gets dragged up
            # by the tail (tau 0.04->0.066 run-to-run); fitting only the first
            # ~3 tau isolates the PA-relevant fast mode and ~halves the spread.
            # Fixed measurement window (s) -- NOT a plastic param but a property
            # of the measurement: watch ~4-6x tau of the decay. 0.14s suits the
            # PLA/PETG range (tau 0.024-0.040). Raise it for exotic long-tau
            # materials; auto-scaling was rejected (it tracks the mean but adds
            # run-to-run variance, and can't be validated without non-PLA data).
            'window': gcmd.get_float('WINDOW', 0.14, above=0.02, maxval=0.5),
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
        meta.update({'flow': p['flow'], 'pulse': p['pulse'], 'off': p['off'],
                     'pulses': p['pulses'], 'pa': p['pa'], 'prime': p['prime'],
                     'warmup': p['warmup'], 'window': p['window'],
                     'stops': stops, 'apply': p['apply'], 'snrmin': p['snrmin'],
                     'tailskip': 0.0, 't0': t0, 'errs': errs})
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
            res = self._estimate_decay(arr, meta)
            if res is None:
                raise gcmd.error("autopa decay: insufficient/unfittable data "
                                 "(errors=%s)" % (meta['errs'],))
            self._report_decay(gcmd, arr, meta, res)
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

    def _estimate_decay(self, arr, meta):
        # Pure analysis (no gcmd): MAD-reject bad pulses, fold, early-window
        # exp fit, slow-tail slack, group-spread repeatability, confidence.
        # Returns a result dict or None.
        import numpy as np
        stops, off = meta['stops'], meta['off']
        window = meta.get('window', 0.14)
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
        # headline tau: fit the fold of all good pulses (best SNR)
        te, fe = self._fold_decay(good, t, force, off)
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
        # pop 'plot' before _save_capture so the schema-v1 stats are unchanged.
        mw = te <= window
        B = np.vstack([np.exp(-te[mw] / tau), np.ones(int(mw.sum()))]).T
        coef, _, _, _ = np.linalg.lstsq(B, fe[mw], rcond=None)
        plot = {'fold_t': te, 'fold_f': fe, 'tau': float(tau),
                'amp': float(coef[0]), 'c': float(coef[1]),
                'window': float(window)}
        return {'tau': float(tau), 'snr': float(snr), 'spread': spread,
                'slack': float(slack), 'n_used': len(good), 'conf': conf,
                'plot': plot}

    def _report_decay(self, gcmd, arr, meta, res):
        plot = res.pop('plot', None)   # UI-only; keep saved stats schema-v1
        tau, snr, spread = res['tau'], res['snr'], res['spread']
        conf, slack = res['conf'], res['slack']
        lines = [
            "autopa decay: %d pulses, %d usable, flow=%.1f mm/s, PA-during="
            "%.3f, errors=%s" % (meta['pulses'], res['n_used'], meta['flow'],
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
        if self.save_captures:
            path = self._save_capture(arr, meta, dict(kind='decay', **res))
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
