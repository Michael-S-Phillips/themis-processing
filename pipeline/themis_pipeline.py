"""THEMIS IR EDR → projected cube + DCS preview pipeline.

Wraps the ISIS half of the workflow described in
`Get themis processing up and running.docx` and adds a Davinci pass for
radiance enhancement (`thm.rectify` → `rrmnoise_pca` → `dcs`).

Replaces the runbook's `thm_pre_process.dv` + ISIS steps + `thm_post_process.dv`
sequence with a single idempotent CLI.

Atmospheric correction (themis_correct.dav, PDART_mineral_library.hdf,
TES_atmlib.hdf) is intentionally not included — those JHU/APL assets are not
present on this machine.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("themis")


@dataclass(frozen=True)
class Paths:
    edr: Path           # input .QUB
    workdir: Path       # all intermediates land here
    stem: str           # observation id, e.g. "I01269007"
    lev1: Path          # post-thm2isis (raw cube, optionally calibrated)
    lev1_spice: Path    # post-spiceinit (same file, but SPICE attached)
    lev2: Path          # post-cam2map (projected)
    preview_png: Path   # isis2std PNG of lev2
    dcs_png: Path       # Davinci DCS image
    dcs_tif: Path       # Davinci DCS reprojected geotiff
    atm_dcs_png: Path   # Davinci DCS image of atmospherically-corrected cube
    atm_envi: Path      # ENVI/ER Mapper file of atmospherically-corrected surface

    @classmethod
    def from_edr(cls, edr: Path, workdir: Path) -> "Paths":
        stem = re.sub(r"EDR$", "", edr.stem, flags=re.IGNORECASE)
        return cls(
            edr=edr,
            workdir=workdir,
            stem=stem,
            lev1=workdir / f"{stem}.lev1.cub",
            lev1_spice=workdir / f"{stem}.lev1.cub",
            lev2=workdir / f"{stem}.lev2.cub",
            preview_png=workdir / f"{stem}.lev2.png",
            dcs_png=workdir / f"{stem}.dcs.png",
            dcs_tif=workdir / f"{stem}.dcs.tif",
            atm_dcs_png=workdir / f"{stem}_atm.dcs.png",
            atm_envi=workdir / f"{stem}_rematm.envi",
        )


def run(cmd: list[str], *, check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a subprocess, logging the command and streaming output to our logger."""
    pretty = " ".join(map(str, cmd))
    log.info("$ %s", pretty)
    result = subprocess.run(cmd, capture_output=True, text=True, env=env,
                            errors="replace")
    if result.stdout:
        for line in result.stdout.splitlines():
            log.info("  %s", line)
    if result.stderr:
        for line in result.stderr.splitlines():
            log.warning("  %s", line)
    if check and result.returncode != 0:
        raise RuntimeError(f"command failed (exit {result.returncode}): {pretty}")
    return result


DAVINCI_ENV_FILE = Path.home() / "local" / "davinci" / "etc" / "davinci.env"


def _has_davinci(env: dict) -> bool:
    """Check whether 'davinci' is on PATH inside the given env dict."""
    for dir_ in env.get("PATH", "").split(os.pathsep):
        if (Path(dir_) / "davinci").is_file():
            return True
    return False


def ensure_isis_env() -> dict:
    """Return an env dict with ISIS variables set, or raise if ISIS isn't usable."""
    for binname in ("thm2isis", "spiceinit", "cam2map", "isis2std"):
        if shutil.which(binname) is None:
            raise RuntimeError(
                f"{binname} not found on PATH. Activate the isis conda env first: "
                "`conda activate isis`"
            )
    env = os.environ.copy()
    isisroot = env.get("ISISROOT") or os.path.dirname(shutil.which("thm2isis") or "")
    if not env.get("ISISDATA"):
        # ISIS needs SPICE kernels under ISISDATA — try common locations.
        for candidate in (
            Path.home() / "isis_data",
            Path("/mnt/themis-processing/isis_data"),
            Path(isisroot).parent / "data",
        ):
            if (candidate / "base" / "kernels").exists() or candidate.exists():
                env["ISISDATA"] = str(candidate)
                break
        else:
            log.warning(
                "ISISDATA is not set and no obvious data area found; "
                "spiceinit will fail until kernels are downloaded."
            )

    # Layer Davinci env on top of the ISIS env, if davinci is installed.
    if DAVINCI_ENV_FILE.exists():
        dv_vars = subprocess.run(
            ["bash", "-c", f"source {DAVINCI_ENV_FILE} && env"],
            capture_output=True, text=True, env=env,
        ).stdout
        for line in dv_vars.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                env[k] = v
    return env


def step_thm2isis(p: Paths, env: dict) -> None:
    """Ingest the raw .QUB EDR into an ISIS cube."""
    if p.lev1.exists():
        log.info("skip thm2isis: %s already exists", p.lev1.name)
        return
    run(["thm2isis", f"from={p.edr}", f"to={p.lev1}"], env=env)
    if not p.lev1.exists():
        # thm2isis writes per-detector outputs (e.g. *.even.cub, *.odd.cub) for VIS
        # but for IR EDRs it produces a single cube matching `to=`.
        siblings = sorted(p.workdir.glob(f"{p.lev1.stem}*.cub"))
        raise RuntimeError(
            f"thm2isis did not produce {p.lev1.name}; outputs were: {[s.name for s in siblings]}"
        )


def step_spiceinit(p: Paths, env: dict) -> None:
    """Attach SPICE kernels (in-place modification of the cube)."""
    # spiceinit is idempotent — it reattaches kernels each run. Cheap to repeat.
    run(["spiceinit", f"from={p.lev1_spice}"], env=env)


def step_cam2map(p: Paths, env: dict) -> None:
    """Project the cube to a map projection (Sinusoidal by default in ISIS)."""
    if p.lev2.exists():
        log.info("skip cam2map: %s already exists", p.lev2.name)
        return
    run(["cam2map", f"from={p.lev1_spice}", f"to={p.lev2}"], env=env)


def step_isis2std(p: Paths, env: dict) -> None:
    """Produce a quicklook PNG of the projected cube."""
    if p.preview_png.exists():
        log.info("skip isis2std: %s already exists", p.preview_png.name)
        return
    # Band 9 (~12.6 µm) is a common THEMIS IR display band; falls back to 1 if not available.
    run(["isis2std", f"from={p.lev2}+9", f"to={p.preview_png}", "format=PNG", "stretch=linear"],
        env=env, check=False)
    if not p.preview_png.exists():
        # Retry with band 1 if band 9 not present (some IR cubes have fewer bands).
        run(["isis2std", f"from={p.lev2}+1", f"to={p.preview_png}", "format=PNG", "stretch=linear"],
            env=env)


def _isis_to_geotiff(p: Paths, env: dict) -> Path:
    """Convert the projected ISIS cube to a multi-band GeoTIFF for downstream tools."""
    tif = p.workdir / f"{p.lev2.stem}.tif"
    if not tif.exists():
        run(["isis2std", f"from={p.lev2}", f"to={tif}", "format=tiff", "stretch=none",
             "bittype=32bit"], env=env, check=False)
    return tif


def step_dcs(p: Paths, env: dict, *, prefer_davinci: bool = True) -> None:
    """Produce a DCS color image of the projected cube.

    Tries Davinci first if installed; falls back to a pure-Python implementation
    that uses rasterio + numpy.
    """
    if p.dcs_png.exists() and p.dcs_tif.exists():
        log.info("skip DCS: %s + %s already exist", p.dcs_png.name, p.dcs_tif.name)
        return

    if prefer_davinci and _has_davinci(env):
        # Davinci 3.0.1 (built with HDF5+FITS) reads ISIS3 .cub directly via
        # read_geo(), which shells out to gdalinfo/gdal_translate. No need to
        # convert through FITS.
        script = Path(__file__).parent / "themis_dcs.dv"
        if script.exists():
            run(["davinci", "-q", "-f", str(script),
                 f"cub={p.lev2}", f"png={p.dcs_png}", f"tif={p.dcs_tif}"],
                env=env, check=False)
            if p.dcs_png.exists():
                return

    log.info("falling back to Python DCS (rasterio + numpy)")
    tif_in = _isis_to_geotiff(p, env)
    if not tif_in.exists():
        log.warning("could not produce a GeoTIFF for DCS — skipping")
        return
    sys.path.insert(0, str(Path(__file__).parent))
    from dcs_python import dcs as run_dcs  # local import to avoid hard dep on rasterio
    run_dcs(tif_in, p.dcs_png, p.dcs_tif)


ATM_STEPS_DIR = Path("/mnt/themis-processing/atm_steps")


def step_atm_correct(p: Paths, env: dict, *,
                      minlat: float = -90.0, maxlat: float = 90.0,
                      albedo: float = 0.25) -> None:
    """Run Rogers' atmospheric correction (themis_tes_overlay + themis_correct).

    Requires the four files in ATM_STEPS_DIR (PDART_mineral_library.hdf,
    TES_atmlib.hdf, themis_tes_overlay.dav, themis_correct.dav) and network
    access to http://app-davinci.mars.asu.edu/Davinci/PSQLQueryTool.
    """
    if p.atm_dcs_png.exists() and p.atm_envi.exists():
        log.info("skip atm correction: %s + %s already exist",
                 p.atm_dcs_png.name, p.atm_envi.name)
        return
    if not _has_davinci(env):
        log.warning("davinci not in the configured env PATH — skipping atm correction")
        return
    required = ["PDART_mineral_library.hdf", "TES_atmlib.hdf",
                "themis_tes_overlay.dav", "themis_correct.dav"]
    for name in required:
        if not (ATM_STEPS_DIR / name).exists():
            log.warning("missing %s in %s — skipping atm correction",
                        name, ATM_STEPS_DIR)
            return

    # Rogers' themis_correct reads `<id>.cub` (not `<id>.lev2.cub`) and feeds
    # the radiance into thm.themis_emissivity, which uses the temp_rad_v4
    # lookup table. The lookup table expects radiance in W/(m²·sr·µm), but
    # ISIS thm2isis output radiance is scaled by ~1e5 from that convention.
    # We use ISIS `algebra` to produce a divided-by-1e5 cube at the canonical
    # name <id>.cub for the Davinci pipeline to consume.
    THEMIS_RAD_SCALE = 1.0e-5
    canonical = p.workdir / f"{p.stem}.cub"
    if not canonical.exists():
        run(["algebra", f"from={p.lev2}", f"to={canonical}",
             "operator=unary", f"A={THEMIS_RAD_SCALE}"],
            env=env, check=False)
        if not canonical.exists():
            # Fall back to a copy if algebra failed.
            log.warning("algebra failed; copying unscaled cube — atm correction may give zero output")
            shutil.copyfile(p.lev2, canonical)

    script = Path(__file__).parent / "themis_atm_correct.dv"
    run(["davinci", "-q", "-f", str(script),
         f"id={p.stem}",
         f"workdir={p.workdir}",
         f"minlib={ATM_STEPS_DIR / 'PDART_mineral_library.hdf'}",
         f"atmlib={ATM_STEPS_DIR / 'TES_atmlib.hdf'}",
         f"tes_overlay={ATM_STEPS_DIR / 'themis_tes_overlay.dav'}",
         f"atm_correct={ATM_STEPS_DIR / 'themis_correct.dav'}",
         f"minlat={minlat}", f"maxlat={maxlat}", f"albedo={albedo}"],
        env=env, check=False)


def process(edr: Path, workdir: Path, *,
             skip_davinci: bool = False,
             with_atm_correction: bool = False,
             atm_minlat: float = -90.0,
             atm_maxlat: float = 90.0,
             atm_albedo: float = 0.25) -> Paths:
    workdir.mkdir(parents=True, exist_ok=True)
    paths = Paths.from_edr(edr.resolve(), workdir.resolve())
    env = ensure_isis_env()
    log.info("processing %s → %s", paths.edr.name, paths.workdir)
    step_thm2isis(paths, env)
    step_spiceinit(paths, env)
    step_cam2map(paths, env)
    step_isis2std(paths, env)
    if not skip_davinci:
        step_dcs(paths, env)
    if with_atm_correction:
        step_atm_correct(paths, env,
                          minlat=atm_minlat, maxlat=atm_maxlat, albedo=atm_albedo)
    log.info("done. outputs:")
    rows = [("lev1 cube", paths.lev1), ("lev2 cube", paths.lev2),
            ("preview", paths.preview_png), ("dcs png", paths.dcs_png),
            ("dcs tif", paths.dcs_tif)]
    if with_atm_correction:
        rows += [("atm dcs png", paths.atm_dcs_png), ("atm envi", paths.atm_envi)]
    for label, path in rows:
        status = "✓" if path.exists() else "—"
        log.info("  %s %s: %s", status, label, path)
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("edr", type=Path, help="Path to THEMIS IR EDR (.QUB file)")
    parser.add_argument("--workdir", type=Path, default=Path("work"),
                        help="Output directory for intermediates (default: ./work)")
    parser.add_argument("--skip-davinci", action="store_true",
                        help="Skip the Davinci DCS post-processing step")
    parser.add_argument("--with-atm-correction", action="store_true",
                        help="Run Rogers' atmospheric correction (themis_tes_overlay + themis_correct). "
                             "Requires network access to ASU's public TES query service.")
    parser.add_argument("--atm-minlat", type=float, default=-90.0,
                        help="TES overlay min latitude filter (default: image lat min, fallback -90)")
    parser.add_argument("--atm-maxlat", type=float, default=90.0,
                        help="TES overlay max latitude filter (default: image lat max, fallback +90)")
    parser.add_argument("--atm-albedo", type=float, default=0.25,
                        help="TES overlay max albedo filter (default 0.25)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        process(args.edr, args.workdir,
                 skip_davinci=args.skip_davinci,
                 with_atm_correction=args.with_atm_correction,
                 atm_minlat=args.atm_minlat,
                 atm_maxlat=args.atm_maxlat,
                 atm_albedo=args.atm_albedo)
    except Exception as e:
        log.error("pipeline failed: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
