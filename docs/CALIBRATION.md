# Calibration methods

**autopa** tunes Klipper's [Pressure Advance](https://www.klipper3d.org/Pressure_Advance.html)
by *measuring the melt directly* with a toolhead load cell — there are no test prints to
slice, print, and squint at. You park the toolhead, run a short in-air extrusion routine,
and autopa reports the pressure-advance value that best fits the measured force.

Two methods ship, and they share the same workflow (measure → apply live → print a
paste-able `SET_PRESSURE_ADVANCE` line → save the raw capture):

| Method | Command | Status | What it measures |
|--------|---------|--------|------------------|
| **Sweep** | `AUTOPA_SWEEP` | Ready | How cleanly the extruder force tracks a velocity step across a range of PA values |
| **Decay** | `AUTOPA_DECAY` | Experimental | The melt-pressure relaxation time after the filament stops |

You pick the method yourself — in the UI, or by naming the command in your own macro. It
never chooses for you. Both apply the result live by default (`APPLY=0` to measure without
changing state).

> **New to pressure advance?** Klipper's own
> [Pressure Advance guide](https://www.klipper3d.org/Pressure_Advance.html) explains what
> the value does and why it matters for corners and seams. It just finds the number for you
> from a sensor instead of a printed pattern.

## Sweep (recommended)

**The idea.** Pressure advance works by *predicting* how nozzle pressure should respond
when the commanded flow changes. If PA is tuned well, a sudden slow→fast→slow change in
extrusion tracks cleanly — no pressure overshoot on acceleration, no lingering ooze on
deceleration. Sweep drives exactly that asymmetric **slow/fast square-wave** in the air at
a parked position, repeats it across a grid of PA (`K`) values, and measures the load-cell
force at each one. For every `K` it scores the **step response** — overshoot, undershoot,
rise/fall error, settling — and the optimal `K` is the one that minimises that cost.

**The axis "wobble".** Klipper only applies pressure advance to moves that also have X or Y
motion; a pure extrude-only move leaves PA inert. So Sweep couples a tiny axis nudge
(default 0.05 mm on Y) to each slow↔fast transition purely to satisfy that firmware gate —
it isn't meaningful toolhead motion. If you ever see Sweep report a flat or nonsensical
result, a disabled or zeroed wobble is the first thing to check.

**Command.**

```gcode
AUTOPA_SWEEP            ; park the toolhead first, hotend at temperature
```

Useful parameters (all optional; defaults in parentheses):

| Param | Default | Meaning |
|-------|---------|---------|
| `KSTART` / `KEND` / `KSTEP` | `0.01` / `0.08` / `0.01` | PA grid to sweep (brackets common PLA/PETG; the report warns if the optimum lands on an edge — widen the range and re-run) |
| `VFR` / `VFR_LOW` | `18` / `2` mm³/s | fast (calibration) and slow (baseline) leg **volumetric flow rate**; the firmware converts to the linear feed using your real `filament_area`. `VFR_LOW` is an independent low baseline (not a ratio of `VFR`) — keep it low so each fast leg is a clean step up |
| `TSLOW` / `TFAST` | `1.0` / `0.25` s | duration of each leg |
| `CYCLES` | `8` | square-wave cycles measured per `K` |
| `PRIME` / `RETRACT` | `20` / `6` mm | bulk melt prime before the sweep; retract after, so the run leaves no oozing blob |
| `WOBBLEAXIS` / `WOBBLE` | `Y` / `0.05` mm | axis nudge that arms the PA gate |
| `ACCEL` | `1000` mm/s² | acceleration for the wobble move |
| `MAXFILAMENT` | `400` mm | safety cap on total filament used |
| `APPLY` | `1` | apply the found `K` live; `APPLY=0` to only measure |

> **Reading flow in mm³/s.** `VFR`/`VFR_LOW` (and Decay's `VFR`) are *volumetric* flow,
> which is how slicers think — `volumetric = line width × layer height × print speed`. For a
> 0.4 × 0.2 mm line that's ≈ 0.4 mm³/s at 5 mm/s (slow detail), ≈ 6 mm³/s at 80 mm/s,
> ≈ 16 mm³/s at 200 mm/s. Most stock hotends top out around 10–15 mm³/s and high-flow ones
> around 25–30, so the `2 → 18` slow/fast span deliberately reaches from slow detail flow up
> to a strong fast step. The firmware converts mm³/s to the linear filament feed using your
> printer's real `filament_area` (so 2.85 mm setups work too).

### Where Sweep comes from

Sweep is a faithful, numpy-only port of **[PrusaPATuner](https://github.com/CNCKitchen/PrusaPATuner)**
by **CNCKitchen** (Stefan Hermann), which calibrates pressure advance on Prusa printers
from their nozzle load cell; autopa reuses PrusaPATuner's measurement approach and its
analysis math, adapted to run as a Klipper extra against any Klipper `load_cell`. Because
PrusaPATuner is licensed **AGPL-3.0**, autopa is too — see [CREDITS.md](../CREDITS.md) and
[LICENSE](../LICENSE).

PrusaPATuner in turn credits **[Snapmaker U1](https://github.com/Snapmaker/u1-klipper)** for
the in-air slow/fast square-wave motion geometry, so that lineage carries through to autopa.

### Why only one estimator

PrusaPATuner computes three estimators side by side and cross-checks them: a step-response
cost, a phase-lag cross-correlation, and an integral-area fit; autopa keeps **only the
step-response estimator** — PrusaPATuner's own intended primary. On a general load cell of
modest sensitivity (autopa's validation hardware), the phase-lag and integral-area
estimators proved too noisy to trust: on the reference PLA capture, for example, the
integral fit confidently reports a badly wrong value. Rather than surface three numbers and
ask the operator to referee, autopa ships the one that holds up. (The raw capture is always
saved, so other estimators can still be explored offline.)

## Decay (experimental)

**The idea.** Klipper models nozzle pressure as a first-order system with a single time
constant. Right after extrusion stops, the residual pressure bleeds out through the nozzle
and the back-pressure on the load cell relaxes roughly as `force ≈ A·exp(-t/τ) + C`. That
melt time constant **τ is the optimal pressure-advance value** — measured directly, with no
sweep and no model of corner behaviour. Decay primes the melt, then at PA = 0 it repeats a
short pulse-and-stop many times, folds (synchronously averages) the post-stop decay windows
together to beat the noise floor, fits τ on each pulse, and takes a robust median.

**Status: experimental.** Decay is autopa's own method and it's faster than Sweep. The
underlying physics is sound — at the default settings the two have agreed across PLA and PETG
and across temperature (when the hotend gets hotter, both report a lower value together). But
the estimator is still under active development and validation, so its behaviour and results
can change between versions. Treat Decay as experimental, and reach for **Sweep** when you
want a result to rely on.

**Why the defaults are what they are.** Decay's τ is only equal to the pressure-advance value
when the measurement *excites the melt like a print does* — a short, fast flow step. A real
extruder isn't a perfect first-order system: on top of the fast, PA-relevant melt relaxation
there's a slow ooze/compliance tail, and a **longer** extrusion (lower `VFR` or longer
`PULSE`) over-excites that slow tail and inflates τ. So the result depends on *excitation
duration* — but `PULSE` auto-scales with the flow to hold it fixed (≈ 0.27 s) as you change
`VFR`, so the protocol stays standardized (like quoting viscosity at a defined shear rate)
without manual co-tuning. The canonical operating point is `VFR` 18 mm³/s.

**Command.**

```gcode
AUTOPA_DECAY           ; park the toolhead first, hotend at temperature
```

Useful parameters (defaults in parentheses): `VFR` (18 mm³/s volumetric flow), `PULSE`
(auto ≈ 2 mm per pulse, scaled with the flow to hold the excitation duration), `OFF` (0.5 s
stop dwell), `PULSES` (20), `PRIME` (20 mm continuous bulk prime), `RETRACT` (6 mm end
retract so the run leaves no oozing blob), `WARMUP` (10 unrecorded pulses at the measurement
cadence — together with `PRIME` they put the first measured pulse in the pulsed steady
state), `WINDOW` (0.14 s decay-fit cutoff), `MAXFILAMENT` (250 mm), `APPLY` (`1`).

## Applying and storing results

Both methods apply the value live and print a `SET_PRESSURE_ADVANCE ADVANCE=…` line you can
paste straight into your slicer's filament start g-code. It can also remember calibrated
values per **(material, temperature)** so a re-print never needs re-calibration, and recall
them automatically from the slicer. The four supported workflows — calibrate every print,
calibrate only when a value is missing, paste-only, and save + recall — are laid out with
ready-to-use macros in [`CONFIG.md`](../CONFIG.md).

Every run is saved as a self-describing `.npz` capture for offline inspection, comparison,
and regression testing — see [docs/DATA_FORMAT.md](DATA_FORMAT.md).

---

**See also:** [README](../README.md) · [Hardware: ALPS load cell](ALPS.md) ·
[Using the ALPS as a Z-probe](PROBE.md) · [Capture data format](DATA_FORMAT.md) ·
[Credits & licensing](../CREDITS.md)
