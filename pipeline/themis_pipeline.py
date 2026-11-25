"""THEMIS IR production pipeline, v2 (calibrated RDR input).

    RDR (.QUB, team-calibrated radiance, W cm-2 sr-1 um-1)
      -> thm2isis -> UNITS GATE (band-9 BT vs PDS index temperatures)
      -> spiceinit -> cam2map -> [maptrim crop]
      -> <id>.cub (radiance, no rescaling)
      -> Rogers/Bandfield TES-based atmospheric correction (themis_atm_correct.dv): TES footprint
         query + per-footprint transmittances (themis_correct)
      -> [default --atm-mode regional] E for every pixel (themis_emissivity_full.dv), then
         surf = E / T(row), T = median footprint transmittance within a 3 deg latitude window
         (regional_atm.py; per-footprint result kept as surf_footprint)
      -> QA (coverage, emissivity ranges) -> georef sidecar -> cleanup
    Every step is checked; nothing falls back silently. Each scene gets a
    <id>.manifest.json recording inputs, parameters, versions, timings and QA.

Why v2: v1 (themis_pipeline_legacy_edr.py) fed raw 8-bit EDR data numbers x 1e-5
to the atmospheric correction as if they were radiance (validation audit
2026-11-16, massif-themis-analysis/wiki/methods/themis-pipeline-validation.md).

Optional --ingest runbook replaces thm2isis/copy with A. D. Rogers'
thm_pre_process.dv / thm_post_process.dv (davinci library), i.e. the expert
recipe in atm_steps/themis_proc_with_full_atm_corr.txt. Which mode becomes
production is decided by the validation comparison.

Usage (conda env `isis`):
    python themis_pipeline.py /path/I17888007RDR.QUB --workdir work/I17888007 \
        --index-csv catalog/rdr_scene_index.csv --atm-minlat -52.4 --atm-maxlat -35.1
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

PIPELINE_VERSION = "2.1.1"          # 2.1: regional-atmosphere correction (regional_atm.py) by default
# 2026-11-24: partial-band and TES-outage fixes kept at 2.1.1. Nothing changes for 10-band scenes, so ok
# scenes are not reprocessed; runs with the fixes show the steps band_check and pad_bands in their manifests.
# 2026-11-25: an empty TES result is checked against the ASU database (tes_db_error); a database fault fails
# as tes_query_failed instead of no_tes_footprints / atm_empty.
log = logging.getLogger("themis")

ATM_STEPS_DIR = Path(os.environ.get("THEMIS_ATM_STEPS", Path(__file__).resolve().parents[1] / "atm_steps"))
DAVINCI_ENV_FILE = Path(os.environ.get("THEMIS_DAVINCI_ENV", Path.home() / "local" / "davinci" / "etc" / "davinci.env"))
# The runbook scripts (thm_pre/post_process.dv) need davinci's isis3 I/O module. On the Mac mini
# that module links ISIS 9 (x86_64 only), so it lives in a separate x86_64 davinci build
# (build_davinci_isis3_macos.sh); every other davinci call keeps using DAVINCI_ENV_FILE.
RUNBOOK_DAVINCI_ENV_FILE = Path(os.environ.get(
    "THEMIS_DAVINCI_RUNBOOK_ENV", Path.home() / "local" / "davinci-x86" / "etc" / "davinci.env"))
SPICE_WEB = os.environ.get("THEMIS_SPICEINIT_WEB", "0") == "1"   # use the USGS SPICE web service instead of local ISISDATA kernels
C1, C2 = 1.191042e8, 1.4387752e4          # Planck constants: W m-2 sr-1 um-1 um^4 ; um K


class PipelineError(RuntimeError):
    """A step failed; .status is the manifest status code."""
    def __init__(self, status: str, msg: str):
        super().__init__(msg)
        self.status = status


# ----------------------------------------------------------------------------- paths / manifest
@dataclass
class Paths:
    src: Path
    workdir: Path
    stem: str                       # 9-char THEMIS id, e.g. I17888007

    @classmethod
    def build(cls, src: Path, workdir: Path) -> "Paths":
        m = re.match(r"^(I\d{8})", src.name, flags=re.I)
        if not m:
            raise PipelineError("bad_input", f"cannot parse THEMIS IR id from {src.name}")
        return cls(src=src, workdir=workdir, stem=m.group(1).upper())

    def f(self, suffix: str) -> Path:
        return self.workdir / f"{self.stem}{suffix}"

    @property
    def lev1(self): return self.f(".lev1.cub")
    @property
    def lev2(self): return self.f(".lev2.cub")
    @property
    def canonical(self): return self.f(".cub")             # what themis_correct reads
    @property
    def tes_hdf(self): return self.f("_tes.hdf")
    @property
    def emiss_hdf(self): return self.f("_emiss.hdf")
    @property
    def tesover(self): return self.workdir / f"tesover_{self.stem}.hdf"
    @property
    def georef(self): return self.f(".georef.json")
    @property
    def manifest(self): return self.f(".manifest.json")


@dataclass
class Manifest:
    path: Path
    data: dict = field(default_factory=dict)

    @classmethod
    def load_or_new(cls, path: Path) -> "Manifest":
        if path.exists():
            try:
                return cls(path, json.loads(path.read_text()))
            except Exception:
                pass
        return cls(path, {"steps": {}})

    def step(self, name: str, **kv):
        self.data.setdefault("steps", {}).setdefault(name, {}).update(kv)
        self.save()

    def save(self):
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2, default=str))
        tmp.replace(self.path)


# ----------------------------------------------------------------------------- helpers
def run(cmd: list[str], env: dict, *, cwd: Path | None = None, check: bool = True) -> str:
    """Run a command, log its output, raise on non-zero exit (unless check=False)."""
    log.info("$ %s", " ".join(map(str, cmd)))
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=cwd, errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.splitlines():
        log.info("  %s", line)
    if check and r.returncode != 0:
        raise PipelineError("command_failed", f"exit {r.returncode}: {' '.join(map(str, cmd))}")
    return out + f"\n__returncode__={r.returncode}"


def atomic_isis(cmd_builder, final: Path, env: dict, **kw) -> str:
    """Run an ISIS command that writes `to=`, via a temp name, then rename."""
    tmp = final.with_name(final.stem + ".partial.cub")
    if tmp.exists():
        tmp.unlink()
    out = run(cmd_builder(tmp), env, **kw)
    if not tmp.exists() or tmp.stat().st_size == 0:
        raise PipelineError("command_failed", f"{final.name}: command produced no output")
    tmp.replace(final)
    return out


def md5(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def product_type(src: Path) -> str:
    """'RDR' or 'EDR' from the PDS label, not just the file name."""
    with open(src, "rb") as fh:
        head = fh.read(32768).decode("latin1", errors="replace")
    m = re.search(r"DATA_SET_ID\s*=\s*\"?([A-Z0-9\-]+)", head)
    ds = m.group(1) if m else ""
    if "IRRDR" in ds:
        return "RDR"
    if "IREDR" in ds:
        return "EDR"
    raise PipelineError("bad_input", f"{src.name}: not a THEMIS IR EDR/RDR (DATA_SET_ID={ds!r})")


WAC_FILTERS = (3, 4, 5, 7, 8, 9)               # THEMIS bands WAC/CSFM need (paper band set)


def _isis_label(path: Path) -> str:
    with open(path, "rb") as fh:
        return fh.read(1 << 20).split(b"\x00")[0].decode("latin1", errors="replace")


def cube_filters(path: Path) -> list[int]:
    """THEMIS filter (band) numbers of a cube's bands, in file order, from BandBin FilterNumber.

    Most IR observations carry all ten filters, but some were acquired with a subset (e.g. 3-10 or
    4, 9, 10). Positions then differ from THEMIS band numbers, so bands are looked up by filter."""
    m = re.search(r"FilterNumber\s*=\s*\(([^)]*)\)", _isis_label(path), re.S)
    if not m:
        raise PipelineError("bad_input", f"{path.name}: no BandBin FilterNumber in the label")
    return [int(float(x)) for x in m.group(1).replace("\n", " ").split(",") if x.strip()]


def check_filters(path: Path) -> list[int]:
    """Partial-band observations are fine only if every WAC/CSFM band is present."""
    f = cube_filters(path)
    missing = [b for b in WAC_FILTERS if b not in f]
    if missing:
        raise PipelineError("partial_bands", f"filters {f}: missing WAC/CSFM bands {missing}")
    return f


# THEMIS IR BandBin for the full ten-filter layout (from the labels of ten-band cubes)
THEMIS_BANDBIN = {"OriginalBand": list(range(1, 11)), "FilterNumber": list(range(1, 11)),
                  "BandNumber": list(range(1, 11)),
                  "Center": [6.78, 6.78, 7.93, 8.56, 9.35, 10.21, 11.04, 11.79, 12.57, 14.88],
                  "Width": [1.01, 1.01, 1.09, 1.16, 1.2, 1.1, 1.19, 1.07, 0.81, 0.87]}
ISIS_NULL_F4 = np.frombuffer(bytes.fromhex("fbff7fff"), "<f4")[0]     # ISIS3 Real NULL (0xFF7FFFFB)


def pad_to_ten_bands(cube: Path) -> dict:
    """Rewrite a partial-band cube in the full ten-filter layout, missing filters as ISIS NULL.

    themis_correct, themis_emissivity_full.dv and everything downstream index THEMIS bands by
    position ([,,1:9], band 9 = 12.57 um). A cube acquired with filters 3-10 would put 14.88 um in
    position 9. Only BandSequential Real cubes (what thm_post_process.dv writes) are handled; the
    label keeps its size, objects after the core (History) are moved and their StartByte updated."""
    f = cube_filters(cube)
    if f == list(range(1, 11)):
        return {"filters": f, "padded": False}
    raw = cube.read_bytes()
    lab_end = raw.index(b"\x00") if b"\x00" in raw[:1 << 20] else None
    txt = raw[:lab_end].decode("latin1")
    core = txt[txt.find("Object = Core"):]
    g = lambda k: re.search(r"^\s*" + k + r"\s*=\s*(\S+)", core, re.M).group(1)
    if g("Format") != "BandSequential" or g("Type") != "Real" or g("ByteOrder") != "Lsb":
        raise PipelineError("partial_bands", f"cannot pad {cube.name}: {g('Format')} {g('Type')} {g('ByteOrder')}")
    S, L, B, start = int(g("Samples")), int(g("Lines")), int(g("Bands")), int(g("StartByte")) - 1
    if B != len(f):
        raise PipelineError("partial_bands", f"{cube.name}: Bands={B} but {len(f)} filter numbers")
    nb = S * L * 4
    core_end = start + B * nb
    new_core = bytearray(np.full(S * L * 10, ISIS_NULL_F4, "<f4").tobytes())
    for i, flt in enumerate(f):
        new_core[(flt - 1) * nb:flt * nb] = raw[start + i * nb:start + (i + 1) * nb]
    shift = (10 - B) * nb
    # label edits: band count, BandBin arrays, StartByte of every object stored after the core
    new = re.sub(r"(Object = Core.*?Bands\s*=\s*)\d+", lambda m: m.group(1) + "10", txt, count=1, flags=re.S)
    for k, v in THEMIS_BANDBIN.items():
        new = re.sub(r"(\n\s*" + k + r"\s*=\s*)\([^)]*\)", lambda m: m.group(1) + "(" + ", ".join(map(str, v)) + ")",
                     new, count=1)
    def move(m):
        sb = int(m.group(2))
        return m.group(1) + str(sb + shift if sb - 1 >= core_end else sb)
    new = re.sub(r"(\n\s*StartByte\s*=\s*)(\d+)", move, new)
    lab = new.encode("latin1")
    if len(lab) > start:
        raise PipelineError("partial_bands", f"{cube.name}: padded label ({len(lab)} B) exceeds {start} B")
    out = lab + b"\x00" * (start - len(lab)) + bytes(new_core) + raw[core_end:]
    tmp = cube.with_suffix(".pad.partial")
    tmp.write_bytes(out)
    tmp.replace(cube)
    return {"filters": f, "padded": True, "null_filters": [b for b in range(1, 11) if b not in f]}


def read_isis_band(path: Path, band: int, *, by_filter: bool = False) -> np.ndarray:
    """Read one band of an ISIS3 cube (Tile or BandSequential); specials -> NaN.

    band is the 1-based position in the file, or the THEMIS filter number with by_filter=True."""
    if by_filter:
        f = cube_filters(path)
        if band not in f:
            raise PipelineError("partial_bands", f"{path.name}: no filter {band} (filters {f})")
        band = f.index(band) + 1
    txt = _isis_label(path)
    core = txt[txt.find("Object = Core"):]
    g = lambda k: re.search(r"^\s*" + k + r"\s*=\s*(\S+)", core, re.M).group(1)
    S, L = int(g("Samples")), int(g("Lines"))
    start = int(g("StartByte")) - 1
    typ = {"Real": "f4", "SignedWord": "i2", "UnsignedByte": "u1", "UnsignedWord": "u2"}[g("Type")]
    dt_ = np.dtype(("<" if g("ByteOrder") == "Lsb" else ">") + typ)
    base, mult = float(g("Base")), float(g("Multiplier"))
    out = np.empty((L, S), np.float32)
    with open(path, "rb") as fh:
        if g("Format") == "Tile":
            ts, tl = int(g("TileSamples")), int(g("TileLines"))
            nts, ntl = -(-S // ts), -(-L // tl)
            tb = ts * tl * dt_.itemsize
            for r in range(ntl):
                for c in range(nts):
                    fh.seek(start + (((band - 1) * ntl + r) * nts + c) * tb)
                    t = np.frombuffer(fh.read(tb), dtype=dt_).reshape(tl, ts)
                    out[r*tl:min((r+1)*tl, L), c*ts:min((c+1)*ts, S)] = t[:min(tl, L-r*tl), :min(ts, S-c*ts)]
        else:
            nb = S * L * dt_.itemsize
            fh.seek(start + (band - 1) * nb)
            out[:] = np.frombuffer(fh.read(nb), dtype=dt_).reshape(L, S)
    if typ == "f4":
        out[(out < -1e30) | (out > 1e30)] = np.nan
    else:
        out = out * mult + base
    return out


def brightness_temp(L_w_m2_um: np.ndarray, lam_um: float) -> np.ndarray:
    return C2 / (lam_um * np.log1p(C1 / (lam_um ** 5 * L_w_m2_um)))


def ensure_env() -> dict:
    for b in ("thm2isis", "spiceinit", "cam2map", "maptrim"):
        if shutil.which(b) is None:
            raise PipelineError("env", f"{b} not on PATH; `conda activate isis` first")
    env = os.environ.copy()
    if not env.get("ISISDATA"):
        cand = Path.home() / "isis_data"
        if cand.exists():
            env["ISISDATA"] = str(cand)
    if DAVINCI_ENV_FILE.exists():
        out = subprocess.run(["bash", "-c", f"source {DAVINCI_ENV_FILE} && env"],
                             capture_output=True, text=True, env=env).stdout
        for line in out.splitlines():
            if "=" in line:
                k, _, v = line.partition("="); env[k] = v
    # Batch runs must not pop up windows: themis_tes_overlay.dav calls display(), which writes an
    # extension-less PNG to $TMPDIR/dv_<pid>/ and runs $DV_VIEWER on it (`open` on macOS -> TextEdit).
    # THEMIS_DV_VIEWER=open restores previews for interactive debugging.
    env["DV_VIEWER"] = os.environ.get("THEMIS_DV_VIEWER", "true")
    # The active (isis) env's GDAL/OGR tools must win over any system copies (e.g. MacPorts
    # /opt/local/bin on the Mac mini, whose GDAL cannot find a PROJ database): davinci's
    # read_geo and shape2georaster shell out to gdalinfo, gdal_translate and ogr2ogr.
    if env.get("CONDA_PREFIX"):
        cbin = str(Path(env["CONDA_PREFIX"]) / "bin")
        parts = [d for d in env.get("PATH", "").split(os.pathsep) if d and d != cbin]
        env["PATH"] = os.pathsep.join([cbin] + parts)
        env.setdefault("DV_GDAL_PATH", cbin)
    if not any((Path(d) / "davinci").is_file() for d in env.get("PATH", "").split(os.pathsep)):
        raise PipelineError("env", "davinci not found (source ~/local/davinci/etc/davinci.env)")
    if "PROJ_DATA" not in env and "PROJ_LIB" not in env:
        log.warning("PROJ_DATA not set: shape2georaster/ogr2ogr may fail (run from an activated isis env)")
    return env


def runbook_env(env: dict) -> dict:
    """env for thm_pre/post_process.dv: the davinci build that has the isis3 I/O module."""
    if not RUNBOOK_DAVINCI_ENV_FILE.exists() or RUNBOOK_DAVINCI_ENV_FILE == DAVINCI_ENV_FILE:
        return env
    out = subprocess.run(["bash", "-c", f"source {RUNBOOK_DAVINCI_ENV_FILE} && env"],
                         capture_output=True, text=True, env=env).stdout
    renv = dict(env)
    for line in out.splitlines():
        if "=" in line:
            k, _, v = line.partition("="); renv[k] = v
    # Something in the x86_64 runbook path is coverage-instrumented and drops a 2.6 MB
    # default.profraw into the workdir; discard it.
    renv.setdefault("LLVM_PROFILE_FILE", "/dev/null")
    return renv


ASU_LIBRARY_SOURCE = 'source("/themis/lib/dav_lib/library/library_mars.dvrc")'


def runbook_script(renv: dict, name: str, workdir: Path) -> Path:
    """Local copy of a runbook script that loads the portable davinci library.

    thm_pre/post_process.dv start with source() of ASU's internal library_mars.dvrc
    (/themis/lib/dav_lib, ASU GDAL, ASU DB settings). library_linux.dvrc loads the same
    science libraries (themis_science, themis_isis3, thm module) but honours DV_HOME/DV_LIB/
    DV_GDAL_PATH from the environment. Nothing else in the script is changed.
    """
    src = Path(renv.get("DV_LIB", "")) / "bin" / name
    if not src.exists():
        raise PipelineError("env", f"missing {src}")
    txt = src.read_text(errors="replace")
    if ASU_LIBRARY_SOURCE not in txt:
        raise PipelineError("env", f"{src}: expected ASU library source() line not found; check the script")
    out = workdir / f"_{name}"
    out.write_text(txt.replace(ASU_LIBRARY_SOURCE, 'source($DV_LIB+"/library_linux.dvrc")', 1))
    return out


# ----------------------------------------------------------------------------- steps
def step_ingest(p: Paths, env: dict, mode: str):
    if p.lev1.exists() and p.lev1.stat().st_size == 0:
        p.lev1.unlink()                     # left by a failed runbook fetch; refetch
    if p.lev1.exists():
        return "exists"
    if mode == "rdr":
        atomic_isis(lambda t: ["thm2isis", f"from={p.src}", f"to={t}"], p.lev1, env)
    else:  # runbook: Rogers' thm_pre_process.dv takes the id and builds lev1 itself
        renv = runbook_env(env)
        run(["davinci", "-q", "-f", str(runbook_script(renv, "thm_pre_process.dv", p.workdir)),
             f"from={p.stem}", f"to={p.lev1}"], renv, cwd=p.workdir)
        if not p.lev1.exists():
            raise PipelineError("command_failed", "thm_pre_process.dv produced no lev1")
        if p.lev1.stat().st_size == 0:
            # uddw(http=1) downloads the RDR from ASU; when ASU's query/download service is down it
            # leaves an empty or non-PDS file and thm_pre_process writes a 0-byte lev1 (2026-11-20 outage)
            p.lev1.unlink()
            raise PipelineError("fetch_failed", "thm_pre_process.dv wrote an empty lev1 (ASU download failed?)")
    return "done"


def units_gate(cube: Path, t_lo: float | None, t_hi: float | None, tol: float) -> dict:
    """Band-9 BT from the cube median must agree with the PDS index temperatures.

    Catches uncalibrated input (raw EDR DN gives ~350 K) and unit/scale errors.
    """
    b9 = read_isis_band(cube, 9, by_filter=True)
    v = b9[np.isfinite(b9) & (b9 > 0)]
    if v.size < 1000:
        raise PipelineError("failed_units_gate", f"band 9 has only {v.size} valid pixels")
    frac_int = float(np.mean(v[:100000] == np.round(v[:100000])))
    T = float(np.median(brightness_temp(v * 1e4, 12.57)))      # W cm-2 -> W m-2
    if t_lo is not None and t_hi is not None and np.isfinite(t_lo) and np.isfinite(t_hi):
        lo, hi, basis = t_lo - tol, t_hi + tol, "pds_index"
    else:
        lo, hi, basis = 170.0, 320.0, "absolute_daytime_range"
    res = dict(bt_b9_median_K=round(T, 2), accept_lo_K=lo, accept_hi_K=hi, basis=basis,
               integer_fraction=round(frac_int, 3), median_radiance=float(np.median(v)))
    if frac_int > 0.99:
        raise PipelineError("failed_units_gate", f"values are integer-valued (raw DN?): {res}")
    if not (lo <= T <= hi):
        raise PipelineError("failed_units_gate", f"band-9 BT {T:.1f} K outside [{lo:.1f}, {hi:.1f}]: {res}")
    return res


def step_spiceinit(p: Paths, env: dict):
    run(["spiceinit", f"from={p.lev1}"] + (["web=true"] if SPICE_WEB else []), env)


def step_cam2map(p: Paths, env: dict, map_template: Path | None):
    if p.lev2.exists():
        return "exists"
    extra = [f"map={map_template}", "matchmap=true"] if map_template else []
    atomic_isis(lambda t: ["cam2map", f"from={p.lev1}", f"to={t}", *extra], p.lev2, env)
    return "done"


def step_crop(p: Paths, env: dict, bbox: tuple[float, float, float, float] | None):
    """maptrim CROP to the target-polygon bbox (clamped to the cube). Returns the bbox applied."""
    if bbox is None:
        return None
    import pvl
    mg = pvl.load(str(p.lev2))["IsisCube"]["Mapping"]
    marker = p.f(".lev2.cropped")
    if "MinimumLatitude" not in mg and marker.exists():
        # lev2 was cropped by an earlier run; maptrim's label has no Minimum/MaximumLatitude, and a
        # cropped cube cannot be re-cropped to a larger box anyway. Keep the recorded crop.
        c = tuple(float(x) for x in marker.read_text().strip().split(","))
        log.info("lev2 already cropped to %s (marker); keeping it", c)
        return c
    cmin, cmax = float(mg["MinimumLatitude"]), float(mg["MaximumLatitude"])
    cwest, ceast = float(mg["MinimumLongitude"]), float(mg["MaximumLongitude"])
    mnla, mxla, mnlo, mxlo = bbox
    if int(mg.get("LongitudeDomain", 360)) == 360:
        mnlo, mxlo = mnlo % 360, mxlo % 360
        if mnlo > mxlo:
            log.warning("crop bbox crosses the 0/360 seam; not cropping"); return None
    c = (max(mnla, cmin), min(mxla, cmax), max(mnlo, cwest), min(mxlo, ceast))
    if c[0] >= c[1] or c[2] >= c[3]:
        raise PipelineError("crop_empty", f"crop bbox {bbox} does not overlap cube {cmin,cmax,cwest,ceast}")
    marker = p.f(".lev2.cropped")
    want = ",".join(f"{x:.4f}" for x in c)
    if marker.exists() and marker.read_text().strip() == want:
        return c
    tmp = p.f(".crop.partial.cub")
    if tmp.exists():
        tmp.unlink()
    run(["maptrim", f"from={p.lev2}", f"to={tmp}", "mode=CROP",
         f"minlat={c[0]}", f"maxlat={c[1]}", f"minlon={c[2]}", f"maxlon={c[3]}"], env)
    tmp.replace(p.lev2)
    marker.write_text(want + "\n")
    return c


def step_canonical(p: Paths, env: dict, mode: str):
    """<id>.cub = radiance cube read by themis_correct. RDR mode: exact copy, no scaling."""
    if p.canonical.exists():
        return "exists"
    if mode == "rdr":
        tmp = p.f(".canon.partial.cub")
        shutil.copyfile(p.lev2, tmp)          # SMB share: no symlinks
        tmp.replace(p.canonical)
    else:
        renv = runbook_env(env)
        run(["davinci", "-q", "-f", str(runbook_script(renv, "thm_post_process.dv", p.workdir)),
             f"from={p.lev2}", f"to={p.canonical}"], renv, cwd=p.workdir)
        if not p.canonical.exists():
            raise PipelineError("command_failed", "thm_post_process.dv produced no output")
    return "done"


TES_SERVICE = "http://app-davinci.mars.asu.edu/Davinci/PSQLQueryTool"


def tes_service_up(timeout: float = 30.0) -> bool:
    """themis_tes_overlay returns an empty result, not null, when the ASU TES service is down, which
    would be recorded as no_tes_footprints. Any HTTP answer counts as up."""
    import urllib.error
    import urllib.request
    try:
        urllib.request.urlopen(TES_SERVICE, timeout=timeout).close()
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


TES_QUERY = "http://app-davinci.mars.asu.edu//Davinci/PSQLTESQueryTool"


def tes_db_error(minlat: float, maxlat: float, timeout: float = 120.0) -> str | None:
    """Ask the TES footprint table for a latitude-band count, as the overlay's query scans it. The ASU
    service answers database failures with HTTP 200 and a 'SQL Error: ...' body, which themis_tes_overlay
    reads as zero rows (seen 2026-11-23/25: 'could not read block ... Input/output error'). Returns the
    error text, or None if the table answered. Only called when a scene came back with no footprints."""
    import urllib.parse
    import urllib.request
    q = (f"select count(*) from tes_4_jmars where latitude between {min(minlat, maxlat):.3f} "
         f"and {max(minlat, maxlat):.3f}")
    url = TES_QUERY + "?" + urllib.parse.urlencode({"queryString": q, "displayHeader": 1})
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read(4096).decode("utf-8", "replace")
    except Exception as e:                            # unreachable or timed out: also not a real "none"
        return f"TES query failed: {type(e).__name__}: {e}"
    return body.strip().splitlines()[0][:200] if "SQL Error" in body or "ERROR" in body else None


def step_atm(p: Paths, env: dict, minlat: float, maxlat: float, albedo: float) -> dict:
    if p.tes_hdf.exists() and p.tes_hdf.stat().st_size > 500_000:
        return {"skipped": "exists"}
    if not tes_service_up():
        raise PipelineError("tes_query_failed", f"ASU TES service unreachable ({TES_SERVICE}); retry later")
    for name in ("PDART_mineral_library.hdf", "TES_atmlib.hdf", "themis_tes_overlay.dav", "themis_correct.dav"):
        if not (ATM_STEPS_DIR / name).exists():
            raise PipelineError("env", f"missing {ATM_STEPS_DIR / name}")
    script = Path(__file__).parent / "themis_atm_correct.dv"
    out = run(["davinci", "-q", "-f", str(script), f"id={p.stem}", f"workdir={p.workdir}",
               f"minlib={ATM_STEPS_DIR / 'PDART_mineral_library.hdf'}",
               f"atmlib={ATM_STEPS_DIR / 'TES_atmlib.hdf'}",
               f"tes_overlay={ATM_STEPS_DIR / 'themis_tes_overlay.dav'}",
               f"atm_correct={ATM_STEPS_DIR / 'themis_correct.dav'}",
               f"minlat={minlat}", f"maxlat={maxlat}", f"albedo={albedo}"], env, cwd=p.workdir, check=False)
    # Parse the specific failure reason before looking at the exit code.
    nfp = re.findall(r"TES footprints overlapping image:\s*(\d+)", out)
    info = {"n_tes_footprints": int(nfp[-1]) if nfp else None}
    if "returned null" in out:
        raise PipelineError("tes_query_failed", "themis_tes_overlay returned null (ASU TES service?)")
    empty = bool(re.search(r"no TES footprints overlap", out)) or info["n_tes_footprints"] == 0
    if empty or not p.tes_hdf.exists() or p.tes_hdf.stat().st_size < 500_000:
        err = tes_db_error(minlat, maxlat)            # an ASU database fault looks like zero footprints
        if err:
            raise PipelineError("tes_query_failed", f"ASU TES database error, retry later: {err}")
    if empty:
        raise PipelineError("no_tes_footprints", "no TES footprints pass the overlay filters")
    if not p.tes_hdf.exists() or p.tes_hdf.stat().st_size < 500_000:
        rc = re.search(r"__returncode__=(-?\d+)", out)
        raise PipelineError("atm_empty", f"{p.tes_hdf.name} missing or < 0.5 MB (davinci exit {rc.group(1) if rc else '?'})")
    return info


def step_emissivity(p: Paths, env: dict) -> str:
    """<id>_emiss.hdf: THEMIS emissivity E for every pixel, as computed inside themis_correct."""
    import h5py
    with h5py.File(p.tes_hdf, "r") as f:
        if "emiss" in f:
            return "exists (in _tes.hdf)"
    if p.emiss_hdf.exists() and p.emiss_hdf.stat().st_size > 100_000:
        return "exists"
    script = Path(__file__).resolve().parent / "themis_emissivity_full.dv"
    run(["davinci", "-q", "-f", str(script), f"id={p.stem}", f"workdir={p.workdir}"], env, cwd=p.workdir)
    if not p.emiss_hdf.exists():
        raise PipelineError("command_failed", "themis_emissivity_full.dv produced no output")
    return "done"


def step_regional_atm(p: Paths, window_deg: float, min_footprints: int) -> dict:
    """Replace themis_correct's per-footprint surf by the regional-atmosphere surf (regional_atm.py)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import regional_atm
    if not p.tesover.exists():
        raise PipelineError("atm_empty", f"missing {p.tesover.name}")
    res = regional_atm.apply(p.tes_hdf, p.emiss_hdf, p.tesover, p.lev2, window_deg, min_footprints)
    if p.emiss_hdf.exists():
        p.emiss_hdf.unlink()                  # now stored (compressed) inside _tes.hdf
    if res.get("valid_px_regional", 1) == 0:
        raise PipelineError("atm_empty", "regional atmosphere left no valid pixels (too few footprints?)")
    return res


def step_qa(p: Paths) -> dict:
    """Coverage and emissivity sanity of the corrected cube. Needs h5py."""
    try:
        import h5py
    except ImportError:
        raise PipelineError("env", "h5py is required for QA: `pip install h5py` in the isis env")
    with h5py.File(p.tes_hdf, "r") as h:
        surf = h["surf"][()]
    valid = np.isfinite(surf[8]) & (surf[8] > 0)
    cov = float(valid.mean())
    if valid.sum() == 0:
        raise PipelineError("atm_empty", "corrected cube has no valid pixels")
    meds = [float(np.median(surf[b][valid])) for b in range(9)]
    em = surf[2:9][:, valid]                          # THEMIS bands 3-9
    out_of_range = float(np.mean(np.any((em < 0.70) | (em > 1.05), axis=0)))
    qa = dict(surf_shape=list(surf.shape), coverage_fraction=round(cov, 4),
              band_medians=[round(m, 4) for m in meds], frac_pixels_out_of_range=round(out_of_range, 4))
    qa["warnings"] = []
    if cov < 0.01:
        qa["warnings"].append("coverage < 1%")
    if not all(0.85 <= m <= 1.02 for m in meds[2:9]):
        qa["warnings"].append("band-median emissivity outside 0.85-1.02")
    if out_of_range > 0.2:
        qa["warnings"].append(">20% of pixels outside 0.70-1.05")
    return qa


def step_georef(p: Paths, surf_shape: list | None):
    """Small sidecar so extraction works after lev2 is deleted."""
    import rasterio
    with rasterio.open(str(p.lev2)) as src:
        g = dict(width=src.width, height=src.height, transform=list(src.transform)[:6],
                 crs_wkt=src.crs.to_wkt() if src.crs else None)
    if surf_shape and (surf_shape[1] != g["height"] or surf_shape[2] != g["width"]):
        raise PipelineError("shape_mismatch", f"surf {surf_shape} vs lev2 ({g['height']},{g['width']})")
    p.georef.write_text(json.dumps(g, indent=2))


def cleanup(p: Paths):
    for f in [p.lev1, p.lev2, p.canonical, p.emiss_hdf, p.f("_rematm.envi"), p.f("_rematm.envi.ers")]:
        if f.exists():
            f.unlink()


# ----------------------------------------------------------------------------- driver
def process(src: Path, workdir: Path, *, ingest: str = "rdr", allow_uncalibrated_edr: bool = False,
            index_t: tuple[float | None, float | None] = (None, None), gate_tol: float = 15.0,
            atm_minlat: float = -90.0, atm_maxlat: float = 90.0, atm_albedo: float = 0.25,
            atm_mode: str = "regional", atm_window_deg: float = 3.0, atm_min_footprints: int = 5,
            crop_bbox=None, map_template: Path | None = None, keep_intermediates: bool = False) -> dict:
    workdir.mkdir(parents=True, exist_ok=True)
    p = Paths.build(src.resolve(), workdir.resolve())
    man = Manifest.load_or_new(p.manifest)
    if man.data.get("status") == "ok" and man.data.get("pipeline_version") == PIPELINE_VERSION:
        log.info("%s already ok (v%s) — skipping", p.stem, PIPELINE_VERSION)
        return man.data
    man.data.update(scene=p.stem, pipeline_version=PIPELINE_VERSION, ingest_mode=ingest,
                    started=dt.datetime.now().isoformat(timespec="seconds"), status="running",
                    params=dict(atm_minlat=atm_minlat, atm_maxlat=atm_maxlat, atm_albedo=atm_albedo,
                                atm_mode=atm_mode, atm_window_deg=atm_window_deg,
                                atm_min_footprints=atm_min_footprints,
                                crop_bbox_requested=crop_bbox, gate_tol_K=gate_tol,
                                index_T=index_t, map_template=str(map_template) if map_template else None),
                    error=None)
    man.save()

    def timed(name, fn, *a, **kw):
        t0 = time.time(); man.step(name, status="running", start=dt.datetime.now().isoformat(timespec="seconds"))
        res = fn(*a, **kw)
        man.step(name, status="ok", seconds=round(time.time() - t0, 1), result=res)
        return res

    try:
        env = ensure_env()
        if ingest == "rdr":
            ptype = product_type(p.src)
            man.data["input"] = dict(path=str(p.src), product_type=ptype, md5=md5(p.src), bytes=p.src.stat().st_size)
            if ptype == "EDR" and not allow_uncalibrated_edr:
                raise PipelineError("bad_input", "EDR input is uncalibrated raw DN; use the RDR. (To reproduce v1, run themis_pipeline_legacy_edr.py.)")
        timed("ingest", step_ingest, p, env, ingest)
        man.data["filters"] = timed("band_check", check_filters, p.lev1)
        if ingest == "rdr":
            timed("units_gate_lev1", units_gate, p.lev1, *index_t, gate_tol)
        timed("spiceinit", step_spiceinit, p, env)
        timed("cam2map", step_cam2map, p, env, map_template)
        applied = timed("crop", step_crop, p, env, None if map_template else crop_bbox)
        man.data["params"]["crop_bbox_applied"] = applied
        timed("canonical", step_canonical, p, env, ingest)
        timed("pad_bands", pad_to_ten_bands, p.canonical)
        timed("units_gate_canonical", units_gate, p.canonical, *index_t, gate_tol)
        timed("atm_correct", step_atm, p, env, atm_minlat, atm_maxlat, atm_albedo)
        if atm_mode == "regional":
            timed("emissivity", step_emissivity, p, env)
            timed("regional_atm", step_regional_atm, p, atm_window_deg, atm_min_footprints)
        qa = timed("qa", step_qa, p)
        man.data["qa"] = qa
        timed("georef", step_georef, p, qa.get("surf_shape"))
        if not keep_intermediates:
            timed("cleanup", cleanup, p)
        man.data["status"] = "ok"              # QA warnings, if any, are recorded in man.data["qa"]
    except PipelineError as e:
        log.error("%s: %s", e.status, e)
        man.data.update(status=e.status, error=str(e))
    except Exception as e:                      # unexpected: record, do not hide
        log.exception("unexpected failure")
        man.data.update(status="unexpected_error", error=repr(e))
    for s in man.data.get("steps", {}).values():
        if s.get("status") == "running":
            s["status"] = "failed"
    man.data["finished"] = dt.datetime.now().isoformat(timespec="seconds")
    man.save()
    return man.data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", type=Path, help="THEMIS IR RDR .QUB (or scene id with --ingest runbook)")
    ap.add_argument("--workdir", type=Path, required=True)
    ap.add_argument("--ingest", choices=["rdr", "runbook"], default="rdr")
    ap.add_argument("--allow-uncalibrated-edr", action="store_true", help="legacy reproduction only")
    ap.add_argument("--index-csv", type=Path, help="CSV with scene,idx_T22,idx_T23 (from download_rdrs.py)")
    ap.add_argument("--gate-tol", type=float, default=15.0)
    ap.add_argument("--atm-minlat", type=float, default=-90.0)
    ap.add_argument("--atm-maxlat", type=float, default=90.0)
    ap.add_argument("--atm-albedo", type=float, default=0.25)
    ap.add_argument("--atm-mode", choices=["regional", "footprint"], default="regional",
                    help="regional (default): one transmittance per image row from the TES footprints within "
                         "--atm-window-deg of latitude; footprint: themis_correct's per-footprint result")
    ap.add_argument("--atm-window-deg", type=float, default=3.0)
    ap.add_argument("--atm-min-footprints", type=int, default=5)
    ap.add_argument("--crop", type=float, nargs=4, metavar=("MINLAT", "MAXLAT", "MINLON", "MAXLON"))
    ap.add_argument("--map-template", type=Path, help="match an existing lev2 grid (validation)")
    ap.add_argument("--keep-intermediates", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")
    t = (None, None)
    if a.index_csv and a.index_csv.exists():
        import pandas as pd
        idx = pd.read_csv(a.index_csv).set_index("scene")
        sid = Paths.build(a.src, a.workdir).stem
        if sid in idx.index:
            t = (float(idx.at[sid, "idx_T22"]), float(idx.at[sid, "idx_T23"]))
    res = process(a.src, a.workdir, ingest=a.ingest, allow_uncalibrated_edr=a.allow_uncalibrated_edr,
                  index_t=t, gate_tol=a.gate_tol, atm_minlat=a.atm_minlat, atm_maxlat=a.atm_maxlat,
                  atm_albedo=a.atm_albedo, atm_mode=a.atm_mode, atm_window_deg=a.atm_window_deg,
                  atm_min_footprints=a.atm_min_footprints, crop_bbox=tuple(a.crop) if a.crop else None,
                  map_template=a.map_template, keep_intermediates=a.keep_intermediates)
    print(json.dumps({k: res.get(k) for k in ("scene", "status", "error", "qa")}, indent=2, default=str))
    return 0 if res.get("status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
