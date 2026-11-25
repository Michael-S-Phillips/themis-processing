"""Regional-atmosphere correction for the THEMIS pipeline (default since 2026-11-19).

Rogers' `themis_correct` derives one atmospheric transmittance per TES footprint i,
    atm_trans_i = mean(E over the footprint) / TES surface emissivity of footprint i,
and divides each pixel by the transmittances of the footprints that cover it. Pixels outside
footprints get no value, and each footprint's TES noise appears as a footprint-sized patch.

Here the same per-footprint transmittances (`atm` = -ln atm_trans in <id>_tes.hdf) are combined
over a latitude window instead: for every image row, T(row) = exp(-median atm_i) over the
footprints whose centroid lies within +-window/2 degrees of the row's latitude (at least
`min_footprints`), per band. Footprints whose atm is not finite are left out: themis_correct gives
atm = +inf (transmittance 0) to footprints with no valid THEMIS pixel, e.g. footprints outside a
cropped lev2, and NaN in the RDR chain; they carry no atmosphere estimate. Then surf = E / T(row) for every pixel with valid E, where E is the
THEMIS emissivity from themis_emissivity_full.dv (identical to the E inside themis_correct).

Justification (cross-scene repeatability with independent TES orbits, window sweep 0.5-8 deg):
massif-themis-analysis/wiki/methods/themis-pipeline-validation.md §4.2. 3 deg sits in the middle
of the 0.5-5 deg plateau; wider windows and a single scene-wide atmosphere are worse.

Datasets written into <id>_tes.hdf (davinci layout: band, line, sample):
  surf            regional-atmosphere surface emissivity (replaces themis_correct's)
  surf_footprint  themis_correct's per-footprint surface emissivity (the original `surf`)
  emiss           E, THEMIS emissivity before atmospheric correction (0 = invalid)
  t_regional      T(row), 9 x lines
  attrs on root:  atm_mode, atm_window_deg, atm_min_footprints, atm_n_footprints
"""
from __future__ import annotations

from pathlib import Path

import h5py
import numpy as np

MARS_LL = "+proj=longlat +R=3396190 +no_defs"


def footprint_lats(tesover_hdf: Path) -> np.ndarray:
    from shapely import wkt as shp_wkt
    with h5py.File(tesover_hdf, "r") as f:
        polys = [p for p in f["wkt"][()][0].decode().split("\n") if p.strip()]
    return np.array([shp_wkt.loads(p).centroid.y for p in polys])


def row_latitudes(lev2: Path) -> np.ndarray:
    import rasterio
    from pyproj import Transformer
    with rasterio.open(str(lev2)) as src:
        h, w, tr, crs = src.height, src.width, src.transform, src.crs
    to_ll = Transformer.from_crs(crs, MARS_LL, always_xy=True).transform
    _, lat = to_ll(np.full(h, tr.c + tr.a * w / 2), tr.f + tr.e * (np.arange(h) + 0.5))
    return np.asarray(lat)


def regional_transmittance(atm: np.ndarray, fp_lat: np.ndarray, row_lat: np.ndarray,
                           window_deg: float, min_footprints: int) -> np.ndarray:
    """atm: (N, 9) -ln transmittance per footprint. Returns T (9, lines); NaN where too few footprints."""
    T = np.full((atm.shape[1], len(row_lat)), np.nan, np.float32)
    keep = np.all(np.isfinite(atm), axis=1)        # footprints with no THEMIS pixels: atm = inf/NaN
    atm, fp_lat = atm[keep], fp_lat[keep]
    rl = np.round(row_lat, 2)
    for lat0 in np.unique(rl):
        near = np.abs(fp_lat - lat0) <= window_deg / 2
        if near.sum() >= min_footprints:
            T[:, rl == lat0] = np.exp(-np.nanmedian(atm[near], axis=0))[:, None]
    return T


def apply(tes_hdf: Path, emiss_hdf: Path, tesover_hdf: Path, lev2: Path,
          window_deg: float = 3.0, min_footprints: int = 5, redo: bool = False) -> dict:
    """redo: recompute the regional `surf` of an already-converted _tes.hdf from its stored
    `surf_footprint` and `emiss` (emiss_hdf is then not needed)."""
    with h5py.File(tes_hdf, "r") as f:
        done = "surf_footprint" in f
        if done and not redo:
            return {"skipped": "exists", "atm_window_deg": float(f.attrs.get("atm_window_deg", np.nan))}
        surf_fp = f["surf_footprint" if done else "surf"][()].astype(np.float32)
        atm = f["atm"][()][:, 0, :].T.astype(np.float64)          # (N, 9)
        E = f["emiss"][()].astype(np.float32) if done else None
    if E is None:
        with h5py.File(emiss_hdf, "r") as f:
            E = f["emiss"][()].astype(np.float32)
    if E.shape != surf_fp.shape:
        raise ValueError(f"emiss {E.shape} vs surf {surf_fp.shape}")
    fp_lat = footprint_lats(tesover_hdf)
    if len(fp_lat) != len(atm):
        raise ValueError(f"{len(fp_lat)} footprints in tesover vs {len(atm)} atm rows")
    row_lat = row_latitudes(lev2)
    if len(row_lat) != E.shape[1]:
        raise ValueError(f"lev2 has {len(row_lat)} lines, surf {E.shape[1]}")
    T = regional_transmittance(atm, fp_lat, row_lat, window_deg, min_footprints)
    E[~np.isfinite(E)] = 0
    valid = (E[8] > 0) & np.all(np.isfinite(T) & (T > 0), axis=0)[:, None]
    with np.errstate(invalid="ignore", divide="ignore"):
        surf = np.where(valid[None], E / T[:, :, None], 0).astype(np.float32)
    comp = dict(compression="gzip", compression_opts=4, shuffle=True,
                chunks=(1, min(512, E.shape[1]), E.shape[2]))
    # write a complete new file, then replace: a crash never leaves a half-modified _tes.hdf
    tmp = tes_hdf.with_name(tes_hdf.name + ".partial")
    with h5py.File(tes_hdf, "r") as src, h5py.File(tmp, "w") as f:
        for k in src:
            if k not in ("surf", "surf_footprint", "emiss", "t_regional"):
                src.copy(src[k], f, name=k)
        f.create_dataset("surf", data=surf, **comp)
        f.create_dataset("surf_footprint", data=surf_fp, **comp)
        f.create_dataset("emiss", data=E, **comp)
        f.create_dataset("t_regional", data=T)
        f.attrs.update(atm_mode="regional", atm_window_deg=window_deg,
                       atm_min_footprints=min_footprints, atm_n_footprints=len(atm),
                       atm_n_footprints_used=int(np.all(np.isfinite(atm), axis=1).sum()))
    tmp.replace(tes_hdf)
    rows_ok = int(np.all(np.isfinite(T) & (T > 0), axis=0).sum())
    return dict(atm_window_deg=window_deg, n_footprints=len(atm),
                n_footprints_used=int(np.all(np.isfinite(atm), axis=1).sum()), rows_with_T=rows_ok,
                rows_total=len(row_lat), valid_px_regional=int(valid.sum()),
                valid_px_footprint=int((surf_fp[8] > 0).sum()))
