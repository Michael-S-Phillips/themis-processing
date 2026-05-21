# THEMIS Processing — User Guide

This is the day-to-day reference for running the THEMIS IR processing pipeline at `/mnt/themis-processing/pipeline/`. For how the environment was built, see `SETUP_GUIDE.md`.

The pipeline ingests a THEMIS IR EDR (`.QUB`), projects it, and produces both a raw decorrelation-stretched color image and (optionally) an atmospherically-corrected emissivity cube using Dr Rogers' algorithm.

## At a glance

```bash
# Activate the ISIS conda env (one time per shell)
source ~/miniconda3/etc/profile.d/conda.sh
conda activate isis

# Basic pipeline (ingest → project → DCS)
python /mnt/themis-processing/pipeline/themis_pipeline.py \
    /mnt/themis-processing/test_data/I01269007EDR.QUB \
    --workdir /mnt/themis-processing/work_full

# Full pipeline including atmospheric correction (slow — ~3 hours)
python /mnt/themis-processing/pipeline/themis_pipeline.py \
    <path/to/IxxxxxxEDR.QUB> \
    --workdir <output dir> \
    --with-atm-correction --atm-minlat=-12 --atm-maxlat=-1
```

## Prerequisites

- Conda env `isis` activated (gives you `thm2isis`, `spiceinit`, `cam2map`, `isis2std`, `algebra`, plus `rasterio`/`pillow` for the Python DCS fallback).
- For the Davinci-based steps: `~/local/davinci/etc/davinci.env` exists. The pipeline auto-sources it when it spawns `davinci`.
- For atmospheric correction: an internet connection to `app-davinci.mars.asu.edu` (public TES catalog endpoint) and the four reference files under `/mnt/themis-processing/atm_steps/`.

## CLI reference

`python pipeline/themis_pipeline.py <edr.QUB> [options]`

| Flag | Default | Description |
| --- | --- | --- |
| `<edr.QUB>` | — | Required. Path to a THEMIS IR EDR file. |
| `--workdir DIR` | `./work` | Output directory. Created if missing. The pipeline is idempotent: rerunning skips steps whose outputs already exist. |
| `--skip-davinci` | off | Skip the Davinci DCS color image. You still get the ISIS lev1, lev2, and grayscale band-9 preview. |
| `--with-atm-correction` | off | Run the full Rogers atmospheric correction. **Adds ~2.5 hours** of compute. |
| `--atm-minlat F` | -90 | TES catalog query filter, southern bound (degrees). Use the image's actual lat extent for fastest queries. |
| `--atm-maxlat F` | +90 | TES catalog query filter, northern bound. **Stay strictly inside the image's latitude range** (e.g. for an image spanning lat=-12.3..-0.18 use `--atm-maxlat=-1`, not 0). See "Why the maxlat margin" below. |
| `--atm-albedo F` | 0.25 | TES catalog filter — maximum Lambert albedo. Lower values pick darker (less dusty) footprints. |
| `--verbose / -v` | off | Debug-level logging. |

## Outputs

For an input `IxxxxxxEDR.QUB`, the workdir gets:

| File | Stage | Approx size | What it is |
| --- | --- | --- | --- |
| `Ixxxxxx.lev1.cub` | `thm2isis` | ~90 MB | Raw ingested cube, native instrument geometry. |
| `Ixxxxxx.lev2.cub` | `cam2map` (after `spiceinit`) | ~400 MB | Sinusoidal-projected cube, 10 bands of raw radiance. |
| `Ixxxxxx.lev2.pgw` | sidecar | 121 B | ESRI world-file with the projection geotransform. |
| `Ixxxxxx.lev2.png` | `isis2std` | ~10 MB | Grayscale band-9 quicklook. Open in any image viewer. |
| `Ixxxxxx.dcs.png` | Davinci `dcs` on bands 8/7/5 | ~6 MB | Color DCS image of raw radiance. **Note**: pixels outside the strip render uniform blue (DCS rotation of zero) — only the strip itself is meaningful. |
| `Ixxxxxx.dcs.tif` | same | ~13 MB | GeoTIFF version of the DCS, openable in QGIS/ENVI. |

If `--with-atm-correction` is on, additionally:

| File | Stage | Approx size | What it is |
| --- | --- | --- | --- |
| `Ixxxxxx.cub` | `algebra A=1e-5` | ~400 MB | Scale-divided lev2 cube, intermediate fed to `themis_correct`. The /1e5 brings radiance into W m⁻² sr⁻¹ µm⁻¹ to match the brightness-temperature lookup table. |
| `tesover_Ixxxxxx.hdf` | `themis_tes_overlay` | ~2 MB | TES footprints overlapping the image with their 73-channel emissivity spectra. Saved so reruns can skip the catalog query. |
| `Ixxxxxx_tes.hdf` | `themis_correct` | ~30 MB | The corrected output struct. `surf` (9×H×W float) is per-pixel surface emissivity. `atm` (9×1×N) is the per-footprint atmospheric transmittance. `tes_surf` and `xaxis` are the TES surface spectra and band-center wavenumbers. |
| `Ixxxxxx_atm.dcs.png` | Davinci `dcs` on `cor.surf` | ~3 MB | DCS image of the corrected cube. Background is properly black; only TES-covered pixels (~10–15% of the strip) carry color. |

## Common workflows

**Just want a preview of a THEMIS image:**

```bash
python pipeline/themis_pipeline.py <edr.QUB> --workdir work_quick --skip-davinci
```

~3 minutes. Open `work_quick/*.lev2.png` for a grayscale quicklook of band 9.

**Color DCS image without atm correction:**

```bash
python pipeline/themis_pipeline.py <edr.QUB> --workdir work_dcs
```

~5 minutes. The `*.dcs.png` and `*.dcs.tif` are produced after the ISIS steps.

**Full corrected cube (overnight job):**

```bash
python pipeline/themis_pipeline.py <edr.QUB> --workdir work_full \
    --with-atm-correction --atm-minlat=<image_minlat+0.5> --atm-maxlat=<image_maxlat-0.5>
```

Roughly 3 hours total. The wall-clock is dominated by the per-footprint loop in `themis_correct`.

**Pick the right `--atm-minlat`/`--atm-maxlat`:** the image's lat extent shows up in the `cam2map` step's printout (`MinimumLatitude` / `MaximumLatitude`). After a basic run, you can grep `work_dcs/print.prt` or open `work_dcs/*.lev2.cub`'s label with `catlab from=*.lev2.cub | grep -E 'Min.*Lat|Max.*Lat'`.

**Compare raw radiance vs corrected emissivity at a few pixels:**

```bash
python pipeline/extract_corrected_spectra.py \
    --tes-hdf work_full/Ixxxxxx_tes.hdf \
    --lev2 work_full/Ixxxxxx.lev2.cub \
    --out work_full/spectra_comparison.png -n 4
```

Produces a side-by-side panel: raw radiance (dominated by the atmospheric/thermal bowl around 9–10 µm) vs corrected emissivity (near 1.0 with subtle silicate features).

**Raw spectra only (no atm correction needed):**

```bash
python pipeline/extract_spectra.py work_full/Ixxxxxx.lev2.cub \
    --out work_full/raw_spectra.png -n 6
```

Picks 6 well-separated valid pixels from top to bottom of the strip and plots their 10-band THEMIS IR radiance spectra alongside the band-9 grayscale.

## Why the `--atm-maxlat` margin matters

`themis_correct.dav` slices the input cube to a latitude band defined by `tes.minlat` / `tes.maxlat` via `proj_geocoord`. If either bound is **outside the image's actual latitude extent**, `proj_geocoord` returns a pixel index that is out of range (e.g. negative when `maxlat=0` but the image's true `MaximumLatitude=-0.18`), the slice degenerates to empty, and the entire correction silently produces a zero output cube.

The pipeline already strips these bounds from the tes struct before invoking `themis_correct`, so today this is mostly a "no-op safeguard" — but if you ever bypass the wrapper and call `themis_correct` directly, **always pass minlat / maxlat strictly inside the image's actual lat range**.

## Troubleshooting

**Q: The DCS image (`*.dcs.png`) has a uniform blue background.**
A: That's the DCS rotation of (0, 0, 0) — pixels outside the THEMIS strip have all bands = ISIS NULL and after our pre-masking they become zero, which DCS then rotates to a single color. Only the strip itself carries real information. The atm-corrected variant (`*_atm.dcs.png`) doesn't have this issue because only TES-covered pixels are populated.

**Q: After `--with-atm-correction`, `Ixxxxxx_tes.hdf` is only ~1 MB and the corrected DCS is uniform gray.**
A: One of three things broke. Diagnose in this order:
1. Check `stats from=work_full/Ixxxxxx.cub | grep -E 'Min|Max'`. Values should be in the range ~0.001–0.002 (W m⁻² sr⁻¹ µm⁻¹). If you see 100–200, the `algebra A=1e-5` scaling step didn't run.
2. Check the `--atm-maxlat` you passed is strictly less than the image's `MaximumLatitude` (and `--atm-minlat` strictly greater than `MinimumLatitude`).
3. Confirm `echo $PROJ_DATA` after activating the env is set to `~/miniconda3/envs/isis/share/proj` — `shape2georaster` silently produces all-zero masks without it.

**Q: spiceinit fails with `No existing files found with a numerical version matching [kernels.????.db]`.**
A: The SPICE kernels under `~/isis_data/` aren't downloaded yet (or were partially downloaded). Re-run:
```bash
downloadIsisData base $ISISDATA
downloadIsisData odyssey $ISISDATA
```
Together ~28 GB; takes about 5 minutes on a fast connection.

**Q: `davinci: command not found`.**
A: Source the env file: `source ~/local/davinci/etc/davinci.env`. The Python wrapper does this automatically when spawning davinci, but you need it manually if you're calling davinci directly from your shell.

**Q: I want to bypass Davinci entirely.**
A: Pass `--skip-davinci`. You'll get lev1, lev2, and a band-9 PNG preview from ISIS only — no DCS color image and no atm correction. `pipeline/dcs_python.py` is a pure-numpy DCS fallback (rasterio + numpy + Pillow); call it directly on the lev2 cube if you want a color image without Davinci.

## Performance notes

On the current Linux box (24 cores, 15 GB RAM, SMB share for I/O):

| Stage | Wall-clock |
| --- | --- |
| `thm2isis` | ~3 s |
| `spiceinit` | ~8 s |
| `cam2map` | ~2.5 min (dominates the basic pipeline) |
| `isis2std` (preview) | ~1 s |
| `algebra` (1e-5 scale) | ~30 s |
| Davinci DCS | ~30 s |
| TES catalog query | ~1 min (one HTTPS round-trip + parse) |
| `themis_correct` loop | ~2.5 hr (1500–2500 iterations × ~3–5 s) |

The correction loop dominates heavily. If you only need a quick look, use `--skip-davinci` or run without `--with-atm-correction`.

The pipeline keeps intermediates on the SMB share at `/mnt/themis-processing/work_full`. Reads/writes go to Windows over SMB and are 5–10× slower than local disk; for production batch runs, mount a local scratch directory for `--workdir` and only copy the final outputs back.
