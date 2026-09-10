#!/usr/bin/env python
"""Plot Tecan i-control absorbance spectra (one trace per well, vs wavelength).

Reads one or more legacy .xls exports produced by the Tecan M200 / infinite 200Pro
("Wavel." spectrum table, 96 wells A1..H12) and draws, for every well, absorbance
against wavelength.

Usage
-----
    # the two files from the request (overlaid per well)
    python Results/plot_spectra.py \
        Results/Excel/t0_DA_20260903_112814.xls \
        Results/Excel/Script2_20260902_171418_20260902_171418.xls

    # a single file
    python Results/plot_spectra.py Results/Excel/t0_DA_20260903_112814.xls

Options
-------
    --out DIR      where to write the PNGs (default: Results/Plots)
    --show         also open the figures interactively
    --overlay-only just the single all-wells overlay figure, skip the 8x12 grid
    --grid-only    just the 8x12 plate-layout grid, skip the overlay figure

For each input file (and, when several files are given, additionally a combined
figure per well) the script writes:
    <name>__grid.png     8 rows x 12 cols, one mini-plot per well
    <name>__overlay.png  all 96 wells on a single axis
"OVER" (detector overflow) cells are treated as missing and left as gaps.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import xlrd
import matplotlib

matplotlib.use("Agg")  # switched to a GUI backend later if --show is passed
import matplotlib.pyplot as plt

ROWS = "ABCDEFGH"
COLS = list(range(1, 13))
WELLS = [f"{r}{c}" for r in ROWS for c in COLS]  # row-major, matches the export


def parse_tecan_xls(path: str):
    """Return (wavelengths, {well: np.ndarray}, meta) for a Tecan spectrum export."""
    book = xlrd.open_workbook(path)
    sheet = book.sheet_by_index(0)

    header_row = None
    for r in range(sheet.nrows):
        if str(sheet.cell_value(r, 0)).strip().lower() == "wavel.":
            header_row = r
            break
    if header_row is None:
        raise ValueError(f"{path}: no 'Wavel.' header row found")

    raw_wl = sheet.row_values(header_row)[1:]
    wl = []
    for v in raw_wl:
        try:
            wl.append(float(v))
        except (TypeError, ValueError):
            wl.append(np.nan)
    wl = np.asarray(wl, dtype=float)
    ncols = int(np.sum(np.isfinite(wl)))
    wl = wl[:ncols]

    data: dict[str, np.ndarray] = {}
    for r in range(header_row + 1, sheet.nrows):
        label = str(sheet.cell_value(r, 0)).strip()
        if label not in WELLS:
            continue
        vals = []
        for c in range(1, ncols + 1):
            cell = sheet.cell_value(r, c)
            if isinstance(cell, (int, float)):
                vals.append(float(cell))
            else:  # "OVER", "", etc.
                vals.append(np.nan)
        data[label] = np.asarray(vals, dtype=float)

    meta = {}
    for r in range(header_row):
        key = str(sheet.cell_value(r, 0)).strip().rstrip(":")
        if key in ("Mode", "Wavelength Start", "Wavelength End",
                   "Wavelength Step Size", "Start Time"):
            row = [x for x in sheet.row_values(r)[1:] if x not in ("", None)]
            meta[key] = row[0] if row else ""
    return wl, data, meta


def _stem(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def plot_grid(datasets, title, out_png):
    """datasets: list of (label, wl, data-dict). One 8x12 grid, wells as subplots."""
    fig, axes = plt.subplots(8, 12, figsize=(24, 16), sharex=True, sharey=True)
    colors = plt.rcParams["axes.prop_cycle"].by_key()["color"]

    for i, well in enumerate(WELLS):
        ax = axes.flat[i]
        for j, (label, wl, data) in enumerate(datasets):
            y = data.get(well)
            if y is None:
                continue
            ax.plot(wl, y, lw=0.8, color=colors[j % len(colors)], label=label)
        ax.set_title(well, fontsize=8, pad=2)
        ax.tick_params(labelsize=6)
        ax.margins(x=0)

    for ax in axes[-1, :]:
        ax.set_xlabel("wavelength (nm)", fontsize=7)
    for ax in axes[:, 0]:
        ax.set_ylabel("absorbance", fontsize=7)

    fig.suptitle(title, fontsize=14, y=0.998)
    bottom = 0.0
    if len(datasets) > 1:
        handles, labels = axes.flat[0].get_legend_handles_labels()
        fig.legend(handles, labels, loc="lower center", ncol=len(datasets),
                   fontsize=10)
        bottom = 0.03
    fig.tight_layout(rect=(0, bottom, 1, 0.975))
    fig.savefig(out_png, dpi=120)
    print(f"wrote {out_png}")
    return fig


def plot_overlay(wl, data, title, out_png):
    """All 96 wells on one axis, coloured by plate row."""
    fig, ax = plt.subplots(figsize=(12, 7))
    cmap = plt.get_cmap("viridis")
    for i, well in enumerate(WELLS):
        y = data.get(well)
        if y is None:
            continue
        ax.plot(wl, y, lw=0.6, color=cmap(ROWS.index(well[0]) / 7.0), alpha=0.8)
    # one legend entry per plate row
    for ri, rl in enumerate(ROWS):
        ax.plot([], [], color=cmap(ri / 7.0), label=f"row {rl}")
    ax.set_xlabel("wavelength (nm)")
    ax.set_ylabel("absorbance")
    ax.set_title(title)
    ax.margins(x=0)
    ax.legend(fontsize=8, ncol=2)
    fig.tight_layout()
    fig.savefig(out_png, dpi=120)
    print(f"wrote {out_png}")
    return fig


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help="Tecan .xls spectrum export(s)")
    ap.add_argument("--out", default=os.path.join("Results", "Plots"),
                    help="output directory for PNGs (default: Results/Plots)")
    ap.add_argument("--show", action="store_true", help="also show figures")
    ap.add_argument("--overlay-only", action="store_true")
    ap.add_argument("--grid-only", action="store_true")
    args = ap.parse_args(argv)

    if args.show:
        matplotlib.use("TkAgg", force=True)
        global plt
        import matplotlib.pyplot as plt  # noqa: F811

    os.makedirs(args.out, exist_ok=True)

    parsed = []
    for f in args.files:
        if not os.path.isfile(f):
            sys.exit(f"not found: {f}")
        wl, data, meta = parse_tecan_xls(f)
        n_over = sum(int(np.isnan(v).sum()) for v in data.values())
        print(f"{f}: {len(data)} wells, {len(wl)} wavelengths "
              f"{wl[0]:.0f}-{wl[-1]:.0f} nm, {n_over} OVER cells  {meta}")
        parsed.append((_stem(f), wl, data))

    for stem, wl, data in parsed:
        if not args.overlay_only:
            plot_grid([(stem, wl, data)], f"{stem} - absorbance per well",
                      os.path.join(args.out, f"{stem}__grid.png"))
        if not args.grid_only:
            plot_overlay(wl, data, f"{stem} - all wells",
                         os.path.join(args.out, f"{stem}__overlay.png"))

    if len(parsed) > 1 and not args.overlay_only:
        combo = "__vs__".join(s for s, _, _ in parsed)
        plot_grid(parsed, "per-well comparison: " + ", ".join(s for s, _, _ in parsed),
                  os.path.join(args.out, f"{combo}__grid.png"))

    if args.show:
        plt.show()


if __name__ == "__main__":
    main()
