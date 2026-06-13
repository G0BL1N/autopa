# Baseline capture samples

A small, curated set of real load-cell captures committed to the repo so the
analysis can be regression-tested offline (no printer required) and the
estimators can be tuned against known cases. The printer writes its own
captures to `~/printer_data/autopa/captures/`; the ones here are a curated
subset committed to the repo as test fixtures.

Each file is a schema-v1 capture `.npz` — see [`docs/DATA_FORMAT.md`](../docs/DATA_FORMAT.md)
for the layout and metadata fields.

## What belongs here

The value of this set is **diversity**, not volume. A good baseline spans:

- both algorithms (`decay` and `sweep`),
- a range of materials, temperatures, and hotend/nozzle hardware,
- different load-cell sensors and sample rates.

Every sample here is listed in the catalogue below with its provenance and the
expected result, so a regression can assert against it.

## Sharing a capture (bug reports)

If a calibration misbehaves, the saved `.npz` is the single most useful thing you
can share — it lets the problem be reproduced offline. Before sharing one:

- **Label it correctly.** The `hotend` description is recorded into a capture *at
  capture time* from your `[autopa]` config, so **set `hotend` before the run**; if
  it was missing or wrong, fix it afterwards. Add the material and any notes too.
  Annotate through the **web UI's capture view** (easiest to fill in and to verify it
  reads back right) or with `AUTOPA_ANNOTATE`, and double-check the labels before
  sharing. See [`docs/DATA_FORMAT.md`](../docs/DATA_FORMAT.md) for the fields.
- **Don't commit it here.** Upload the file somewhere and **link it in a
  [GitHub issue](https://github.com/G0BL1N/autopa/issues)** instead. This `captures/`
  directory is a small, hand-curated regression fixture set, not a drop box — only
  vetted, catalogued samples are committed.

## Catalogue

Each entry records the file, its provenance, and the expected result. All four
below are from the same rig — *Mellow Fly ALPS* (`ADS131M0X`, ~488 sps) on a
Volcano NCB 0.4 mm nozzle — and the same FDPlast spool, which was **old and not
dried** (some wet-filament variance is expected and intentional in these
fixtures). They span both methods across PLA and PETG.

| file | kind | material | hotend / nozzle | temp (°C) | expected result | notes |
|------|------|----------|-----------------|-----------|-----------------|-------|
| `capture_20260613-122757.npz` | sweep | PLA (FDPlast)  | Volcano NCB 0.4 mm | 210 | k ≈ 0.033 | clean long sweep |
| `capture_20260613-123718.npz` | decay | PLA (FDPlast)  | Volcano NCB 0.4 mm | 210 | τ ≈ 0.031, conf HIGH | flow 8, well-driven |
| `capture_20260613-130126.npz` | sweep | PETG (FDPlast) | Volcano NCB 0.4 mm | 235 | k ≈ 0.034 | clean long sweep |
| `capture_20260613-142619.npz` | decay | PETG (FDPlast) | Volcano NCB 0.4 mm | 255 | τ ≈ 0.030, conf HIGH | high-temp PETG |

---

**See also:** [README](../README.md) · [Capture data format](../docs/DATA_FORMAT.md) ·
[Calibration methods](../docs/CALIBRATION.md)
