# themis-processing

This pipeline converts a Mars Odyssey Thermal Emission Imaging System (THEMIS) infrared (IR;
6.8–14.9 µm) observation into a map-projected surface-emissivity cube, corrected for the
atmosphere with Thermal Emission Spectrometer (TES) data. It uses the Integrated Software for
Imagers and Spectrometers (ISIS 9) and Davinci. We built it for the THEMIS analysis of the Argyre
and Hellas massifs (`../massif-themis-analysis/`), and it accepts any THEMIS IR scene.

Current version: **2.1.1** (`PIPELINE_VERSION` in `pipeline/themis_pipeline.py`).

## Processing steps

```
THEMIS IR reduced data record (RDR; .QUB, team-calibrated radiance, W cm-2 sr-1 um-1)
  -> ingest (thm2isis, or Rogers' thm_pre_process.dv with --ingest runbook)
  -> band_check (locate bands by BandBin FilterNumber)
  -> units gate (band-9 brightness temperature vs Planetary Data System index temperatures)
  -> spiceinit -> cam2map -> [crop]
  -> <id>.cub (radiance, no rescaling); pad_bands (missing filters as ISIS NULL)
  -> atmospheric correction: TES footprint query + per-footprint transmittance
     (Rogers/Bandfield themis_tes_overlay + themis_correct)
  -> regional atmosphere (default): emissivity E for every pixel, then
     surf = E / T(row), T = median footprint transmittance in a 3° latitude window
  -> QA -> georef sidecar -> cleanup
```

The pipeline checks the output of each step. If a check fails, it stops processing that scene and
records the failure as a status code. For each scene it writes `<id>.manifest.json` with the
inputs, parameters, software versions, step timings, QA results, and status.

## Repository layout

| Path | Contents |
|---|---|
| `pipeline/themis_pipeline.py` | The pipeline (v2): a Python command-line tool that processes one scene per call. |
| `pipeline/regional_atm.py` | Regional-atmosphere step, `surf = E / T(row)`. `apply(..., redo=True)` recomputes a scene from its stored `_tes.hdf` without rerunning Davinci. |
| `pipeline/themis_emissivity_full.dv` | Davinci script that computes the THEMIS emissivity E for every pixel, with the same formula `themis_correct` uses. |
| `pipeline/themis_atm_correct.dv` | Davinci wrapper around the TES overlay and `themis_correct`. |
| `atm_steps/` | A. D. Rogers' atmospheric-correction scripts and libraries (`themis_tes_overlay.dav`, `themis_correct.dav`, `TES_atmlib.hdf`, `PDART_mineral_library.hdf`) and the processing recipe (`themis_proc_with_full_atm_corr.txt`). |
| `pipeline/build_davinci_macos.sh` | Build of Davinci 3.01 for Apple Silicon (native arm64). |
| `pipeline/build_davinci_isis3_macos.sh`, `pipeline/iomod_isis3/` | Build of a second, x86_64 Davinci with its ISIS3 I/O module ported to ISIS 9. Only the runbook ingest scripts use it. |
| `pipeline/build_davinci.sh` | Build of Davinci for Linux (May 2026 setup). |
| `pipeline/themis_pipeline_legacy_edr.py` | v1, kept only to reproduce v1 results (see "v1 (EDR input)"). |
| `pipeline/themis_dcs.dv`, `dcs_python.py`, `extract_*spectra.py` | Decorrelation-stretch (DCS) images and quick spectrum plots. |
| `CLAUDE.md` | Working notes: version history, production notes, Davinci quirks. |
| `SETUP_GUIDE.md`, `USER_GUIDE.md` | The May 2026 Linux installation and v1 usage. The v1 usage no longer applies. |

Git ignores `work*/`, `test_data/`, and the Davinci and ISIS logs.

## Requirements

- **ISIS 9** in a conda environment named `isis` (osx-64 on the Mac, under Rosetta). Activating
  it sets `ISISROOT`, `ISISDATA`, and `PROJ_DATA`. Run the pipeline from the activated
  environment: reprojecting the TES footprint polygons fails without `PROJ_DATA`. `ISISDATA` needs
  the `base` and `odyssey` kernels; alternatively, `THEMIS_SPICEINIT_WEB=1` makes `spiceinit` use
  the USGS SPICE web service.
- **Davinci 3.01** with the `dv_3_01` library, at `~/local/davinci` (set `THEMIS_DAVINCI_ENV` to
  change). With `--ingest runbook` on the Mac, also the x86_64 build at `~/local/davinci-x86`
  (`THEMIS_DAVINCI_RUNBOOK_ENV`).
- **Python packages** in the `isis` environment: `numpy`, `h5py`, and `pandas` (for `--index-csv`).
- **Network access** to ASU's TES database (`app-davinci.mars.asu.edu`), which the pipeline
  queries for every scene. With `--ingest runbook`, `thm_pre_process.dv` also downloads the
  scene from ASU's THEMIS archive.

## Usage

One scene:

```bash
conda activate isis
python pipeline/themis_pipeline.py /path/to/I17888007RDR.QUB --workdir work/I17888007 \
    --index-csv ../massif-themis-analysis/catalog/rdr_scene_index.csv
```

With `--ingest runbook`, the input can be a scene ID in place of a file. The exit code is 0 only
when the scene's status is `ok`. The pipeline skips a scene whose manifest is already `ok` at the
current version, so a rerun processes only the scenes that failed or never ran.

Main options:

| Option | Default | Meaning |
|---|---|---|
| `--ingest {rdr,runbook}` | `rdr` | `thm2isis` on a local RDR, or Rogers' `thm_pre_process.dv` / `thm_post_process.dv` (used in production). |
| `--index-csv` | none | CSV with `scene`, `idx_T22`, `idx_T23` (index temperatures) for the units gate. |
| `--gate-tol` | 15 K | Units-gate tolerance on the band-9 brightness temperature. |
| `--atm-mode {regional,footprint}` | `regional` | Regional atmosphere, or only `themis_correct`'s per-footprint result. |
| `--atm-window-deg` | 3.0 | Latitude window for the regional transmittance. |
| `--atm-min-footprints` | 5 | Minimum number of footprints per window. |
| `--atm-minlat`, `--atm-maxlat`, `--atm-albedo` | −90, 90, 0.25 | Limits on the TES footprint query. |
| `--crop MINLAT MAXLAT MINLON MAXLON` | none | Crop the projected cube before the correction. |
| `--keep-intermediates` | off | Keep `lev1`, `lev2`, `<id>.cub`, and `_emiss.hdf`. |
| `--allow-uncalibrated-edr` | off | Accept experiment data record (EDR) input; only for reproducing v1. |

For many scenes, `../massif-themis-analysis/scripts/run_pipeline_batch.py` runs the pipeline in
parallel, retries failed downloads, and summarizes the manifests in `_status.csv`.

## Outputs

Files kept for each scene after cleanup:

| File | Contents |
|---|---|
| `<id>_tes.hdf` | `surf` (regional-atmosphere surface emissivity, bands 1–9, 0 = invalid), `surf_footprint` (`themis_correct`'s per-footprint result), `emiss` (E before correction), `t_regional` (T per row), and `atm` (per footprint). The atmosphere settings are stored as root attributes. |
| `tesover_<id>.hdf` | TES footprints that overlap the scene. |
| `<id>.georef.json` | Map grid of the projected cube, for locating pixels after `lev2` is deleted. |
| `<id>.manifest.json` | Status, error message, parameters, versions, per-step results, and QA. |
| `<id>_atm.dcs.png` | DCS image of the per-footprint corrected cube. |

Band N of `surf` is THEMIS band N. Band 10 (14.88 µm) falls in the atmospheric CO₂ absorption and
is not included.

## Status codes

The manifest's `status` is `ok`, or the code of the first failure:

| Code | Meaning |
|---|---|
| `bad_input` | The input is not a THEMIS IR product, its label lacks filter numbers, or it is an EDR without `--allow-uncalibrated-edr`. |
| `env` | A required program, script, or library is missing. |
| `fetch_failed` | The download from ASU was empty; retry. |
| `partial_bands` | The observation lacks a filter used for the weighted absorption center (WAC) and cubic-spline-fit minimum (CSFM): 3, 4, 5, 7, 8, or 9. |
| `failed_units_gate` | The band-9 brightness temperature is outside the tolerance, the values are integers (raw counts), or band 9 has too few valid pixels. |
| `crop_empty` | The crop box does not overlap the scene. |
| `tes_query_failed` | ASU's TES service is unreachable or its database returned an error; retry later. |
| `no_tes_footprints` | No TES footprint that overlaps the scene passes the overlay filters. |
| `atm_empty` | The atmospheric correction produced no output or no valid pixels. |
| `shape_mismatch` | The corrected cube and the projected cube differ in size. |
| `command_failed` | An ISIS or Davinci command exited with an error or wrote no output. |
| `unexpected_error` | Any other exception; the manifest contains its message. |

To check ASU's TES database, run the query below. A working database returns a row count; a
faulty one returns a line that begins `SQL Error`.

```bash
curl -G http://app-davinci.mars.asu.edu//Davinci/PSQLTESQueryTool --data-urlencode "queryString=select count(*) from tes_4_jmars where latitude between -30 and -29" --data-urlencode displayHeader=1
```

## v1 (EDR input)

v1 (May 2026, `themis_pipeline_legacy_edr.py`) read THEMIS EDRs, which `thm2isis` does not
calibrate, and passed the raw 8-bit counts multiplied by 10⁻⁵ to the atmospheric correction as
radiance. Its emissivities are therefore invalid. v2 reads team-calibrated RDRs, applies no
rescaling, and checks the units before the correction. Do not reintroduce a scale factor. The
audit and validation are in `../massif-themis-analysis/wiki/methods/themis-pipeline-validation.md`.

## Credits

The atmospheric correction is the Rogers/Bandfield TES-based method. The scripts and libraries in
`atm_steps/` are A. D. Rogers'; `thm_pre_process.dv` and `thm_post_process.dv` are distributed
with ASU's Davinci library. We added the step checks, the manifests, batch processing, and the
regional-atmosphere step.
