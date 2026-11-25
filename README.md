# themis-processing

Pipeline that turns a THEMIS IR observation (Mars Odyssey) into a map-projected,
atmospherically corrected surface-emissivity cube, using ISIS 9 and Davinci. It is the
processing stage for the Argyre/Hellas massif THEMIS analysis
(`../massif-themis-analysis/`), but runs on any THEMIS IR scene.

Current version: **2.1.1** (`PIPELINE_VERSION` in `pipeline/themis_pipeline.py`).

## What it does

```
THEMIS IR RDR (.QUB, team-calibrated radiance, W cm-2 sr-1 um-1)
  -> ingest (thm2isis, or Rogers' thm_pre_process.dv with --ingest runbook)
  -> band_check (locate bands by BandBin FilterNumber)
  -> units gate (band-9 brightness temperature vs PDS index temperatures)
  -> spiceinit -> cam2map -> [crop]
  -> <id>.cub (radiance, no rescaling); pad_bands (missing filters as ISIS NULL)
  -> atmospheric correction: TES footprint query + per-footprint transmittance
     (Rogers/Bandfield themis_tes_overlay + themis_correct)
  -> regional atmosphere (default): emissivity E for every pixel, then
     surf = E / T(row), T = median footprint transmittance in a 3 deg latitude window
  -> QA -> georef sidecar -> cleanup
```

Every step is checked and nothing falls back silently. Each scene writes
`<id>.manifest.json` with its inputs, parameters, versions, step timings, QA and a status code.

## Repository layout

| Path | What it is |
|---|---|
| `pipeline/themis_pipeline.py` | The pipeline (v2). Python CLI, one scene per call. |
| `pipeline/regional_atm.py` | Regional-atmosphere step: `surf = E / T(row)`; can redo a scene offline from its stored `_tes.hdf` (`apply(..., redo=True)`). |
| `pipeline/themis_emissivity_full.dv` | Davinci: THEMIS emissivity E for every pixel, computed as inside `themis_correct`. |
| `pipeline/themis_atm_correct.dv` | Davinci wrapper around the TES overlay and `themis_correct`. |
| `atm_steps/` | A. D. Rogers' atmospheric-correction scripts and libraries (`themis_tes_overlay.dav`, `themis_correct.dav`, `TES_atmlib.hdf`, `PDART_mineral_library.hdf`) and the expert recipe (`themis_proc_with_full_atm_corr.txt`). |
| `pipeline/build_davinci_macos.sh` | Davinci 3.01 build for Apple Silicon (native arm64). |
| `pipeline/build_davinci_isis3_macos.sh`, `pipeline/iomod_isis3/` | Second, x86_64 Davinci with the ISIS3 I/O module ported to ISIS 9, needed only by the runbook ingest scripts. |
| `pipeline/build_davinci.sh` | Davinci build for Linux (the original 2026-05 setup). |
| `pipeline/themis_pipeline_legacy_edr.py` | v1, kept to reproduce old results only (see below). |
| `pipeline/themis_dcs.dv`, `dcs_python.py`, `extract_*spectra.py` | Decorrelation-stretch images and quick spectrum plots. |
| `CLAUDE.md` | Detailed working notes: version history, production notes, Davinci quirks. |
| `SETUP_GUIDE.md`, `USER_GUIDE.md` | The 2026-05 Linux install and v1 usage. Historical; v1 usage no longer applies. |

`work*/`, `test_data/` and Davinci/ISIS logs are gitignored.

## Requirements

- **ISIS 9** in a conda env named `isis` (osx-64 on the Mac, run under Rosetta). Its activation
  sets `ISISROOT`, `ISISDATA` and `PROJ_DATA`; run the pipeline from the activated env, because
  the TES polygon reprojection needs `PROJ_DATA`. `ISISDATA` needs the `base` and `odyssey`
  kernels, or set `THEMIS_SPICEINIT_WEB=1` to use the USGS SPICE web service.
- **Davinci 3.01** with the `dv_3_01` library, at `~/local/davinci` (override with
  `THEMIS_DAVINCI_ENV`). For `--ingest runbook` on the Mac, also the x86_64 build at
  `~/local/davinci-x86` (`THEMIS_DAVINCI_RUNBOOK_ENV`).
- Python packages in the `isis` env: `numpy`, `h5py`, `pandas` (for `--index-csv`).
- Network access to ASU's TES database (`app-davinci.mars.asu.edu`), queried for every scene,
  and, with `--ingest runbook`, to ASU's THEMIS archive for the download.

## Running it

One scene:

```bash
conda activate isis
python pipeline/themis_pipeline.py /path/to/I17888007RDR.QUB --workdir work/I17888007 \
    --index-csv ../massif-themis-analysis/catalog/rdr_scene_index.csv
```

With `--ingest runbook`, `src` can be a scene id and Rogers' scripts download and ingest it.
The exit code is 0 only when the scene's status is `ok`; a scene already `ok` at the current
version is skipped, so reruns are cheap.

Main options:

| Option | Default | Meaning |
|---|---|---|
| `--ingest {rdr,runbook}` | `rdr` | `thm2isis` on a local RDR, or Rogers' `thm_pre_process.dv` / `thm_post_process.dv` (production). |
| `--index-csv` | none | CSV with `scene, idx_T22, idx_T23` (PDS index temperatures) for the units gate. |
| `--gate-tol` | 15 K | Units-gate tolerance on band-9 brightness temperature. |
| `--atm-mode {regional,footprint}` | `regional` | Regional atmosphere, or `themis_correct`'s per-footprint result only. |
| `--atm-window-deg` | 3.0 | Latitude window for the regional transmittance. |
| `--atm-min-footprints` | 5 | Minimum footprints per window. |
| `--atm-minlat`, `--atm-maxlat`, `--atm-albedo` | -90, 90, 0.25 | TES footprint query limits. |
| `--crop MINLAT MAXLAT MINLON MAXLON` | none | Crop the projected cube before the correction. |
| `--keep-intermediates` | off | Keep `lev1`, `lev2`, `<id>.cub` and `_emiss.hdf`. |
| `--allow-uncalibrated-edr` | off | Accept EDR input. For reproducing v1 only. |

Many scenes: the massif project drives the pipeline with
`../massif-themis-analysis/scripts/run_pipeline_batch.py` (parallel workers, retries for failed
downloads, `_status.csv` summary).

## Outputs (per scene, after cleanup)

| File | Contents |
|---|---|
| `<id>_tes.hdf` | `surf` (regional-atmosphere surface emissivity, bands 1-9, 0 = invalid), `surf_footprint` (`themis_correct`'s per-footprint result), `emiss` (E before correction), `t_regional` (T per row), `atm` per footprint; root attributes record the atmosphere settings. |
| `tesover_<id>.hdf` | TES footprints overlapping the scene. |
| `<id>.georef.json` | Map grid of the projected cube, so pixels can be located after `lev2` is deleted. |
| `<id>.manifest.json` | Status, error, parameters, versions, per-step results and QA. |
| `<id>_atm.dcs.png` | Decorrelation stretch of the per-footprint corrected cube. |

Bands follow the THEMIS numbering: band N of `surf` is THEMIS band N. Band 10 (14.88 µm, CO2)
is not carried through.

## Status codes

`ok`, or the first failure: `bad_input`, `env`, `fetch_failed` (empty download; retry),
`partial_bands` (a WAC/CSFM filter 3, 4, 5, 7, 8 or 9 is missing), `failed_units_gate`,
`crop_empty`, `tes_query_failed` (ASU TES service or database fault; retry later),
`no_tes_footprints`, `atm_empty`, `shape_mismatch`, `command_failed`, `unexpected_error`.

If ASU's TES database is suspect, this should return a count, not an SQL error:

```bash
curl -G http://app-davinci.mars.asu.edu//Davinci/PSQLTESQueryTool --data-urlencode "queryString=select count(*) from tes_4_jmars where latitude between -30 and -29" --data-urlencode displayHeader=1
```

## v1 results are invalid

v1 (2026-05, `themis_pipeline_legacy_edr.py`) took THEMIS EDRs, which `thm2isis` does not
calibrate, and fed the raw 8-bit data numbers x 1e-5 to the atmospheric correction as if they
were radiance. v2 uses team-calibrated RDRs with no rescaling and checks the units before the
correction. Do not reintroduce a scale factor. The audit and validation are in
`../massif-themis-analysis/wiki/methods/themis-pipeline-validation.md`.

## Credits

The atmospheric correction is the Rogers/Bandfield TES-based method: the scripts and libraries in
`atm_steps/` are A. D. Rogers', and `thm_pre_process.dv` / `thm_post_process.dv` come with ASU's
Davinci library. This repository wraps them in a checked, batch-able pipeline and adds the
regional-atmosphere step.
