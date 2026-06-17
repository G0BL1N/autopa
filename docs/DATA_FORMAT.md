# autopa capture data format

Every autopa measurement can be saved as a single `.npz` file (enabled by
default; see `save_captures` in [`CONFIG.md`](../CONFIG.md)). One file is fully
self-describing — samples *and* metadata travel together — so a capture can be
replayed offline or added to the baseline library without any companion file.

## Measurement kinds

The `kind` field records which command produced the capture:

| `kind`    | command          | what it does |
|-----------|------------------|--------------|
| `sweep`   | `AUTOPA_SWEEP`   | K-sweep force-tracking estimator (numpy port of PrusaPATuner). Drives a slow/fast extrusion square wave across a grid of PA values and finds the K that best tracks commanded flow. The recommended calibration path. |
| `decay`   | `AUTOPA_DECAY`   | Pulse-and-stop melt-relaxation estimator (experimental). Fits the post-stop force decay `~exp(-t/tau)`; `tau` is the optimal pressure-advance estimate. |

## File layout

`np.load(path)` yields three arrays:

| key       | type            | contents |
|-----------|-----------------|----------|
| `samples` | float `(N, 4)`  | per-sample `[time_s, grams, counts, tare]`. Analysis uses raw `counts - tare`; grams are approximate. |
| `meta`    | JSON string     | how the capture was produced (see schema below) |
| `stats`   | JSON string     | the computed result (per `kind`: `tau`/`conf` for decay, `*_k_opt` for sweep) |

`meta` and `stats` are stored as JSON strings (not pickled), so the files load
with `allow_pickle=False`.

## Metadata schema (`schema_version: 1`)

### Auto-captured — always present

The printer knows these, so they're recorded with no operator input:

| field            | meaning |
|------------------|---------|
| `schema_version` | metadata schema version (currently `1`) |
| `autopa_version` | autopa package version that wrote the file |
| `klipper_version`| Klipper `software_version` string |
| `kind`           | `decay` \| `sweep` |
| `sensor`         | load-cell sensor class (e.g. `HX711`, `ADS1220`) |
| `sps`            | sensor samples per second |
| `hotend_target`  | commanded hotend temperature (°C) at capture |
| `hotend_temp`    | *measured* hotend temperature (°C) at capture |

Plus per-`kind` timing and command parameters (e.g. decay's `flow`/`pulse`/
`pulses`/`stops`, sweep's `ks`/`windows`/`transitions`).

### Operator-supplied — optional, fill in any time

These describe the setup and can't be derived from the printer. **All are
optional** — omit any or all at capture time:

| field      | meaning |
|------------|---------|
| `material` | filament material token, e.g. `PLA`, `PETG` — the slicer-compatible name (slicers export `filament_type`) |
| `brand`    | optional filament brand refinement, e.g. `Prusament`. Kept separate from `material` so the material stays g-code-friendly; code combines them material-first (profile keys become e.g. `PLA PRUSAMENT@220`, so profile lists group by material). |
| `hotend`   | free-text hotend/nozzle description. No fixed format — hotend implementations vary too much to constrain. e.g. `"Volcano, brass heatblock, CHC steel nozzle"`, `"stock Qidi Q2 + brass nozzle"`. Defaults from the `hotend:` option in `[autopa]` (hardware is static — set it once in config). |
| `notes`    | free-text |

Fill them in after the fact with `AUTOPA_ANNOTATE` (the calibration commands
themselves no longer take labels):

```
AUTOPA_ANNOTATE CAPTURE=latest MATERIAL=PLA BRAND=Prusament NOTES="..."
```

A saved capture can be removed with `AUTOPA_DELETE CAPTURE=<filename>` (the
capture name is required — there is deliberately no `latest` default for an
irreversible command).

`CAPTURE=latest` targets the most recently saved capture; otherwise pass a
filename (with or without `.npz`, resolved inside the configured capture
directory) or an absolute path. Only the fields you pass are changed; the samples
and stats are preserved untouched.

### Deliberately not recorded

Bed temperature and extruder name are **not** saved — they're usually unknown or
irrelevant for the in-air extrusion these captures use. The hotend *temperature*
is already covered by `hotend_target`/`hotend_temp`, so there's no separate
operator temperature field.

Live pressure advance is likewise not stored as ambient metadata, because the
captures that depend on it already record the PA they *commanded*: `decay` forces
`pa` (the test PA, default 0), and `sweep` records `orig_pa` plus the swept `ks`.

## Baseline sample library (`captures/`)

[`captures/`](../captures/) is a small, version-controlled set of hand-vetted
captures used to regression-test the analysis offline (no hardware needed) and to
fine-tune the estimators against known cases.

The library grows by **cherry-picking diverse, documented captures** — different
materials, temperatures, sensors, both methods, and deliberately-pathological
cases — rather than bulk-importing similar ones. Every committed sample is listed
in [`captures/README.md`](../captures/README.md) with its provenance and expected
result, and is a schema-v1 `.npz`.

---

**See also:** [README](../README.md) · [Calibration methods](CALIBRATION.md) ·
[Baseline captures](../captures/README.md)
