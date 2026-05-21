"""Extract and plot THEMIS IR spectra from a projected cube.

Picks N "interesting" pixels from the cube (well-separated, all 10 bands valid)
and plots their radiance spectra.

THEMIS IR band centers (µm): from the standard band-binning at
https://themis.mars.asu.edu/specifications.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import rasterio
from rasterio.windows import Window

THEMIS_IR_BAND_CENTERS_UM = np.array(
    [6.78, 6.78, 7.93, 8.56, 9.35, 10.21, 11.04, 11.79, 12.57, 14.88]
)

ISIS3_NULL = -3.4028235e38


def load_cube(path: Path) -> tuple[np.ndarray, dict, tuple]:
    """Read all bands; return (data, profile, (transform, crs)).

    `data` is shape (bands, H, W), float32, NULL replaced with NaN.
    """
    with rasterio.open(str(path)) as src:
        data = src.read(out_dtype=np.float32)
        data[data < -1e30] = np.nan
        meta = {"width": src.width, "height": src.height, "count": src.count,
                "crs": src.crs, "transform": src.transform, "nodata": src.nodata}
    return data, meta


def pick_sample_pixels(data: np.ndarray, n: int = 6) -> list[tuple[int, int]]:
    """Pick n well-separated pixels where every band is valid."""
    valid = np.all(~np.isnan(data), axis=0)
    H, W = valid.shape
    if not valid.any():
        raise RuntimeError("no valid pixels in cube")
    # Walk down the cube in evenly-spaced rows; for each row, take the pixel
    # closest to the centerline that is valid.
    rows = np.linspace(0.05 * H, 0.95 * H, n).astype(int)
    samples: list[tuple[int, int]] = []
    for r in rows:
        row_valid = np.where(valid[r])[0]
        if len(row_valid) == 0:
            # Search outwards for the nearest valid row.
            for dr in range(1, H):
                if r - dr >= 0 and valid[r - dr].any():
                    r = r - dr; row_valid = np.where(valid[r])[0]; break
                if r + dr < H and valid[r + dr].any():
                    r = r + dr; row_valid = np.where(valid[r])[0]; break
        c = int(np.median(row_valid))
        samples.append((r, c))
    return samples


def plot_spectra(data: np.ndarray, samples: list[tuple[int, int]],
                 out_path: Path, title: str,
                 y_label: str = "radiance (W m⁻² sr⁻¹ µm⁻¹)",
                 band_centers: np.ndarray | None = None) -> None:
    if band_centers is None:
        band_centers = THEMIS_IR_BAND_CENTERS_UM[: data.shape[0]]
    fig, (ax_preview, ax_spec) = plt.subplots(
        1, 2, figsize=(18, 14), gridspec_kw={"width_ratios": [1, 2]}
    )

    # Preview: top band (typically band 9 for raw 10-band, or the last band
    # of a corrected 9-band cube).
    preview_band_idx = min(8, data.shape[0] - 1)
    preview = data[preview_band_idx]
    p2, p98 = np.nanpercentile(preview, [2, 98])
    ax_preview.imshow(preview, cmap="gray", vmin=p2, vmax=p98, aspect="auto",
                      origin="upper")
    colors = plt.cm.viridis(np.linspace(0, 1, len(samples)))
    for (r, c), color in zip(samples, colors):
        ax_preview.scatter([c], [r], s=80, marker="o",
                            facecolor="none", edgecolor=color, linewidth=2)
        ax_preview.text(c + 30, r, f"({r},{c})", color=color, fontsize=8,
                         va="center")
    ax_preview.set_title(f"band {preview_band_idx+1} ({band_centers[preview_band_idx]:.2f} µm)\nsample pixels")
    ax_preview.set_xlabel("col"); ax_preview.set_ylabel("row")

    # Spectra: radiance vs. wavelength for each sample pixel.
    for (r, c), color in zip(samples, colors):
        spec = data[:, r, c]
        ax_spec.plot(band_centers, spec, marker="o",
                      color=color, linewidth=1.5, label=f"({r},{c})")
    ax_spec.set_xlabel("wavelength (µm)")
    ax_spec.set_ylabel(y_label)
    ax_spec.set_title(title)
    ax_spec.set_xticks(np.unique(band_centers))
    ax_spec.grid(True, alpha=0.3)
    ax_spec.legend(loc="best", fontsize=9)

    plt.tight_layout()
    plt.savefig(str(out_path), dpi=120, bbox_inches="tight")
    print(f"wrote {out_path}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("cube", type=Path,
                   help="Path to projected THEMIS IR cube (.cub or .tif)")
    p.add_argument("--out", type=Path, default=Path("spectra.png"))
    p.add_argument("-n", "--num-samples", type=int, default=6)
    p.add_argument("--bands", type=int, default=10,
                   help="Number of bands to plot (10 for raw radiance, 9 for atm-corrected emissivity)")
    p.add_argument("--y-label", default="radiance (W m⁻² sr⁻¹ µm⁻¹)")
    args = p.parse_args(argv)

    data, meta = load_cube(args.cube)
    print(f"cube: {meta['count']} bands × {meta['height']} × {meta['width']}")

    nb = data.shape[0]
    if nb < args.bands:
        print(f"cube has {nb} bands; truncating to that")
        args.bands = nb

    samples = pick_sample_pixels(data, n=args.num_samples)
    for r, c in samples:
        spec = data[:, r, c]
        print(f"  pixel ({r:5d},{c:4d}): " + " ".join(f"{v:6.2f}" for v in spec[: args.bands]))

    plot_spectra(data[: args.bands], samples, args.out,
                  title=f"THEMIS IR spectra — {args.cube.name}",
                  y_label=args.y_label,
                  band_centers=THEMIS_IR_BAND_CENTERS_UM[: args.bands])
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
