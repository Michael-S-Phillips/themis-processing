"""Plot atmospherically-corrected THEMIS IR emissivity spectra alongside the
raw radiance spectra from the same pixels.

Reads the corrected surface emissivity cube from `<id>_tes.hdf` (Dr Rogers'
themis_correct output, key `surf`, 9 bands BSQ) and pulls the matching raw
radiance spectra from the projected lev2 cube via rasterio.

The corrected cube is sparsely populated — only pixels covered by TES
footprints have nonzero emissivity. We pick samples from the dense central
strip where coverage is best.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import h5py
import matplotlib.pyplot as plt
import numpy as np
import rasterio

THEMIS_IR_BAND_CENTERS_UM = np.array(
    [6.78, 6.78, 7.93, 8.56, 9.35, 10.21, 11.04, 11.79, 12.57]
)


def pick_corrected_samples(surf: np.ndarray, n: int = 4) -> list[tuple[int, int]]:
    """Pick n samples from the most densely TES-covered region.

    `surf` is (9, H, W). We look at band 9 and pick pixels with valid emissivity
    well inside the [0.5, 1.2] band so we get genuine surface measurements,
    spaced apart along the strip.
    """
    band9 = surf[8]
    valid = (band9 > 0.5) & (band9 < 1.2)
    if not valid.any():
        raise RuntimeError("no valid emissivity pixels in surf cube")
    rows_with_data = np.where(valid.any(axis=1))[0]
    sample_rows = np.linspace(rows_with_data[0], rows_with_data[-1], n).astype(int)
    samples: list[tuple[int, int]] = []
    for r in sample_rows:
        cols = np.where(valid[r])[0]
        if len(cols) == 0:
            for dr in range(1, 20):
                if r - dr >= 0 and valid[r - dr].any():
                    r = r - dr; cols = np.where(valid[r])[0]; break
                if r + dr < surf.shape[1] and valid[r + dr].any():
                    r = r + dr; cols = np.where(valid[r])[0]; break
        c = int(np.median(cols))
        samples.append((r, c))
    return samples


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tes-hdf", type=Path, required=True,
                   help="Path to <id>_tes.hdf from themis_correct")
    p.add_argument("--lev2", type=Path, required=True,
                   help="Path to projected lev2 cube (raw radiance)")
    p.add_argument("--out", type=Path, required=True,
                   help="Output PNG path")
    p.add_argument("-n", "--num-samples", type=int, default=4)
    args = p.parse_args(argv)

    with h5py.File(str(args.tes_hdf), "r") as f:
        surf = f["surf"][()]
    print(f"corrected: {surf.shape}, range {surf.min():.4g}..{surf.max():.4g}")

    with rasterio.open(str(args.lev2)) as src:
        rad = src.read(out_dtype=np.float32)
        rad[rad < -1e30] = np.nan
    print(f"raw radiance: {rad.shape}")

    samples = pick_corrected_samples(surf, n=args.num_samples)
    for r, c in samples:
        print(f"  pixel ({r:5d},{c:4d})  raw rad: " +
              " ".join(f"{v:6.1f}" for v in rad[:9, r, c]) +
              f"   corrected emiss: " +
              " ".join(f"{v:5.3f}" for v in surf[:, r, c]))

    fig, (ax_l, ax_r) = plt.subplots(1, 2, figsize=(16, 9))
    colors = plt.cm.plasma(np.linspace(0, 1, len(samples)))

    for (r, c), color in zip(samples, colors):
        ax_l.plot(THEMIS_IR_BAND_CENTERS_UM, rad[:9, r, c],
                   marker="o", color=color, lw=1.5, label=f"({r},{c})")
        ax_r.plot(THEMIS_IR_BAND_CENTERS_UM, surf[:, r, c],
                   marker="o", color=color, lw=1.5, label=f"({r},{c})")

    ax_l.set_xlabel("wavelength (µm)")
    ax_l.set_ylabel("radiance (W m⁻² sr⁻¹ µm⁻¹, ISIS-scaled × 1e5)")
    ax_l.set_title("raw radiance (lev2.cub)")
    ax_l.grid(True, alpha=0.3); ax_l.legend(fontsize=9)
    ax_l.set_xticks(np.unique(THEMIS_IR_BAND_CENTERS_UM))

    ax_r.set_xlabel("wavelength (µm)")
    ax_r.set_ylabel("emissivity (atm-corrected)")
    ax_r.set_title("atmospherically-corrected emissivity (themis_correct.surf)")
    ax_r.grid(True, alpha=0.3); ax_r.legend(fontsize=9)
    ax_r.set_xticks(np.unique(THEMIS_IR_BAND_CENTERS_UM))
    ax_r.axhline(1.0, color="k", lw=0.5, linestyle="--", alpha=0.5)

    plt.tight_layout()
    plt.savefig(str(args.out), dpi=120, bbox_inches="tight")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
