# Project Identity & Branding
- **Project Name:** **Huq** (`Huq-Player` on GitHub), powered by the **Anechoic Engine** (zero-echo real-time HRIR binaural DSP).
- **Naming Split (SEO vs. UI):**
  - Keep plain ASCII **`Huq`** in search-indexed locations (`README.md` H1, repo name, GitHub Release titles `Huq vX.Y.Z`, PyInstaller `--name "Huq"`, and archive filenames `Huq-Windows.zip` / `Huq-macOS-AppleSilicon.dmg`).
  - Use stylized **`ɦuk — Anechoic Engine`** inside the desktop app UI (`player_ui/main.py` window title).

# Project Architecture
- `hrtf_core/`: Cross-platform Rust DSP engine (`cdylib`). Build with `cargo build --release`. Keep audio callbacks zero-allocation and real-time safe.
- `player_ui/`: Python PySide6 UI + lazy `pyfar` SOFA loader + `pyqtgraph` spectrum visualizer. Calls `hrtf_core` via `dsp_bridge.py`.
- Never rewrite the UI or SOFA parser in Rust. Modify one module at a time.

# Key Implementation Conventions (Do Not Regress)
- **Fast Startup & Lazy Imports:** Keep heavy modules (`pyfar`, `scipy.signal.resample_poly`, `mutagen`, `soundfile`) lazy-loaded and defer `load_default_sofa()` / `load_session_state()` via `QTimer.singleShot(10, self._deferred_startup)` so the window opens immediately.
- **CPU Quality Modes (`low_end_mode`):**
  - Default is `CPU: Full Quality` (lossless `scipy.signal.resample_poly` HRIR resampling, 3x3s 8192-FFT `Tonal Match`, 60 FPS spectrum).
  - `CPU: Low-End` reduces `Tonal Match` analysis windows, uses fast linear HRIR resampling, and drops spectrum FPS to 10 FPS while keeping real-time Rust HRTF DSP active.
  - `_check_cpu_benchmark()` runs once on first boot using warmup + `np.median` across 16 Rust DSP blocks to avoid OS page-fault false alarms.
- **Windows Non-ASCII / Cyrillic Path Safety:** `netCDF4` / `h5py` inside `pyfar.io.read_sofa()` fails on non-ASCII Windows paths; `load_sofa()` copies non-ASCII paths to an ASCII temp directory before reading and never persists the bundled `default.sofa` path into `session.json`.
- **Spectrum Analyzer (`update_graph`):**
  - `Time Decay` uses calibrated millisecond time constants (`Instant=0ms`, `Fast=20ms`, `Medium=42ms`, `Slow=70ms`).
  - `Input` and `Processed Output` use a sliding sub-block cursor (`h_audio`) from atomic `viz_state` for true 10/30/60/120 FPS motion.
  - `HRIR Only (Raw)` is computed directly in the frequency domain from `P_in * (|H_LL_raw|^2 + |H_RL_raw|^2)` (and equals `P_in_db` when bypassed) so it stays phase-locked with zero ring-buffer seam jumps.

# Git & Release Workflow
- Pushing tags matching `v*` (currently `v0.3.5`) triggers `.github/workflows/build-release.yml` to build Windows (`Huq-Windows.zip`) and macOS Apple Silicon (`Huq-macOS-AppleSilicon.dmg`) releases titled `Huq vX.Y.Z`.
- When sharing terminal patches for `player_ui/main.py`, keep scripts compact (under ~60 lines) or split into two parts so they fit cleanly in terminal paste buffers.

# Planned Roadmap
- Refactor into a modular single-repo architecture: `anechoic_core/` (Rust DSP), `anechoic_engine/` (shared Python audio/SOFA bridge), and `editions/` (hot-swappable UI themes + DSP feature profiles, starting with a midnight-black and crimson-ruby theme for `ɦuk`).

# Recent Additions & Architecture Updates (v0.3.8+)
- **Platform Focus:**
  - Active release targets are **Windows (`Huq-Windows.zip`)** and **macOS Apple Silicon (`Huq-macOS-AppleSilicon.dmg`)**. Android (`android_app/` + `android_core/`) is paused/experimental until desktop feature parity is complete.
- **Driver-Free macOS System Audio (`macos_tap/huq_tap.m` -> `libhuq_tap.dylib`):**
  - Never install experimental HAL plugins in `/Library/Audio/Plug-Ins/HAL/`; use the pure user-space CoreAudio Process Tap (`CATapDescription` with `CATapMuted` and `stereoGlobalTapButExcludeProcesses` excluding `Huq`'s own PID).
  - Controlled via the single **`Go Live`** button (`toggle_go_live`): locks `engine.fs` to the tap's native sample rate, defaults volume to `80%` (restoring `44%` local volume on exit), disables `Prev/Next/Seek/Export`, and runs `_update_live_tonal_match` every `1.5s` with a `0.25 dB` stability deadband.
  - `.github/workflows/build-release.yml` compiles `libhuq_tap.dylib`, bundles it via PyInstaller `--add-binary`, and injects `NSAudioCaptureUsageDescription` into `Info.plist`.
- **Limiter & Bit-Perfect Safeguards (`player_ui/main.py`):**
  - Rust receives `hard_clip=False` so Python sees true unclipped post-HRTF peaks; when `Hard Clip (Exact)` (`not float_mode`) is active, `auto_trim_gain` enforces a `-0.5 dBFS` (`0.94406`) ceiling (`1.015`/block release, instant reset when lowering volume) and displays live attenuation as `Peak: -X.X dB (LIM -Y.Y dB)`.
  - `Bit-Perfect` uses `RightClickPushButton`: **Left-Click** runs an exact `2.0s` (`80 x 25ms`) RME-style volume ramp to `100%` before locking `bit_perfect=True`; **Right-Click** jumps instantly to `100%`. Temporary `100%` Bit-Perfect / `80%` Live volumes are never persisted to `session.json`.
