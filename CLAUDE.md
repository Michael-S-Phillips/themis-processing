# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this directory is

This is a **workspace directory** containing both the original THEMIS-processing runbook (`Get themis processing up and running.docx`) and a **working, automated implementation** of that pipeline:

- `pipeline/themis_pipeline.py` — Python CLI that runs the full ingest → project → preview → DCS chain on a THEMIS IR EDR (.QUB).
- `pipeline/themis_dcs.dv` — Davinci script that produces the DCS color image and the projected GeoTIFF.
- `pipeline/build_davinci.sh` — Repeatable Davinci 3.0.1 source build via SVN (user-prefix, no sudo).
- `pipeline/dcs_python.py` — pure-Python (rasterio + numpy) DCS fallback for when Davinci isn't available.
- `test_data/I01269007EDR.QUB` — Tharsis-region THEMIS IR EDR (Mars Odyssey orbit 01269, 2002-03-29) used for pipeline validation.
- `work_full/` — outputs from the validated end-to-end run.

The `.docx` is still useful as the canonical reference, but the path/version specifics in it are JHU/APL-era artifacts; the working version is the Python pipeline.

## v2 pipeline (2026-11-16): calibrated RDR input — read this first

`pipeline/themis_pipeline.py` is now v2. v1 (kept as `pipeline/themis_pipeline_legacy_edr.py`) fed raw
8-bit EDR data numbers x 1e-5 into the atmospheric correction as if they were radiance: `thm2isis`
does NOT calibrate EDRs. v2 takes the team-calibrated RDR (`<ID>RDR.QUB`, W cm-2 sr-1 um-1), applies
no rescaling, and runs a units gate (band-9 BT vs PDS index temperatures) before the atmospheric
correction. It refuses EDR input unless `--allow-uncalibrated-edr`. Every step is checked and each
scene writes `<ID>.manifest.json`. `--ingest runbook` uses Rogers' `thm_pre_process.dv` /
`thm_post_process.dv` instead. Details and the validation plan:
`massif-themis-analysis/wiki/methods/themis-pipeline-validation.md`. The sections below describe v1
and are kept for history; the "radiance unit scale" fix in them is wrong.

**v2.1 (2026-11-19): regional atmosphere by default.** After `themis_correct`, the pipeline runs
`pipeline/themis_emissivity_full.dv`, which writes the THEMIS emissivity E for every pixel, computed
exactly as inside `themis_correct`. It then runs `pipeline/regional_atm.py`. For each image row this
takes the median of the per-footprint transmittances (`atm` in `_tes.hdf`) over the footprints within
a 3° latitude window, and sets surf = E / T(row). In `<id>_tes.hdf`, `surf` is now the regional
product. `surf_footprint` keeps `themis_correct`'s per-footprint result, and `emiss` and `t_regional`
are stored too. Coverage is about 4x larger (whole strip, not just under footprints); files are
~6x larger. Flags: `--atm-mode {regional,footprint}`, `--atm-window-deg 3.0`,
`--atm-min-footprints 5`. Why 3° and why regional: validation wiki §4.2 (cross-scene repeatability
with independent TES orbits). Production uses `--ingest runbook` (chain C radiance processing).
`_atm.dcs.png` is still made from the per-footprint result.
Footprints with non-finite `atm` are left out of the median (fixed 2026-11-20). `themis_correct` gives
atm = +inf to footprints that have no THEMIS pixels, for example those outside a cropped lev2. Before
the fix this produced T = 0 and `surf` = inf in cropped scenes. `regional_atm.apply(..., redo=True)`
recomputes `surf` from the stored `surf_footprint` and `emiss` without rerunning davinci.

**2.1.1 production notes (2026-11-23/25).**
- *Download failures.* With `--ingest runbook`, `thm_pre_process.dv` downloads from ASU. An empty lev1 is
  reported as `fetch_failed` (before 2.1.1 it surfaced as spiceinit "Unable to open" = `command_failed`).
  `massif-themis-analysis/scripts/run_pipeline_batch.py` retries `fetch_failed` 3 times per scene
  (waits 5 and 10 min); rerunning the batch retries every scene that is not `ok` at the current version.
- *Partial-band observations.* Some IR observations carry only some filters (seen: 3-10, and 4/9/10);
  every Davinci step indexes bands by position (`[,,1:9]`, band 9 = 12.57 um). Bands are now located by
  BandBin `FilterNumber`: step `band_check` fails a scene as `partial_bands` if any WAC/CSFM filter
  (3, 4, 5, 7, 8, 9) is missing, and step `pad_bands` rewrites the canonical `<id>.cub` in the full
  ten-filter layout with the missing filters as ISIS NULL (BandSequential Real only; History moved).
  The units gate reads filter 9, not position 9.
- *TES service outages.* `themis_tes_overlay` returns an empty result, not null, when ASU's TES service
  is down, which used to be recorded as `no_tes_footprints`. `step_atm` now probes the service first and
  fails as `tes_query_failed` (retry later) if it is unreachable. `no_tes_footprints` recorded during an
  outage may be wrong; rerun them.
- *TES database faults (2026-11-25).* The service can be up while its database fails: `PSQLTESQueryTool`
  answers HTTP 200 with a body `SQL Error: ERROR: could not read block ... Input/output error` (indexed
  lookups by orbit still worked; any latitude scan failed), and the overlay reads that as zero rows. When
  `step_atm` gets no footprints or no `_tes.hdf`, `tes_db_error()` now runs a latitude-band count on
  `tes_4_jmars`; an SQL error or no answer fails the scene as `tes_query_failed`. Quick manual check:
  `curl -G http://app-davinci.mars.asu.edu//Davinci/PSQLTESQueryTool --data-urlencode "queryString=select
  count(*) from tes_4_jmars where latitude between -30 and -29" --data-urlencode displayHeader=1`.
- *Partial-band scenes in the regional atmosphere (2026-11-25).* `regional_atm.py` required a finite
  atmosphere in all 9 bands, so padded 8-band scenes lost every footprint and failed `atm_empty`
  ("regional atmosphere left no valid pixels"). It now uses only the bands present (`present_bands`);
  absent bands are 0 in `surf`. Ten-band output is bit-identical (tested on I06456003). Scenes that already
  have a converted `_tes.hdf` can be redone offline with `regional_atm.apply(..., redo=True)`.
- The version stays 2.1.1: nothing changes for ten-band scenes, and a bump would reprocess every `ok`
  scene.

## Git

Since 2026-11-25 the git dir is `.git/` inside this directory on the share (before that `.git` was a file
pointing to `/home/phillipsm/git-repos/themis-processing.git` on the retired Linux workstation, whose
commits after the GitHub initial commit were not recoverable). `core.filemode` is false because the share
has no executable bit. The GitHub remote `origin` holds only the initial commit until it is pushed.

## Running the pipeline

```bash
# One-time per shell session:
source ~/miniconda3/etc/profile.d/conda.sh
conda activate isis
# (themis_pipeline.py auto-sources ~/local/davinci/etc/davinci.env when it spawns davinci)

# Process an EDR end-to-end:
python /mnt/themis-processing/pipeline/themis_pipeline.py \
    /mnt/themis-processing/test_data/I01269007EDR.QUB \
    --workdir /mnt/themis-processing/work_full

# Skip the Davinci DCS step (just ISIS lev1 + lev2 + IR preview PNG):
python /mnt/themis-processing/pipeline/themis_pipeline.py <edr.QUB> \
    --workdir <dir> --skip-davinci
```

The pipeline is idempotent — rerunning skips steps whose outputs already exist.

Outputs (for input `IxxxxxEDR.QUB`):

| File | Stage | Size (typical for IR EDR) |
| --- | --- | --- |
| `Ixxxxx.lev1.cub` | `thm2isis` (ingest) | ~90 MB |
| `Ixxxxx.lev1.cub` + SPICE | `spiceinit` (in-place) | (unchanged) |
| `Ixxxxx.lev2.cub` | `cam2map` (sinusoidal projection) | ~400 MB |
| `Ixxxxx.lev2.png` | `isis2std` band-9 quicklook | ~10 MB grayscale |
| `Ixxxxx.dcs.png` | Davinci DCS (bands 8/7/5) | ~6 MB RGB |
| `Ixxxxx.dcs.tif` | Davinci DCS GeoTIFF | ~13 MB RGB |

## How the install pieces fit together

- **ISIS 9.0.0** lives in conda env `isis` (`~/miniconda3/envs/isis/`). Created with `conda create -n isis -c conda-forge -c usgs-astrogeology --solver=libmamba isis rasterio pillow`. The activation hook at `~/miniconda3/envs/isis/etc/conda/activate.d/isis-vars.sh` sets `ISISROOT`, `ISISDATA`, and `ALESPICEROOT`.
- **ISIS SPICE data** lives at `~/isis_data/` (`base/` + `odyssey/`, ~28 GB total). Refresh with `downloadIsisData base $ISISDATA` and `downloadIsisData odyssey $ISISDATA`. The full ISIS distribution is ~500 GB — only `base` and `odyssey` (for the THEMIS instrument on Mars Odyssey) are needed.
- **Davinci 3.01** lives at `~/local/davinci/` (user-prefix install). Built from SVN tag `dv_3_01` at `https://oss.mars.asu.edu/svn/davinci/davinci/tags/dv_3_01` using `pipeline/build_davinci.sh`. The matching library tag at `davinci_library/tags/dv_3_01` ships `themis_atm_support/{TES_atmlib_73channel.hdf, THEMIS_ATM_mineral_library.hdf}` plus `bin/thm_pre_process.dv`, `thm_post_process.dv`, `thm_dcs.dv`, etc.
- **Source** the env with `source ~/local/davinci/etc/davinci.env`. It exports `DV_HOME`, `DV_LIB`, `DV_SCRIPT_FILES`, `DV_OS`, `DV_VIEWER`, `DV_GDAL_PATH`, and prepends `~/local/davinci/bin` + `$DV_LIB/bin` to `PATH`. **`DV_GDAL_PATH` is critical** — `read_geo()` in `map_projection.dvrc` shells out to `$DV_GDAL_PATH/gdalinfo` and `gdal_translate`; we point it at the ISIS conda env so the GDAL plugin set is consistent.

## Build gotchas that bit us (in case you need to rebuild Davinci)

The Davinci 3.0.1 source predates gcc 14, so a modern toolchain rejects code that older compilers accepted. The flags below make it compile cleanly on conda's gcc 15.2 — they're already baked into `build_davinci.sh`:

```
CFLAGS=-std=gnu89 -Wno-error -Wno-implicit-int -Wno-implicit-function-declaration \
       -Wno-incompatible-pointer-types -Wno-int-conversion -Wno-return-mismatch -fcommon ...
LIBS=-lpng -lz                     # iomedley/io_png.c isn't auto-linked against libpng
LDFLAGS=-Wl,-rpath,$CONDA_PREFIX/lib
```

Also: do **not** use `make -j8` for the davinci_core build — there's a race where `davinci` (the binary) links before `libdavinci.la` finishes. Use `make -j1` or `make libdavinci.la && make` to be safe. (The build script does the safe ordering.)

## Davinci scripting quirks we hit

- **`read_geo(path.cub)`** can ingest ISIS3 cubes natively when `DV_GDAL_PATH` points at a working gdalinfo + gdal_translate. Returns a struct with `.data`, `.w`, `.h`, `.ul/ur/ll/lr`, `.resolution`, etc.
- **ISIS3 NULL is `-3.40282e+38`.** `dcs(..., ignore=ISIS3_NULL)` does NOT work reliably because of float-precision mismatches between Davinci's parsed literal and the actual sentinel in the data. The reliable pattern is `masked[where(masked < -1e30)] = 0; dcs(stack, ignore=0)`.
- **Don't name script command-line args `png` or `tif`** — they collide with Davinci's format tokens. Use the `opt = {png="",tif=""}; opt = getopt($ARGV, opt)` pattern instead and access via `opt.png`. The runbook scripts (`thm_pre_process.dv` etc.) all use this pattern.
- **Use `davinci -q`** when invoking from a script — without `-q`, Davinci tries to read a per-directory `.dvrc` history file, which causes spurious errors when run from arbitrary working directories.

## Atmospheric correction

Working end-to-end via `--with-atm-correction`. Uses Dr Rogers' scripts and reference files in `/mnt/themis-processing/atm_steps/` (`PDART_mineral_library.hdf`, `TES_atmlib.hdf`, `themis_tes_overlay.dav`, `themis_correct.dav`), plus the public TES catalog at `http://app-davinci.mars.asu.edu/Davinci/PSQLQueryTool` (public `davinci/davinci` credentials).

A typical full run for a single observation takes ~3 hours: 30 s ingest + 3 min projection + 2 min DCS + ~2.5 hr for the atm correction loop (one iteration per overlapping TES footprint — usually 1500–2500 for an equatorial image at 97 m/pix). Memory peaks around 2 GB.

Outputs (in addition to the basic-pipeline ones):

| File | Stage |
| --- | --- |
| `tesover_<id>.hdf` | TES footprints overlapping the image (query result) |
| `<id>_tes.hdf` | Atmospherically-corrected emissivity cube (key `surf`, 9 bands) + atm transmittance |
| `<id>_atm.dcs.png` | DCS image of the corrected cube |
| `<id>.cub` | Scale-divided lev2 cube (intermediate, fed to themis_correct) |

Three nontrivial fixes were needed to get this working — flag them if you ever rebuild the pipeline:

1. **Radiance unit scale (WRONG — see v2 section: the input was uncalibrated EDR DN, not radiance).** ISIS `thm2isis` produces calibrated THEMIS IR radiance ~1×10⁵ larger than what `temp_rad_v4` (the brightness-temperature lookup that `thm.themis_emissivity` reads) expects. Empirically, dividing the lev2 cube by 1.0e5 brings band-9 radiance into the 1×10⁻³ W m⁻² sr⁻¹ µm⁻¹ range that produces sensible 270–330 K brightness temperatures from Mars equatorial surfaces. The pipeline calls `algebra A=1e-5` on the lev2 cube to produce `<id>.cub` before invoking `themis_correct`.
2. **Negative `miny` from out-of-bounds TES bounds.** `themis_correct` computes `miny = proj_geocoord(0,tes.maxlat,geostruct)[2]` and uses `[,miny:maxy,]` to slice the image. If `tes.maxlat` is outside the image's latitude extent (e.g. our image runs lat -12.3..-0.18 but the runbook example uses maxlat=0), `miny` lands at a negative pixel y, the slice degenerates to an empty array, and the whole correction silently fills with zeros. `pipeline/themis_atm_correct.dv` strips `minlat`/`maxlat` from the tes struct before passing it to `themis_correct` so the latter falls back to `miny=1, maxy=image.h`.
3. **`PROJ_DATA` must be inherited.** `shape2georaster` shells out to `ogr2ogr` to reproject TES polygons; `ogr2ogr` needs `PROJ_DATA` (the conda-isis activation hook sets this) to read the longlat→sinusoidal SRS definitions. Run the pipeline from an activated `isis` conda env so this is inherited; bare `source davinci.env` is not enough.

If any of those three break in a future run, the symptom is the same: a 1 MB `<id>_tes.hdf` with `surf` all zero. Check the lev2 cube's scale, the lat-bounds passed to `themis_tes_overlay`, and `env | grep PROJ` inside the python subprocess that invokes davinci.

### Per-spectrum visualization

`pipeline/extract_spectra.py <projected.cub>` produces a 6-pixel radiance-spectrum panel. `pipeline/extract_corrected_spectra.py --tes-hdf <id>_tes.hdf --lev2 <id>.lev2.cub --out spectra.png` produces a side-by-side raw-radiance vs corrected-emissivity comparison.

## Known limitations

- **DCS background color** (raw cube DCS): pixels outside the THEMIS strip render as the DCS rotation of (0,0,0) (uniform blue) instead of black in `<id>.dcs.png`. The atm-corrected version `<id>_atm.dcs.png` has a clean black background by construction (only TES-covered pixels are nonzero).
- **Atm correction coverage**: only pixels covered by a TES footprint get a corrected emissivity. For a typical equatorial image you see ~10–15% of the strip pixels covered (the rest stay at 0).

## Important context

- **`sudo` requires a password** in this WSL2 env, so nothing in the install chain uses it. Everything lives in user-prefix locations (`~/miniconda3/envs/isis`, `~/local/davinci`, `~/isis_data`).
- **`/mnt/themis-processing` is a Windows SMB share** mounted from `\\gigas\Mars_GIS\themis-processing`. It does **not** honor `chmod` (`Operation not permitted`). Scripts here cannot have the executable bit set; invoke via `bash script.sh` or `python3 script.py`.
- The pipeline writes intermediates to `/mnt/themis-processing/work*/` (SMB) but reads/writes ISIS SPICE kernels and conda envs on the **local** `/` disk (`~/isis_data`, `~/miniconda3`). Don't put large temp data on the SMB share if you can avoid it — the round-trip is ~5–10× slower than local disk.
