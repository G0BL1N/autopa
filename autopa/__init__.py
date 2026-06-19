# autopa - automatic pressure advance calibration using a load cell
#
# Copyright (C) 2026  autopa contributors
#
# This file may be distributed under the terms of the GNU AGPLv3 (or later)
# license.
#
# Package layout (one Klipper object, split into mixins by concern):
#   __init__.py  - this file: wiring, load-cell resolution, shared toolhead/PA
#                  helpers, capture persistence, get_status, load_config.
#   capture.py   - saved-capture labelling/deletion (AUTOPA_ANNOTATE /
#                  AUTOPA_DELETE) plus the shared capture-index helpers.
#   decay.py     - AUTOPA_DECAY (melt-tau estimator; autopa's own method).
#   sweep.py     - AUTOPA_SWEEP ("Sweep": K-sweep force-tracking estimator and
#                  the recommended method; numpy port of the PrusaPATuner
#                  algorithm; see sweep_analysis.py).
#   profiles.py  - per-(material, temperature) PA profile store + commands.
#
# Each mixin owns its own commands and registers them from __init__ via a
# _register_*_commands hook, so adding a new algorithm is "add a module +
# a mixin + one register call" with no churn to the others.
import os, json, logging, math

from .capture import CaptureMixin
from .decay import DecayMixin
from .sweep import SweepMixin
from .profiles import ProfileMixin

__version__ = "0.2.0"
# Bump whenever the saved-capture metadata layout changes incompatibly; readers can
# branch on meta['schema_version'] (absent => pre-v1 development capture).
#   v2: VFR-native flow schema -- 'vfr'(/excitation_s) on decay, 'vfr'/'vfr_low'
#       on sweep (all mm^3/s), plus filament_area; the linear mm/s feeds are NOT
#       stored (a runtime detail; filament_area re-derives them). Also embeds a
#       precomputed 'detail' npz member (decimated trace + plot) so capture_detail
#       needn't replay the estimator on the reactor. (Captures saved by earlier v2
#       builds carry slow/fast or vfr_slow/vfr_fast; _sweep_lin reads those too.)
SCHEMA_VERSION = 2

# Version of the precomputed capture 'detail' payload (decimated trace + plot).
# SEPARATE from SCHEMA_VERSION because the *code* that produces detail can change
# without the on-disk layout changing: bump this whenever _capture_detail_payload
# would produce a DIFFERENT result for the same samples -- estimator math
# (_estimate_decay / _sweep_analyse), the plot/trace shape, or the decimation.
# _handle_capture_detail recomputes from the (always-retained) samples whenever a
# capture's stored detail version != this, so a stale cache is never served.
DETAIL_VERSION = 1

# Fallback filament cross-section (mm^2) for the vol<->lin conversion when the
# extruder can't be read; 1.75 mm is the common default. The live value comes
# from Klipper's extruder.filament_area (see _filament_area), which is the
# single source of truth so 2.85 mm setups convert correctly.
_DEFAULT_FILAMENT_AREA = math.pi * (1.75 / 2) ** 2


class AutoPA(CaptureMixin, DecayMixin, SweepMixin, ProfileMixin):
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.name = config.get_name()
        # configuration
        self.profile_path = os.path.expanduser(config.get(
            'profile_path', '~/printer_data/autopa/profiles.json'))
        self.capture_dir = os.path.expanduser(config.get(
            'capture_dir', '~/printer_data/autopa/captures'))
        self.save_captures = config.getboolean('save_captures', True)
        # operator's hotend description: hardware rarely changes, so it lives in
        # config and every saved capture inherits it (ANNOTATE overrides it)
        self.hotend_default = config.get('hotend', None)
        # state
        self._load_cell = None
        self._profiles = self._load_profiles()
        self._last = {}
        self._activity = {'state': 'idle'}
        self._captures_index = []
        # get_status is polled ~4x/s; the heavy, slowly-changing fields
        # (captures index, last-run result incl. plot arrays, profiles) are
        # coerced to native once and cached, rebuilt only when they change (see
        # _invalidate_status / get_status). Re-walking them every poll churned
        # allocations on the reactor thread, feeding GC pauses that can stall
        # step generation during a concurrent run ("Timer too close").
        self._status_cached = {}
        self._status_dirty = True
        # The load cell built by [load_cell_probe] is private to the probe and
        # not registered as its own object, so resolve it on ready and also
        # capture it from the events load_cell fires with the instance.
        self.printer.register_event_handler('klippy:ready', self._handle_ready)
        self.printer.register_event_handler('load_cell:tare', self._on_lc_event)
        self.printer.register_event_handler('load_cell:calibrate',
                                            self._on_lc_event)
        # commands -- each mixin registers its own
        self._register_capture_commands()
        self._register_decay_commands()
        self._register_sweep_commands()
        self._register_profile_commands()
        # web API: saved-capture inspection for the SPA (read-only; reached
        # through Moonraker's /klippysocket bridge, same as load_cell/dump_force)
        webhooks = self.printer.lookup_object('webhooks')
        webhooks.register_endpoint('autopa/capture_detail',
                                   self._handle_capture_detail)

    # -- load cell resolution -------------------------------------------------
    def _handle_ready(self):
        # scan saved captures off the ready path; the index feeds get_status
        self.reactor.register_callback(self._refresh_captures_index)
        if self._load_cell is not None:
            return
        probe = self.printer.lookup_object('probe', None)
        if probe is not None and hasattr(probe, '_load_cell'):
            self._load_cell = probe._load_cell
        else:
            lc = self.printer.lookup_object('load_cell', None)
            if lc is not None:
                self._load_cell = lc

    def _on_lc_event(self, load_cell):
        self._load_cell = load_cell

    def _get_load_cell(self, gcmd):
        if self._load_cell is None:
            self._handle_ready()
        if self._load_cell is None:
            raise gcmd.error("autopa: no load cell found; requires "
                             "[load_cell] or [load_cell_probe]")
        return self._load_cell

    # -- shared toolhead / pressure-advance helpers ---------------------------
    def _check_extrude_temp(self, gcmd):
        # Defer to the extruder's own cold-extrude guard (its min_extrude_temp)
        # rather than duplicating that threshold in our config.
        extruder = self.printer.lookup_object('toolhead').get_extruder()
        status = extruder.get_status(self.reactor.monotonic())
        if not status.get('can_extrude', False):
            raise gcmd.error("autopa: extruder too cold to extrude (%.1fC) - "
                             "heat the hotend above its min_extrude_temp first"
                             % (status.get('temperature', 0.),))

    def _get_pa(self):
        extruder = self.printer.lookup_object('toolhead').get_extruder()
        return extruder.get_status(self.reactor.monotonic()).get(
            'pressure_advance', 0.)

    def _set_pa(self, value):
        self.gcode.run_script_from_command(
            "SET_PRESSURE_ADVANCE ADVANCE=%.6f" % value)

    # Save/restore the gcode-move state around a calibration so the operator's
    # coordinate modes survive it: a calibrator flips to absolute XYZ (G90) and
    # relative E (M83), and RESTORE_GCODE_STATE puts G90/G91 + M82/M83 + feedrate
    # back the way they were. (Pressure advance is extruder state, not part of
    # gcode-move state, so each calibrator still restores PA itself.) MOVE=1
    # returns the toolhead to its saved position -- used by the sweep, whose
    # axis wobble leaves it a fraction off; the decay never moves XY and is not
    # always homed, so it restores without a move.
    def _save_gcode_state(self, name='autopa'):
        self.gcode.run_script_from_command("SAVE_GCODE_STATE NAME=%s" % name)

    def _restore_gcode_state(self, name='autopa', move=False):
        self.gcode.run_script_from_command(
            "RESTORE_GCODE_STATE NAME=%s%s" % (name, " MOVE=1" if move else ""))

    # -- activity (UI progress) -----------------------------------------------
    # Calibrators queue their whole move sequence ahead of execution, so live
    # per-step progress isn't meaningful; instead each command declares its
    # expected wall time up front and the UI renders elapsed/expected. Webhooks
    # keep polling get_status while collect_until blocks (the reactor stays
    # live), so this propagates mid-run.
    def _set_busy(self, method, expected_s):
        self._activity = {'state': 'running', 'method': method,
                          'started': float(self.reactor.monotonic()),
                          'expected_s': float(expected_s)}

    def _clear_busy(self):
        self._activity = {'state': 'idle'}

    # The exact line a user pastes into their slicer's filament start-gcode to
    # make a calibrated PA permanent. Kept identical across estimators so the
    # console output is one consistent, copy-pasteable command.
    @staticmethod
    def _pa_command(value):
        return "SET_PRESSURE_ADVANCE ADVANCE=%.4f" % value

    def _report_applied_pa(self, lines, value, apply):
        # Shared tail for every calibrator: optionally apply the result live
        # (so a mid-print run can be ignored and the print just continues), then
        # always print the copy-paste line for the slicer filament profile.
        if apply:
            self._set_pa(value)
            lines.append("  applied live: pressure advance = %.4f "
                         "(active now, for the rest of this print/session)"
                         % value)
        lines.append("  to make it permanent, paste into your slicer's "
                     "filament start g-code:")
        lines.append("      %s" % self._pa_command(value))

    # -- capture metadata -----------------------------------------------------
    # See docs/DATA_FORMAT.md. _base_meta is the single source of the schema-v1
    # auto-captured fields so capture/decay/sweep can't drift; each adds its own
    # timing + command params on top.
    def _software_version(self):
        try:
            return self.printer.get_start_args().get('software_version', '?')
        except Exception:
            return '?'

    def _sensor_type(self, lc):
        sensor = getattr(lc, 'sensor', None)
        return type(sensor).__name__ if sensor is not None else '?'

    def _max_extrude_cross_section(self):
        # Effective max_extrude_cross_section (mm^2): Klipper stores it as
        # max_extrude_ratio = cross_section / filament_area and doesn't surface
        # the original in get_status, so reconstruct it. The UI uses this to show
        # the Sweep guard warning only when it's actually too low. None if the
        # extruder can't be read. (Klipper parses the option with above=0, so it
        # is always > 0 -- there is no "disabled" value; unset == 4*nozzle^2.)
        try:
            extruder = self.printer.lookup_object('toolhead').get_extruder()
            ratio = getattr(extruder, 'max_extrude_ratio', None)
            area = getattr(extruder, 'filament_area', None)
            if ratio is not None and area is not None:
                return ratio * area
        except Exception:
            pass
        return None

    def _filament_area(self):
        # Single source of truth for the volumetric<->linear filament conversion.
        # Klipper derives filament_area from the [extruder] filament_diameter, so
        # using it makes a VFR (mm^3/s) map to the linear feed (mm/s) actually
        # commanded even on 2.85 mm setups. Falls back to 1.75 mm if the extruder
        # can't be read (e.g. early/un-homed calls). Returns mm^2.
        try:
            area = getattr(
                self.printer.lookup_object('toolhead').get_extruder(),
                'filament_area', None)
            if area:
                return float(area)
        except Exception:
            pass
        return _DEFAULT_FILAMENT_AREA

    def _lin_to_vol(self, mm_s):
        # filament feed (mm/s) -> volumetric flow (mm^3/s)
        return float(mm_s) * self._filament_area()

    def _vol_to_lin(self, mm3_s):
        # volumetric flow (mm^3/s, the print-relevant VFR the user sets) ->
        # linear filament feed (mm/s) for the G1 E moves. Inverse of
        # _lin_to_vol, same single source of truth (_filament_area).
        return float(mm3_s) / self._filament_area()

    def _hotend_status(self):
        # (commanded target, measured temperature) in degC, or (None, None).
        try:
            est = self.printer.lookup_object('toolhead').get_extruder() \
                .get_status(self.reactor.monotonic())
            return est.get('target'), est.get('temperature')
        except Exception:
            return None, None

    OPERATOR_FIELDS = ('material', 'brand', 'hotend', 'notes')

    def _operator_meta(self, gcmd):
        # Free-text operator labels. These are read ONLY by AUTOPA_ANNOTATE --
        # the calibration commands deliberately do NOT take them (it keeps the
        # calibration surface to what affects the measurement; filament type
        # comes from the slicer/UI, and labels are filled in afterwards via
        # AUTOPA_ANNOTATE). Material is the slicer-compatible token (PLA);
        # brand is an optional refinement (Prusament). See OPERATOR_FIELDS.
        return {'material': gcmd.get('MATERIAL', None),
                'brand': gcmd.get('BRAND', None),
                'hotend': gcmd.get('HOTEND', None),
                'notes': gcmd.get('NOTES', None)}

    def _base_meta(self, lc, kind, gcmd):
        target, temp = self._hotend_status()
        meta = {'schema_version': SCHEMA_VERSION, 'autopa_version': __version__,
                'klipper_version': self._software_version(), 'kind': kind,
                'sensor': self._sensor_type(lc),
                'sps': lc.sensor.get_samples_per_second(),
                'hotend_target': target, 'hotend_temp': temp,
                'filament_area': self._filament_area()}
        # Operator labels are off the calibration surface; store None
        # placeholders so the schema is stable and AUTOPA_ANNOTATE can fill
        # them in later. The hotend defaults from config (hardware is static).
        meta.update({f: None for f in self.OPERATOR_FIELDS})
        meta['hotend'] = self.hotend_default
        return meta

    # -- capture persistence --------------------------------------------------
    # -- off-reactor analysis/capture ----------------------------------------
    # The post-run estimator replay and capture write are hundreds of ms to
    # several seconds on an SBC host. Run synchronously on the reactor they
    # block the event loop for that whole span, which starves whatever MCU
    # timer next comes due -- crucially the hotend's soft-PWM queue_digital_out,
    # which must be refed continuously whether or not the toolhead is moving --
    # so the MCU shuts down with "Timer too close". The fix is structural, not a
    # matter of doing the work when "idle": the reactor must never block, so the
    # heavy work is offloaded UNCONDITIONALLY (no toolhead-idle gate, which was
    # both ineffective -- the heater starves at standstill too -- and racy).
    def _run_off_reactor(self, fn, *args):
        # Run fn(*args) on a worker thread and park the calling gcode greenlet
        # on a reactor completion: the reactor keeps looping (heater PWM, clock
        # sync, get_status all serviced on time) until the worker finishes.
        # numpy ops and file writes release the GIL, so the worker makes real
        # progress while the reactor runs. fn MUST be reactor-free -- pure
        # compute + file I/O over immutable inputs; anything touching
        # printer/gcode/shared state stays on the reactor, after this returns.
        import threading
        comp = self.reactor.completion()
        def worker():
            try:
                res = (None, fn(*args))
            except BaseException as e:        # deliver any failure to the waiter
                res = (e, None)
            self.reactor.async_complete(comp, res)
        threading.Thread(target=worker, name='autopa-offload',
                         daemon=True).start()
        err, out = comp.wait()
        if err is not None:
            raise err
        return out

    def _write_capture(self, arr, meta, stats):
        # Pure capture write (no shared-state access) -- runs on the offload
        # worker. Precompute the UI detail payload (decimated trace + plot) and
        # embed it so capture_detail never replays the estimator on the reactor
        # (see _capture_detail_payload). Returns (path, summary) or None; the
        # caller records the summary into the index via _register_capture on the
        # reactor.
        import numpy as np
        try:
            if not os.path.exists(self.capture_dir):
                os.makedirs(self.capture_dir)
            import time as _t
            path = os.path.join(self.capture_dir,
                                "capture_%s.npz" % _t.strftime("%Y%m%d-%H%M%S"))
            try:
                detail = json.dumps(self._capture_detail_payload(arr, meta))
            except Exception:
                logging.exception("autopa: failed to precompute capture detail")
                detail = None
            # write to a temp file and rename so a crash mid-write can't leave a
            # half-written capture behind (pass a file object so np.savez writes
            # the exact name, not <name>.npz)
            tmp = path + ".tmp"
            kw = dict(samples=arr, meta=json.dumps(meta), stats=json.dumps(stats))
            if detail is not None:
                kw['detail'] = detail
            with open(tmp, "wb") as fh:
                np.savez(fh, **kw)
            os.replace(tmp, path)
            return path, self._capture_summary(path, meta, stats)
        except Exception:
            logging.exception("autopa: failed to save capture")
            return None

    def _register_capture(self, saved):
        # Reactor-side half of a capture save: record the summary produced off
        # the reactor by _write_capture into the in-memory index. Returns the
        # saved path (or None).
        if not saved:
            return None
        path, summary = saved
        self._captures_index.insert(0, summary)
        del self._captures_index[self.CAPTURES_INDEX_MAX:]
        self._invalidate_status()
        return path

    def _find_capture(self, capture):
        # Map a capture reference to a saved .npz path: 'latest' (or empty)
        # picks the newest capture in capture_dir; otherwise a bare name
        # (+/- .npz, looked up in capture_dir) or an absolute path. Raises
        # ValueError when unresolvable so both gcode (_resolve_capture) and
        # webhook callers can wrap it.
        import glob
        if not capture or capture == 'latest':
            files = sorted(glob.glob(os.path.join(self.capture_dir, "capture_*.npz")))
            if not files:
                raise ValueError("autopa: no saved captures in %s" % self.capture_dir)
            return files[-1]
        for cand in (capture, os.path.join(self.capture_dir, capture),
                     os.path.join(self.capture_dir, capture + ".npz")):
            if os.path.exists(cand):
                return cand
        raise ValueError("autopa: capture not found: %s" % capture)

    def _resolve_capture(self, gcmd, capture):
        try:
            return self._find_capture(capture)
        except ValueError as e:
            raise gcmd.error(str(e))

    # -- captures index (UI capture browser) ----------------------------------
    # Scalar per-capture summaries only -- get_status is polled ~4x/s, so the
    # index is cached: built once on ready, then maintained by _register_capture
    # and AUTOPA_ANNOTATE. Full traces/plots come from the capture_detail
    # endpoint.
    CAPTURES_INDEX_MAX = 50

    @staticmethod
    def _capture_time(path):
        # capture_%Y%m%d-%H%M%S.npz encodes the capture time; mtime is wrong
        # after AUTOPA_ANNOTATE rewrites the file.
        import time as _t
        try:
            return _t.mktime(_t.strptime(os.path.basename(path),
                                         "capture_%Y%m%d-%H%M%S.npz"))
        except Exception:
            return os.path.getmtime(path)

    @staticmethod
    def _capture_summary(path, meta, stats):
        kind = meta.get('kind')
        result = {'decay': stats.get('tau'),
                  'sweep': stats.get('k_opt')}.get(kind)
        if result is not None and result != result:    # NaN -> JSON-safe
            result = None
        # VFR (mm^3/s): decay uses its single 'vfr'; sweep reports the fast/
        # calibration leg ('vfr'). Older captures carry vfr_fast or a linear feed
        # (flow/fast mm/s) -- derive mm^3/s from filament_area when needed.
        area = meta.get('filament_area')
        if kind == 'decay':
            vfr = meta.get('vfr')
            if vfr is None and meta.get('flow') is not None and area:
                vfr = meta['flow'] * area
        elif kind == 'sweep':
            vfr = meta.get('vfr', meta.get('vfr_fast'))
            if vfr is None and meta.get('fast') is not None and area:
                vfr = meta['fast'] * area
        else:
            vfr = None
        return {'file': os.path.basename(path), 'kind': kind,
                'time': AutoPA._capture_time(path),
                'material': meta.get('material'), 'brand': meta.get('brand'),
                'hotend': meta.get('hotend'), 'notes': meta.get('notes'),
                'hotend_target': meta.get('hotend_target'),
                'vfr': float(vfr) if vfr is not None else None,
                'result': result, 'confidence': stats.get('conf')}

    def _refresh_captures_index(self, eventtime=None):
        import glob
        import numpy as np
        files = sorted(glob.glob(os.path.join(self.capture_dir, "capture_*.npz")),
                       reverse=True)[:self.CAPTURES_INDEX_MAX]
        index = []
        for path in files:
            try:
                data = np.load(path, allow_pickle=False)
                meta = json.loads(str(data['meta']))
                stats = (json.loads(str(data['stats']))
                         if 'stats' in data.files else {})
                index.append(self._capture_summary(path, meta, stats))
            except Exception:
                logging.exception("autopa: skipping unreadable capture %s" % path)
        self._captures_index = index
        self._invalidate_status()

    # -- capture detail endpoint (UI capture inspection) ----------------------
    @staticmethod
    def _decimate_minmax(t, y, max_pts=4000):
        # Per-bucket min+max decimation: keeps the visual envelope of the raw
        # trace intact at a payload the browser can take over the bridge.
        import numpy as np
        n = len(t)
        if n <= max_pts:
            return t, y
        edges = np.linspace(0, n, max_pts // 2 + 1).astype(int)
        keep = []
        for a, b in zip(edges[:-1], edges[1:]):
            if b > a:
                seg = y[a:b]
                keep.extend({a + int(np.argmin(seg)), a + int(np.argmax(seg))})
        keep = sorted(set(keep))
        return t[keep], y[keep]

    def _capture_detail_payload(self, arr, meta):
        # The UI capture view's heavy parts: a decimated raw force trace plus the
        # kind-specific plot data, produced by replaying the saved samples
        # through the SAME estimator that produced the capture. Computed once at
        # save time (toolhead idle) and embedded in the .npz so capture_detail is
        # a cheap read -- replaying it on the reactor thread on every UI open is a
        # ~0.1-1s stall that can starve step generation ("Timer too close").
        # 'v' stamps the algorithm/format version so a later code change
        # invalidates this cache (see DETAIL_VERSION / _handle_capture_detail).
        out = {'v': DETAIL_VERSION, 'trace': None, 'plot': None}
        if not len(arr):
            return out
        t0 = meta.get('t0')
        t_rel = arr[:, 0] - (float(t0) if t0 is not None else arr[0, 0])
        force = -(arr[:, 2] - arr[:, 3])
        dt, df = self._decimate_minmax(t_rel, force)
        out['trace'] = {'t': dt, 'force': df}
        try:
            kind = meta.get('kind')
            if kind == 'decay':
                res = self._estimate_decay(arr, meta)
                if res is not None:
                    plot = res.pop('plot', None) or {}
                    plot['conf'] = res.get('conf')
                    out['plot'] = plot
            elif kind == 'sweep':
                res = self._sweep_analyse(arr, meta)
                out['plot'] = {'per_k': self._sweep_per_k(res),
                               'k_opt': res.bd_k_opt}
        except Exception:
            logging.exception("autopa: capture_detail payload compute failed")
        return self._native(out)

    def _handle_capture_detail(self, web_request):
        # webhooks endpoint autopa/capture_detail
        # {"capture": "latest"|<name>|<path>}: meta + stats + decimated raw
        # trace + the kind-specific plot data. Served from the precomputed
        # 'detail' member embedded at save time; captures that lack it (pre-v2)
        # or whose stored detail predates the current algorithm/format
        # (DETAIL_VERSION) are recomputed from the retained samples.
        import numpy as np
        capture = web_request.get_str('capture', 'latest')
        try:
            path = self._find_capture(capture)
        except ValueError as e:
            raise web_request.error(str(e))
        try:
            with np.load(path, allow_pickle=False) as data:
                meta = json.loads(str(data['meta']))
                stats = (json.loads(str(data['stats']))
                         if 'stats' in data.files else {})
                payload = None
                if 'detail' in data.files:
                    cached = json.loads(str(data['detail']))
                    if cached.get('v') == DETAIL_VERSION:    # fresh: cheap read
                        payload = cached
                if payload is None:               # absent/stale: replay samples
                    arr = np.asarray(data['samples'], dtype=float)
                    payload = self._capture_detail_payload(arr, meta)
        except Exception as e:
            raise web_request.error("autopa: unreadable capture %s: %s"
                                    % (capture, e))
        detail = {'file': os.path.basename(path), 'meta': meta, 'stats': stats,
                  'trace': payload.get('trace'), 'plot': payload.get('plot')}
        web_request.send(self._native(detail))

    # -- status ---------------------------------------------------------------
    @staticmethod
    def _native(obj):
        # Klipper's webhook JSON encoder rejects numpy scalars/arrays (Python's
        # json accepts np.float64 as a float subclass, Klipper's does not), so
        # coerce everything get_status exposes to native python types.
        import numpy as np
        if isinstance(obj, dict):
            return {k: AutoPA._native(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [AutoPA._native(v) for v in obj]
        if isinstance(obj, np.generic):
            return obj.item()
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return obj

    def _invalidate_status(self):
        # Mark the cached get_status payload stale after mutating profiles, the
        # captures index, or the last-run result, so the next poll rebuilds it.
        self._status_dirty = True

    def get_status(self, eventtime):
        # 'now' shares _set_busy's clock so the UI can compute elapsed time
        # without syncing to the host; load_cell_name is the dump_force mux key.
        # The captures/last/profiles block changes only on a run or edit, so
        # coerce it to native once per change instead of on every ~4 Hz poll
        # (see __init__ for why -- avoids reactor-thread allocation churn).
        if self._status_dirty:
            self._status_cached = self._native({'profiles': self._profiles,
                                                 'captures': self._captures_index,
                                                 'last': self._last})
            self._status_dirty = False
        # Only the small, always-changing scalars are rebuilt per poll; the
        # cached block is shared by reference (a fresh outer dict each call keeps
        # Klipper's subscription diffing correct for the scalars).
        return dict(self._status_cached,
                    version=__version__,
                    has_load_cell=self._load_cell is not None,
                    load_cell_name=getattr(self._load_cell, 'name', None),
                    max_extrude_cross_section=self._max_extrude_cross_section(),
                    filament_area=self._filament_area(),
                    activity=dict(self._activity, now=float(eventtime)))


def load_config(config):
    return AutoPA(config)
