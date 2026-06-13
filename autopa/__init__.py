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
import os, json, logging

from .capture import CaptureMixin
from .decay import DecayMixin
from .sweep import SweepMixin
from .profiles import ProfileMixin

__version__ = "0.1.0"
# Bump whenever the saved-capture metadata layout changes incompatibly; readers can
# branch on meta['schema_version'] (absent => pre-v1 development capture).
SCHEMA_VERSION = 1


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
                'hotend_target': target, 'hotend_temp': temp}
        # Operator labels are off the calibration surface; store None
        # placeholders so the schema is stable and AUTOPA_ANNOTATE can fill
        # them in later. The hotend defaults from config (hardware is static).
        meta.update({f: None for f in self.OPERATOR_FIELDS})
        meta['hotend'] = self.hotend_default
        return meta

    # -- capture persistence --------------------------------------------------
    def _save_capture(self, arr, meta, stats):
        import numpy as np
        try:
            if not os.path.exists(self.capture_dir):
                os.makedirs(self.capture_dir)
            import time as _t
            path = os.path.join(self.capture_dir,
                                "capture_%s.npz" % _t.strftime("%Y%m%d-%H%M%S"))
            np.savez(path, samples=arr,
                     meta=json.dumps(meta), stats=json.dumps(stats))
            self._captures_index.insert(0, self._capture_summary(path, meta, stats))
            del self._captures_index[self.CAPTURES_INDEX_MAX:]
            return path
        except Exception:
            logging.exception("autopa: failed to save capture")
            return None

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
    # index is cached: built once on ready, then maintained by _save_capture
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
        return {'file': os.path.basename(path), 'kind': kind,
                'time': AutoPA._capture_time(path),
                'material': meta.get('material'), 'brand': meta.get('brand'),
                'hotend': meta.get('hotend'), 'notes': meta.get('notes'),
                'hotend_target': meta.get('hotend_target'),
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

    def _handle_capture_detail(self, web_request):
        # webhooks endpoint autopa/capture_detail
        # {"capture": "latest"|<name>|<path>}: meta + stats + decimated raw
        # trace + the kind-specific plot data, recomputed by replaying the saved
        # samples through the SAME estimator code that produced the capture
        # (nothing is re-implemented client-side).
        import numpy as np
        capture = web_request.get_str('capture', 'latest')
        try:
            path = self._find_capture(capture)
        except ValueError as e:
            raise web_request.error(str(e))
        try:
            data = np.load(path, allow_pickle=False)
            arr = np.asarray(data['samples'], dtype=float)
            meta = json.loads(str(data['meta']))
            stats = (json.loads(str(data['stats']))
                     if 'stats' in data.files else {})
        except Exception as e:
            raise web_request.error("autopa: unreadable capture %s: %s"
                                    % (capture, e))
        detail = {'file': os.path.basename(path), 'meta': meta, 'stats': stats,
                  'trace': None, 'plot': None}
        if len(arr):
            t0 = meta.get('t0')
            t_rel = arr[:, 0] - (float(t0) if t0 is not None else arr[0, 0])
            force = -(arr[:, 2] - arr[:, 3])
            dt, df = self._decimate_minmax(t_rel, force)
            detail['trace'] = {'t': dt, 'force': df}
            try:
                kind = meta.get('kind')
                if kind == 'decay':
                    res = self._estimate_decay(arr, meta)
                    if res is not None:
                        plot = res.pop('plot', None) or {}
                        plot['conf'] = res.get('conf')
                        detail['plot'] = plot
                elif kind == 'sweep':
                    res = self._sweep_analyse(arr, meta)
                    detail['plot'] = {'per_k': self._sweep_per_k(res),
                                      'k_opt': res.bd_k_opt}
            except Exception:
                logging.exception("autopa: capture_detail replay failed for %s"
                                  % path)
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

    def get_status(self, eventtime):
        # 'now' shares _set_busy's clock so the UI can compute elapsed time
        # without syncing to the host; load_cell_name is the dump_force mux key.
        return self._native({'version': __version__,
                             'has_load_cell': self._load_cell is not None,
                             'load_cell_name': getattr(self._load_cell, 'name',
                                                       None),
                             'activity': dict(self._activity,
                                              now=float(eventtime)),
                             'profiles': self._profiles,
                             'captures': self._captures_index,
                             'last': self._last})


def load_config(config):
    return AutoPA(config)
