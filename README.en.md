# 3d-print-quote

English | [简体中文](README.md)

> **Data source**: All material prices, industry figures (e.g. the ¥0.1/g print-farm price) and default values in this skill were sampled from the **mainland China** market in October 2026. Material densities are physical constants and transferable. Prices are not — replace them with your own supplier quotes before use.

An AI skill for 3D printing quotes. Hand it a model file, get back cost, price, and a full parameter breakdown.

Zero dependencies — Python standard library only (no numpy, no trimesh).

## What it solves

The hard part of 3D print quoting isn't arithmetic. It's that **every step carries an assumption, and those assumptions usually go unstated**.

Three principles:

1. **Real values first** — filament usage and print time from a sliced file are real; read them when available, estimate only when they aren't
2. **Assumptions must be stated** — fill rate, unit judgment, whether machine time is included: all of it goes in the quote
3. **Don't expect to profit from printing** — machine time is billed at depreciation cost (example: ¥1.2/h). The goal is "don't lose money + be fast", not "maximize profit"

## Quick start

```bash
# 1. Inspect: how accurate can this file be, which path does it take
python scripts/analyze.py your_file.gcode.3mf

# 2. Quote
python scripts/quote.py --mass 103.41 --material PLA --hours 2.79 --waste 0.12

# 3. Look up a material (41 available)
python scripts/materials.py PLA
```

`analyze.py` emits a `quote_path` field that tells you exactly what to do next — no reasoning required:

| `quote_path` | Meaning | Next step |
|---|---|---|
| `true_mass` | Has GCode, real filament usage and time available | Use `--mass` + `--hours`, waste 12% |
| `estimated` | No real values, geometry only | Use `--volume --fill 0.25`, waste 20%, machine time not counted |
| `blocked` | Neither real values nor geometry | Resolve the warnings first |

## Three kinds of input, three levels of accuracy

| Input | What you get | Accuracy |
|---|---|---|
| Sliced output `.gcode` / `.gcode.3mf` | Real filament usage, print time, layer height, material type | Accurate |
| Project 3MF (Bambu / Orca export) | Geometry + slice settings (infill, supports, wall count) | Filament estimated |
| Plain geometry `.stl` / ordinary `.3mf` | Geometry volume | Filament estimated, machine time unavailable |

**Classification is based on what's inside the file, not the extension.** The same `.3mf` can be a slice package, a project file, or plain geometry.

## Why a fill rate is needed

STL gives you **solid volume** — as if the model were one solid block of plastic. But real prints aren't solid: there's infill, perimeters, top/bottom shells and supports.

Measured on a paired sample (geometry file and sliced file for the same part):

| | Value |
|---|---|
| Geometry volume | 355.139 cm³ |
| × PLA density 1.24 → solid mass | 440.4 g |
| Actual filament in GCode | **95.57 g** |
| Actual / solid | **21.7% (the solid-volume method overestimates by 4.6×)** |

So the geometry path defaults to **25%**. Note this 25% is *not* the "infill percentage" slider in your slicer — that one is typically 10~15%, because it excludes perimeters, top/bottom shells and supports. **Using 15% directly as the factor underestimates by roughly 40%.**

The ratio varies a lot between models (samples in the dev workspace range from 5% to 15% infill settings). 25% is a chosen default, not a statistical result. For better accuracy, upload a sliced file.

## Structure

```
3d-print-quote/
├── SKILL.md                    # Operating manual for the AI (main file)
├── README.md
├── references/                 # Loaded on demand, not all at once
│   ├── materials.md            # 41 materials: density + reference cost
│   ├── fill-rate.md            # Fill rate rationale, sensitivity, exceptions
│   └── tuning.md               # Parameter tuning, measured sensitivity, verification
├── scripts/
│   ├── analyze.py              # File parsing + inspection, outputs JSON
│   ├── quote.py                # Pure arithmetic, never touches files
│   ├── materials.py            # Material table lookup
│   └── mesh.py                 # Zero-dependency STL/3MF mesh parsing
└── tests/
    ├── cube_10mm.stl           # 10mm reference cube, regression sample
    ├── open_cube_10mm.stl      # Missing one face, tests non-watertight mesh
    └── small_cube_5mm.stl      # 5mm, tests unit lower bound
```

Progressive disclosure: `SKILL.md` holds the *how*, `references/` holds the *why*. The AI reads the main file on trigger and only opens a reference when a specific question comes up.

**To change prices, edit only `references/materials.md`** — scripts read that table live, there is no second copy to sync. Fill in the "your price" column and it takes priority.

## Defaults are examples

Replace the machine hourly rate, gross margin and material cost with values that fit your machine, your supplier and your expectations. Defaults are just enough to make the flow run — they are not recommendations.

| Parameter | Default |
|---|---|
| Gross margin | 30% (price = cost ÷ 0.70, **not** × 1.30) |
| Machine hourly rate | ¥1.2/h (depreciation basis) |
| Fill rate (geometry path) | 25% |
| Waste rate | 12% sliced path / 20% geometry path |
| Setup fee | 0 (hidden option — if the user doesn't mention it, don't apply it) |

## Known limits

- Post-processing labor (support removal, sanding, painting) isn't modeled; currently covered roughly by the setup fee
- Machine time for batch printing on one plate isn't a simple × quantity; currently treated as linear per piece
- The fill rate rests on a single paired sample; no size-bucketed measurements yet
- The default 25% fill rate underestimates thin-shell / vase-mode parts
- Surface-area-to-volume ratio could serve as a thin-wall proxy, but thresholds aren't calibrated

## License

MIT. See `LICENSE`.

## Regression checks

Three samples in `tests/`, each covering one branch:

| Sample | Covers | Expected |
|---|---|---|
| `cube_10mm.stl` | Volume algorithm correctness | Volume 1.0000 cm³, area 600.00 mm², bbox 10×10×10, watertight True — all match theory |
| `open_cube_10mm.stl` | Non-watertight mesh | Triggers `NOT_WATERTIGHT` |
| `small_cube_5mm.stl` | Unit lower bound | Triggers `UNIT_CHECK_SMALL` (5mm suspected inch units) |

```bash
for f in tests/*.stl; do python scripts/analyze.py "$f"; done
```
