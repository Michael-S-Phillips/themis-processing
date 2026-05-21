"""Pure-Python Decorrelation Stretch (DCS) for projected THEMIS IR cubes.

A fallback for when Davinci is unavailable. Operates on a FITS or ISIS cube
read via rasterio/numpy and produces an RGB PNG plus a GeoTIFF.

Used by themis_pipeline.py when --use-python-dcs is set or Davinci isn't on PATH.

Algorithm: classic decorrelation stretch.
  1. Stack 3 input bands as channels in a (H*W, 3) matrix.
  2. Mask no-data, compute mean and covariance of valid pixels.
  3. Eigendecompose covariance → eigenvectors V, eigenvalues λ.
  4. Whiten: data' = (data - mean) @ V @ diag(1/sqrt(λ)).
  5. Stretch each PC to its 2nd–98th percentile to fill display range.
  6. Rotate back: data'' = data' @ V.T, then re-add mean.
  7. Linear stretch each output channel to [0, 255].
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import numpy as np

log = logging.getLogger("dcs")


def decorrelation_stretch(rgb: np.ndarray, ignore: float | None) -> np.ndarray:
    """rgb shape (H, W, 3) float; returns uint8 (H, W, 3)."""
    h, w, c = rgb.shape
    assert c == 3, "DCS expects exactly 3 input bands"
    flat = rgb.reshape(-1, 3).astype(np.float64)

    if ignore is not None:
        valid = np.all(flat != ignore, axis=1) & np.all(np.isfinite(flat), axis=1)
    else:
        valid = np.all(np.isfinite(flat), axis=1)
    if valid.sum() < 100:
        raise RuntimeError(f"too few valid pixels for DCS: {valid.sum()}")

    sample = flat[valid]
    mean = sample.mean(axis=0)
    cov = np.cov(sample - mean, rowvar=False)
    eigvals, eigvecs = np.linalg.eigh(cov)
    # Guard against zero eigenvalues
    eigvals = np.clip(eigvals, 1e-12, None)

    whitening = eigvecs @ np.diag(1.0 / np.sqrt(eigvals)) @ eigvecs.T
    centered = flat - mean
    stretched = centered @ whitening

    # Per-channel percentile stretch to [0, 255]
    out = np.zeros_like(stretched, dtype=np.float64)
    for i in range(3):
        ch = stretched[valid, i]
        lo, hi = np.percentile(ch, [2, 98])
        if hi <= lo:
            hi = lo + 1.0
        out[:, i] = np.clip((stretched[:, i] - lo) * (255.0 / (hi - lo)), 0, 255)

    out[~valid] = 0
    return out.reshape(h, w, 3).astype(np.uint8)


def read_cube(path: Path) -> tuple[np.ndarray, dict]:
    """Read a multiband image as (H, W, bands) float, plus a metadata dict."""
    try:
        import rasterio  # noqa
    except ImportError as e:
        raise RuntimeError(
            "rasterio is required for DCS. Install in your conda env: "
            "`conda install -c conda-forge rasterio`"
        ) from e
    import rasterio
    with rasterio.open(str(path)) as src:
        data = src.read().astype(np.float32)  # (bands, H, W)
        meta = src.meta.copy()
        nodata = src.nodata
    return np.transpose(data, (1, 2, 0)), {"meta": meta, "nodata": nodata}


def write_png(path: Path, rgb_uint8: np.ndarray) -> None:
    try:
        from PIL import Image
    except ImportError as e:
        raise RuntimeError(
            "Pillow is required. Install in your conda env: `pip install Pillow`"
        ) from e
    Image.fromarray(rgb_uint8).save(str(path))


def write_geotiff(path: Path, rgb_uint8: np.ndarray, meta: dict) -> None:
    import rasterio
    out_meta = meta.copy()
    out_meta.update(count=3, dtype="uint8", nodata=None, compress="lzw")
    with rasterio.open(str(path), "w", **out_meta) as dst:
        for i in range(3):
            dst.write(rgb_uint8[:, :, i], i + 1)


def dcs(input_path: Path, png_path: Path, tif_path: Path | None = None,
        bands: tuple[int, int, int] = (9, 8, 6), ignore: float | None = -32768.0) -> None:
    """Run the DCS on a multi-band image.

    bands: 1-indexed band numbers (R, G, B). Default 9/8/6 highlights silicate
    mineralogy for THEMIS IR (close to the runbook's 9/7/5 triplet).
    """
    log.info("reading %s", input_path)
    cube, info = read_cube(input_path)
    h, w, nb = cube.shape
    log.info("dims: %d x %d x %d (H W bands)", h, w, nb)

    b = [bands[i] - 1 for i in range(3)]
    for bi in b:
        if not (0 <= bi < nb):
            raise IndexError(f"band {bi + 1} out of range (cube has {nb} bands)")
    rgb = np.stack([cube[:, :, bi] for bi in b], axis=-1)

    log.info("running DCS on bands %s, ignore=%s", bands, ignore)
    out = decorrelation_stretch(rgb, ignore=ignore)

    log.info("writing %s", png_path)
    write_png(png_path, out)

    if tif_path is not None:
        if info["meta"].get("crs") is None:
            log.warning("no CRS in source — geotiff will be ungeoreferenced")
        log.info("writing %s", tif_path)
        write_geotiff(tif_path, out, info["meta"])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input", type=Path, help="Input cube (FITS, GeoTIFF, or anything rasterio reads)")
    p.add_argument("--png", type=Path, required=True, help="Output PNG path")
    p.add_argument("--tif", type=Path, help="Optional GeoTIFF output path")
    p.add_argument("--bands", default="9,8,6", help="Comma-separated 1-indexed R,G,B bands")
    p.add_argument("--ignore", type=float, default=-32768.0, help="No-data value to mask")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")
    bands = tuple(int(x) for x in args.bands.split(","))  # type: ignore[assignment]
    if len(bands) != 3:
        p.error("--bands must be 3 integers")
    try:
        dcs(args.input, args.png, args.tif, bands=bands, ignore=args.ignore)
    except Exception as e:
        log.error("DCS failed: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
