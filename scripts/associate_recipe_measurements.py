#!/usr/bin/env python
"""Associate per-well reagent amounts (from a LabOS recipe) with plate-reader signal.

It reads a recipe JSON (the `Steps` list of Distribute/Transfer commands) and one
or more Tecan absorbance `.xls` exports, then builds a per-well table joining

    reagent volumes (uL)  x  spectral metrics (absorbance at / mean / max / area)

and writes a CSV plus diagnostic figures (composition-vs-signal scatter, plate
heatmaps, spectra coloured by a chosen reagent amount).

Usage
-----
    python Results/associate_recipe_measurements.py \
        Library/Recipes/01_SPAAC_hydrogel_96_combinations_05_20260902_155236.json \
        Results/Excel/t0_DA_20260903_112814.xls \
        Results/Excel/Script2_20260902_171418_20260902_171418.xls \
        --range 290 320 --color-by 4arm-20kPEG-DBCO

Options
-------
    --wavelength NM     single wavelength for the "A_at" metric (default 309)
    --range LO HI       nm window for "A_mean" / "area" metrics (default 290 320)
    --color-by REAGENT  reagent whose per-well volume colours the spectra plot
                        (default: the last non-solvent reagent in the recipe)
    --out DIR           output directory (default: Results/Plots)
    --dump             print the composition+signal table to stdout and exit
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy import stats

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from plot_spectra import parse_tecan_xls, WELLS, ROWS  # noqa: E402

SECTION_RE = re.compile(r"===\s*(.+?)\s*:")


def _well(dest: str) -> str:
    """'TargetPlate:H12' -> 'H12'."""
    return dest.split(":")[-1].strip()


def parse_recipe(path: str) -> pd.DataFrame:
    """Return a DataFrame indexed by well with one uL column per reagent."""
    recipe = json.load(open(path, encoding="utf-8"))
    steps = recipe.get("Steps", [])
    declared = recipe.get("Metadata", {}).get("Reagents", [])

    vols: dict[str, dict[str, float]] = {}
    current = None
    for step in steps:
        cmd = step.get("Command")
        if cmd == "Comment":
            m = SECTION_RE.search(step.get("Text", ""))
            if m:
                current = m.group(1).strip()
            continue
        if cmd not in ("Distribute", "Transfer"):
            continue

        dests = step.get("Destinations")
        if dests is None and step.get("Destination") is not None:
            dests = [step["Destination"]]
        if not dests:
            continue
        dests = [_well(d) for d in dests]

        vol = step.get("Volume")
        if isinstance(vol, list):
            vlist = [float(x) for x in vol]
        else:
            vlist = [float(vol)] * len(dests)
        if len(vlist) != len(dests):
            print(f"  warn: {cmd} for {current}: {len(vlist)} volumes vs "
                  f"{len(dests)} destinations - truncating to min")
        # reagent name: section comment, else parsed from Description
        reagent = current
        if reagent is None:
            desc = step.get("Description", "")
            reagent = next((r for r in declared if r in desc), desc or "reagent")

        bucket = vols.setdefault(reagent, {})
        for w, v in zip(dests, vlist):
            bucket[w] = bucket.get(w, 0.0) + v

    order = declared or list(vols)
    cols = {f"vol_{r}": pd.Series(vols.get(r, {})) for r in order if r in vols}
    df = pd.DataFrame(cols).reindex(WELLS).fillna(0.0)
    df.index.name = "well"
    df.insert(0, "row", [w[0] for w in df.index])
    df.insert(1, "col", [int(w[1:]) for w in df.index])
    df["vol_total"] = df[[c for c in df.columns if c.startswith("vol_")]].sum(axis=1)
    return df


def spectral_metrics(wl, series, wl_at, wl_lo, wl_hi) -> dict:
    y = np.asarray(series, dtype=float)
    finite = np.isfinite(y)
    i_at = int(np.argmin(np.abs(wl - wl_at)))
    band = (wl >= wl_lo) & (wl <= wl_hi) & finite
    return {
        f"A_at_{wl_at:g}": y[i_at] if finite[i_at] else np.nan,
        f"A_mean_{wl_lo:g}_{wl_hi:g}": np.nanmean(y[band]) if band.any() else np.nan,
        f"area_{wl_lo:g}_{wl_hi:g}": (np.trapezoid(y[band], wl[band])
                                      if band.sum() > 1 else np.nan),
        "A_max": np.nanmax(y) if finite.any() else np.nan,
        "n_OVER": int((~finite).sum()),
    }


def corr_table(df: pd.DataFrame, metric: str, reagents: list[str]) -> pd.DataFrame:
    rows = []
    for r in reagents:
        x, y = df[r].to_numpy(float), df[metric].to_numpy(float)
        ok = np.isfinite(x) & np.isfinite(y)
        if ok.sum() < 3 or np.ptp(x[ok]) == 0:
            rows.append((r, np.nan, np.nan, int(ok.sum())))
            continue
        pr = stats.pearsonr(x[ok], y[ok]).statistic
        sr = stats.spearmanr(x[ok], y[ok]).statistic
        rows.append((r, pr, sr, int(ok.sum())))
    return pd.DataFrame(rows, columns=["reagent", "pearson_r", "spearman_r", "n"])


def fig_scatter(df, metric, reagents, title, out_png):
    n = len(reagents)
    ncol = min(3, n)
    nrow = int(np.ceil(n / ncol))
    fig, axes = plt.subplots(nrow, ncol, figsize=(4.5 * ncol, 4 * nrow),
                             squeeze=False)
    for ax in axes.flat[n:]:
        ax.set_visible(False)
    for ax, r in zip(axes.flat, reagents):
        x, y = df[r].to_numpy(float), df[metric].to_numpy(float)
        ok = np.isfinite(x) & np.isfinite(y)
        ax.scatter(x[ok], y[ok], s=18, alpha=0.75, edgecolor="none")
        if ok.sum() >= 3 and np.ptp(x[ok]) > 0:
            pr = stats.pearsonr(x[ok], y[ok]).statistic
            sr = stats.spearmanr(x[ok], y[ok]).statistic
            b, a = np.polyfit(x[ok], y[ok], 1)
            xs = np.linspace(x[ok].min(), x[ok].max(), 50)
            ax.plot(xs, a + b * xs, color="crimson", lw=1)
            ax.set_title(f"{r.replace('vol_', '')}\nPearson {pr:+.2f}  "
                         f"Spearman {sr:+.2f}  (n={ok.sum()})", fontsize=9)
        else:
            ax.set_title(r.replace("vol_", ""), fontsize=9)
        ax.set_xlabel(f"{r.replace('vol_', '')} (uL)")
        ax.set_ylabel(metric)
    fig.suptitle(title, fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(out_png, dpi=120)
    print(f"wrote {out_png}")


def _grid(series: pd.Series) -> np.ndarray:
    g = np.full((8, 12), np.nan)
    for w, v in series.items():
        g[ROWS.index(w[0]), int(w[1:]) - 1] = v
    return g


def fig_heatmaps(df, metric, reagents, title, out_png):
    panels = reagents + [metric]
    ncol = min(3, len(panels))
    nrow = int(np.ceil(len(panels) / ncol))
    fig, axes = plt.subplots(nrow, ncol, figsize=(5 * ncol, 3.6 * nrow),
                             squeeze=False)
    for ax in axes.flat[len(panels):]:
        ax.set_visible(False)
    for ax, key in zip(axes.flat, panels):
        g = _grid(df[key])
        im = ax.imshow(g, aspect="auto", cmap="viridis")
        ax.set_title(key.replace("vol_", ""), fontsize=10)
        ax.set_xticks(range(12), range(1, 13), fontsize=7)
        ax.set_yticks(range(8), list(ROWS), fontsize=7)
        fig.colorbar(im, ax=ax, fraction=0.045)
    fig.suptitle(title, fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(out_png, dpi=120)
    print(f"wrote {out_png}")


def fig_spectra_by_reagent(wl, data, df, reagent_col, title, out_png):
    vals = df[reagent_col]
    vmin, vmax = np.nanmin(vals), np.nanmax(vals)
    norm = plt.Normalize(vmin, vmax)
    cmap = plt.get_cmap("viridis")
    fig, ax = plt.subplots(figsize=(12, 7))
    for well in WELLS:
        y = data.get(well)
        if y is None or not np.isfinite(vals.get(well, np.nan)):
            continue
        ax.plot(wl, y, lw=0.7, alpha=0.85, color=cmap(norm(vals[well])))
    sm = plt.cm.ScalarMappable(norm=norm, cmap=cmap)
    fig.colorbar(sm, ax=ax, label=f"{reagent_col.replace('vol_', '')} (uL)")
    ax.set_xlabel("wavelength (nm)")
    ax.set_ylabel("absorbance")
    ax.set_title(title)
    ax.margins(x=0)
    fig.tight_layout()
    fig.savefig(out_png, dpi=120)
    print(f"wrote {out_png}")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("recipe")
    ap.add_argument("measurements", nargs="+", help="Tecan .xls export(s)")
    ap.add_argument("--wavelength", type=float, default=309.0)
    ap.add_argument("--range", type=float, nargs=2, default=(290.0, 320.0),
                    metavar=("LO", "HI"))
    ap.add_argument("--color-by", default=None)
    ap.add_argument("--out", default=os.path.join("Results", "Plots"))
    ap.add_argument("--dump", action="store_true")
    args = ap.parse_args(argv)

    comp = parse_recipe(args.recipe)
    reagent_cols = [c for c in comp.columns if c.startswith("vol_")
                    and c != "vol_total"]
    print(f"recipe: {os.path.basename(args.recipe)}")
    print(f"  reagents: {[c.replace('vol_', '') for c in reagent_cols]}")
    print(f"  per-well total volume: {comp['vol_total'].min():g}-"
          f"{comp['vol_total'].max():g} uL "
          f"(mean {comp['vol_total'].mean():.1f})")

    wl_lo, wl_hi = args.range
    color_by = args.color_by
    if color_by is None:
        non_solvent = [c for c in reagent_cols if "solvent" not in c.lower()]
        color_by = (non_solvent or reagent_cols)[-1].replace("vol_", "")
    color_col = f"vol_{color_by}"
    if color_col not in comp.columns:
        sys.exit(f"--color-by {color_by!r} not among {reagent_cols}")

    os.makedirs(args.out, exist_ok=True)
    frames = []
    for mpath in args.measurements:
        if not os.path.isfile(mpath):
            sys.exit(f"not found: {mpath}")
        wl, data, meta = parse_tecan_xls(mpath)
        stem = os.path.splitext(os.path.basename(mpath))[0]
        met = pd.DataFrame(
            {w: spectral_metrics(wl, data[w], args.wavelength, wl_lo, wl_hi)
             for w in data}).T.reindex(WELLS)
        metric_cols = list(met.columns)
        joined = comp.join(met)
        joined.insert(0, "measurement", stem)
        frames.append(joined)

        primary = f"A_at_{args.wavelength:g}"
        print(f"\nmeasurement: {stem}   {meta.get('Mode', '')}  "
              f"scan {wl[0]:.0f}-{wl[-1]:.0f} nm")
        print(corr_table(joined, primary, reagent_cols + ["vol_total"])
              .to_string(index=False))

        base = os.path.join(args.out, f"{stem}__vs_composition")
        fig_scatter(joined, primary, reagent_cols + ["vol_total"],
                    f"{stem}: {primary} vs reagent volume", base + "_scatter.png")
        fig_heatmaps(joined, primary, reagent_cols,
                     f"{stem}: plate maps ({primary})", base + "_heatmaps.png")
        fig_spectra_by_reagent(
            wl, data, joined, color_col,
            f"{stem}: spectra coloured by {color_by} volume",
            os.path.join(args.out, f"{stem}__spectra_by_{color_by}.png"))

    out = pd.concat(frames)
    csv_path = os.path.join(
        args.out,
        os.path.splitext(os.path.basename(args.recipe))[0] + "__composition_signal.csv")
    out.to_csv(csv_path)
    print(f"\nwrote {csv_path}  ({len(out)} rows)")

    if args.dump:
        cols = ["measurement"] + reagent_cols + ["vol_total"] + metric_cols
        with pd.option_context("display.max_rows", None, "display.width", 200):
            print(out[cols].to_string())


if __name__ == "__main__":
    main()
