# Project Identity & Branding
- **Project Name:** **REXA** (`REXA-Player` on GitHub), powered by the **Anechoic Engine** (zero-echo real-time HRIR binaural DSP).
- **Naming Split (SEO vs. UI):**
  - Keep plain ASCII **`REXA`** in search-indexed locations (`README.md` H1, repo name, GitHub Release titles `REXA vX.Y.Z`, PyInstaller `--name "REXA"`, and archive filenames `REXA-Windows.zip` / `REXA-macOS-AppleSilicon.dmg`).
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
- **NEVER push to GitHub unless the user explicitly says it can be pushed** (e.g. “you can push now”, “push it”, “it can be pushed”). Local commits are allowed; `git push`, pushing tags, and opening PRs require explicit approval every time. If unsure, do not push.
- Pushing tags matching `v*` (currently `v0.3.5`) triggers `.github/workflows/build-release.yml` to build Windows (`REXA-Windows.zip`) and macOS Apple Silicon (`REXA-macOS-AppleSilicon.dmg`) releases titled `REXA vX.Y.Z`.
- When sharing terminal patches for `player_ui/main.py`, keep scripts compact (under ~60 lines) or split into two parts so they fit cleanly in terminal paste buffers.
- See `docs/LIQUID_GLASS_HANDOFF.md` for the current liquid-glass / `Glass macOS 27` work state and exact local-only commit commands.

# Planned Roadmap
- Refactor into a modular single-repo architecture: `anechoic_core/` (Rust DSP), `anechoic_engine/` (shared Python audio/SOFA bridge), and `editions/` (hot-swappable UI themes + DSP feature profiles, starting with a midnight-black and crimson-ruby theme for `ɦuk`).

# Recent Additions & Architecture Updates (v0.3.8+)
- **Platform Focus:**
  - Active release targets are **Windows (`REXA-Windows.zip`)** and **macOS Apple Silicon (`REXA-macOS-AppleSilicon.dmg`)**. Android (`android_app/` + `android_core/`) is paused/experimental until desktop feature parity is complete.
- **Driver-Free macOS System Audio (`macos_tap/huq_tap.m` -> `libhuq_tap.dylib`):**
  - Never install experimental HAL plugins in `/Library/Audio/Plug-Ins/HAL/`; use the pure user-space CoreAudio Process Tap (`CATapDescription` with `CATapMuted` and `stereoGlobalTapButExcludeProcesses` excluding `REXA`'s own PID).
  - Controlled via the single **`Go Live`** button (`toggle_go_live`): locks `engine.fs` to the tap's native sample rate, defaults volume to `80%` (restoring `44%` local volume on exit), disables `Prev/Next/Seek/Export`, and runs `_update_live_tonal_match` every `1.5s` with a `0.25 dB` stability deadband.
  - `.github/workflows/build-release.yml` compiles `libhuq_tap.dylib`, bundles it via PyInstaller `--add-binary`, and injects `NSAudioCaptureUsageDescription` into `Info.plist`.
- **Limiter & Bit-Perfect Safeguards (`player_ui/main.py`):**
  - Rust receives `hard_clip=False` so Python sees true unclipped post-HRTF peaks; when `Hard Clip (Exact)` (`not float_mode`) is active, `auto_trim_gain` enforces a `-0.5 dBFS` (`0.94406`) ceiling (`1.015`/block release, instant reset when lowering volume) and displays live attenuation as `Peak: -X.X dB (LIM -Y.Y dB)`.
  - `Bit-Perfect` uses `RightClickPushButton`: **Left-Click** runs an exact `2.0s` (`80 x 25ms`) RME-style volume ramp to `100%` before locking `bit_perfect=True`; **Right-Click** jumps instantly to `100%`. Temporary `100%` Bit-Perfect / `80%` Live volumes are never persisted to `session.json`.

## UI & Styling Architecture Rules (`player_ui/`)

1. **Never Toggle `WA_TranslucentBackground` at Runtime:**
   * On macOS PySide6, flipping `Qt.WA_TranslucentBackground` after `window.show()` recreates the native backing store and causes black/gray paint glitches.
   * Keep `WA_TranslucentBackground` enabled once in `__init__` (guarded by `windows_transparency_enabled()` on Windows). `Classic` and `Light` paint `100%` opaque gradients (`alpha=255`) over `centralWidget` and `settingsRoot`, while `Glass` paints translucent tints over the native blur view.
2. **Avoid Mismatched `border-top`/`border-bottom` Colors with `border-radius`:**
   * Always use uniform `border: 1px solid ...` rules in `build_liquid_qss()` so Qt does not draw 45-degree corner miter seams.
3. **Valid QSS Sub-Control Pseudo-States:**
   * Always place `:disabled` **after** sub-controls (`QSlider::sub-page:horizontal:disabled`, never `QSlider:disabled::sub-page:horizontal`), otherwise Qt's CSS parser drops the slider groove height rule.
4. **Stock Theme Defaults & Persistence (`session.json`):**
   * `theme_glow`: `{"classic": 0.20, "light": 0.30, "liquid_glass": 1.00}`
   * `glass_material`: `automatic` | `glass_clarity`: `45` (legacy/automatic fallback) | `glass_rim`: `20`
   * Keep `SettingsDialog` synced with `MainWindow` via `sync_from_main()` and `_on_ambient_color_changed()`.
5. **Layout & Text Overflow Protection:**
   * Use `MarqueeLabel` (with `QSizePolicy.Ignored`) for dynamic track metadata so long titles never compress `lbl_cover` or push right-side controls.
   * Keep `combo_group` at `min-width >= 116px` (`view().setMinimumWidth(126)`) and `btn_sort_dir` at `50px` with `padding: 6px 4px;` so `"Custom"` and `"A-Z"`/`"Z-A"` never elide or clip.
 6. **Native macOS Glass Style:** `pyqt_liquidglass` defaults `NSGlassEffectView` to `Regular`. Always call `configure_macos_glass()` after `apply_glass_to_window()` and whenever the Glass Material selector changes. `macos27_glass` uses `DarkAqua` + a 14pt corner radius + transparent root — `VibrantDark` reads too bright/blue next to native Settings; original `Glass` keeps `Regular (0)` + `VibrantDark`. **Never lower `NSGlassEffectView.alphaValue`** — AppKit then hides the material and the window looks flat; keep it at `1.0`. Use the discrete `Glass Material` selector (`Automatic / Default / Dark / Clear / Tinted`) instead of a fake blur slider: `Clear`/`Tinted` map to native `Clear (1)`, `Default`/`Dark` to `Regular (0)`, with subtle `tintColor` and QSS veil per mode. `Automatic` follows the last system sync via legacy `glass_clarity`. macOS has no public blur-radius API or readable exact native mode; do not claim otherwise. Keep the ambient/lava overlay static and very faint in `macos27_glass` (peak alpha `0.015 * glow`, no dither tile) so it does not muddy the native blur. Do not make it white/light — references are dark macOS panels over a picture background. Accent stays muted; neutral at Ambient Glow 0.
 7. **Cover Lens Pill:** The WebGPU-style `GlassLensOverlay` is only for the original `Glass` theme and only when a real cover pixmap exists. Never render its accent-gradient fallback, and keep it hidden in `macos27_glass` to match native macOS Settings.
 8. **System Glass Sync:** Double-clicking `Glass Material:` in `SettingsDialog` calls `read_system_glass_preferences()`, selects `Automatic`, and maps public OS signals to a best-effort mode plus legacy `glass_clarity`. macOS: effective appearance, `AppleInterfaceStyleSwitchesAutomatically`, `NSWorkspace.accessibilityDisplayShouldReduceTransparency()`, and main-wallpaper luminance; Windows: `AppsUseLightTheme` + `EnableTransparency`. Do **not** claim exact macOS `Default/Dark/Clear/Tinted` detection — those values are not public. Android is paused/experimental and has no system sync yet.

## v0.5.2 Architecture & Build Notes
- **Pre-baked HRTF Profile (`player_ui/default_sofa.npz`):** Bundled into PyInstaller releases so first startup skips importing `pyfar`/`sofar`/`netCDF4`/`h5py` (preventing Windows HDF5 thread deadlocks and enabling <0.5s cold starts).
- **Last.fm App Keys (`player_ui/lfm_keys.py`):** Gitignored locally; injected at CI build time from `LFM_API_KEY` and `LFM_API_SECRET` GitHub Actions repository secrets.
- **In-App Updater (`UpdateCheckThread` / `UpdateInstallThread`):** Queries `://github.com`, strips `com.apple.quarantine` on macOS `.app` updates, and uses a detached `.bat` swapper on Windows.
- **Library Delegates & Dialogs:** `MarqueeDelegate` renders non-selectable `56 px` album cover headers (`_is_header`) and async per-track cover strips via `ThreadPoolExecutor`; `SettingsDialog` and `_show_track_properties` are non-modal so `Cmd+Q` / window close never blocks.
