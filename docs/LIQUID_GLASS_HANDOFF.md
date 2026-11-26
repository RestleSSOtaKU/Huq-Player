# Liquid Glass / macOS 27 UI Work — Handoff

> **PUSH POLICY: DO NOT PUSH TO GITHUB.**
> Push only when the user explicitly says something like: “you can push now”, “push it”, or “it can be pushed”.
> Local commits are allowed and encouraged so progress survives model switches. Never create/push a `v*` tag without explicit approval too.

## Goal
- Make REXA’s `Glass macOS 27` theme look like clean native macOS glass panels (visible desktop diffusion, thin rim, neutral tint) instead of a fake dimming layer.
- Keep the theme applied through the whole app: main window, settings, dialogs, cards, spectrum, and cover lens.
- Keep the settings simple: `Dark` / `Light` are the only appearance choices, with separate best-effort tuning for each stage.
- Give Ambient Glow a visibly stronger ceiling in both glass themes.

## Current state
- Branch: `main`; all work is local only. No push, tag, or PR has been made.
- Local build artifacts must **not** be committed: `REXA-Windows.zip`, `REXA-Windows/`, `macos_tap/libhuq_tap.dylib`.
- The latest stage-only and Ambient Glow changes are included in the final local commit for this handoff.
- Final visual confirmation from the user is still pending after restarting with `python -B player_ui/main.py`.

## Completed

### Theme and lens foundation
- Added `Glass macOS 27` as a fourth theme (`macos27_glass`, label `Glass macOS 27`), with cycle order `Classic → Light → Glass → Glass macOS 27` and persistence in `session.json`.
- Added `player_ui/glass_lens.py`: a static cached WebGPU-style lens pill over album art. It appears only in the original `Glass` theme and only with a real cover pixmap; the accent-gradient fallback was removed. It is hidden in `macos27_glass` to match native macOS Settings.
- Added `player_ui/LICENSES/liquid-glass-webgpu.LICENSE` and bundled it into Windows/macOS release artifacts in `.github/workflows/build-release.yml`.

### Native glass baseline
- `configure_macos_glass()` in `player_ui/liquid_glass.py` is called whenever native glass is applied: main window, `SettingsDialog`, track-properties dialog, startup, theme changes, and stage changes.
- `macos27_glass` uses `DarkAqua` by default, a `14pt` corner radius, transparent root, and the public `Clear` style. Original `Glass` keeps `VibrantDark` + public `Regular (0)`. `VibrantDark` read too bright/blue next to native Settings for `macos27_glass`.
- `NSGlassEffectView.alphaValue` always stays `1.0`; lowering it hides the material and makes the window flat. Density comes from native `tintColor` plus QSS scrim.
- A guarded private-KVC backdrop downsample (~`0.33–0.36`) gives soft, bokeh-like diffusion that breaks up terminal/wallpaper text without Regular’s mush. AppKit resets it after layer rebuild and on window move/resize, so it is reapplied on a short timer series and from a Qt event filter. Failures are ignored.
- Public `Regular (0)` remains an advanced opt-in override for `macos27_glass` because it can smear over picture backgrounds.

### Stage-only settings (latest)
- The `Glass Material` selector (`Automatic / Default / Dark / Clear / Tinted`) was removed: it acted as a confusing modifier that changed appearance independently of the stage.
- `Glass Stage` is now the only `macos27_glass` appearance control and exposes exactly `Dark` and `Light`:
  - `Dark` → `DarkAqua`, dark native tint, dark QSS scrim.
  - `Light` → `Aqua`, white native tint, white QSS scrim with dark text.
- `configure_macos_glass()` and `build_liquid_qss()` derive their profiles from the stage, not from a separately selected material. `glass_material` survives only as an internal derived value via `GLASS_STAGE_MATERIAL`.
- Advanced tuning sliders persist clamped **per stage** in `glass_overrides` under `dark` / `light`: `backdrop_scale`, `tint_alpha`, `card_alpha` / `list_alpha`, and public `Native Style` (`clear` / `regular`). `Reset Stage Tuning` clears only the active stage.
- Existing sessions migrate legacy material-keyed tuning (`dark` → `dark`, `clear` → `light`) and legacy material choices into the two stages. Stage is persisted as `glass_appearance`.
- Double-clicking **Glass Stage:** calls `read_system_glass_preferences()` and maps public OS appearance signals to a best-effort `Dark` / `Light` stage. It does **not** claim exact macOS `Default / Dark / Clear / Tinted` detection.
- `SettingsDialog` uses a frameless `QScrollArea`; minimum height is `420`, `_fit_to_screen()` caps it to the available screen (max width `700`, height `avail.height() - 80`), and a vertical scrollbar appears on short displays.

### Ambient Glow (latest)
- `LavaLampOverlay.paintEvent()` now uses peak alpha scales:
  - `macos27_glass`: `0.09 * glow` (was `0.015`).
  - original `Glass`: `0.22 * glow` (was `0.12`).
  - `Classic` / `Light`: unchanged (`0.44` / `0.36`).
- `macos27_glass` remains static with no dither tile so the stronger wash does not muddy the native material. The old ceiling was too weak for the user even at max.

### Verification
- `python3 -m py_compile player_ui/main.py player_ui/liquid_glass.py player_ui/glass_lens.py` passes.
- `git diff --check` passes.
- Offscreen smoke test passes. It asserts:
  - settings area scrolls and the dialog is capped to the screen,
  - there is no `combo_glass_material` / `lbl_material`,
  - `Glass Stage` contains exactly `['Dark', 'Light']`,
  - Light defaults are backdrop `36`, tint `27`, scrim `162`,
  - tuning is stored under the `light` stage key and reset removes only that key,
  - QSS honors the stage even when a legacy material is passed,
  - native helper receives stage-based scale / tint / public style overrides.
- Live probes confirmed a helper scale move from `0.5 → 0.35`, a held `0.33` through layer rebuild/move/resize, and a stage override applying style `0` (Regular), scale `0.30`, and white tint alpha `0.12`.

## Still unresolved
- User visual check after restart: confirm only `Dark` / `Light` appear, each stage keeps its own tuning, and Ambient Glow at max is strong enough.
- If bright terminal text is still legible, lower `Backdrop Downsample` in small steps; if the panel gets mushy, raise it.
- Exact native material-mode detection and a public numeric blur-radius API do not exist on macOS; keep the tuning sliders labeled best-effort/experimental.

## Important constraints / gotchas
- `pyqt_liquidglass` is installed in `.venv` and reports `HAS_GLASS_EFFECT True`, `MACOS_VERSION (27, 2, 1)`.
- Offscreen macOS tests that call real native glass functions can segfault; monkeypatch `main.glass`, `apply_seamless_titlebar`, `apply_windows_glass`, and `reset_macos_window_opaque` in smoke tests, and stub `pyqt_liquidglass._bridge.get_nswindow_from_widget` before calling `configure_macos_glass()`.
- `NSColor.whiteComponent()` needs a colorspace conversion in probes; use `redComponent()` / `greenComponent()` / `blueComponent()` instead.
- On macOS, never toggle `WA_TranslucentBackground` after `window.show()`; it recreates the native backing store and causes black/gray glitches. Set it once at creation, guarded by `windows_transparency_enabled()` on Windows.
- Keep QSS borders uniform (`border: 1px solid ...`) to avoid 45° corner seams with `border-radius`.
- Keep `:disabled` after sub-controls in QSS (e.g. `QSlider::sub-page:horizontal:disabled`).
- Heavy modules stay lazy; do not regress fast startup.

## Key files
- `player_ui/main.py` — theme constants, `SettingsDialog` stage-only controls, theme switching, native glass hooks, lens hooks, graph theme, session persistence/migration.
- `player_ui/liquid_glass.py` — `build_liquid_qss()`, `configure_macos_glass()`, stage profiles/overrides, system sync, lava overlay.
- `player_ui/glass_lens.py` — lens overlay widget.
- `player_ui/LICENSES/liquid-glass-webgpu.LICENSE` — upstream MIT license.
- `.github/workflows/build-release.yml` — license bundling.
- `README.md` / `AGENTS.md` — user-facing and project-level behavior notes.
- `~/.hrtf_audio_player/session.json` — user session.

## Local commands (safe, no network)
```bash
cd "/Users/max/Projects/REXA copy"
git status --short
python3 -m py_compile player_ui/main.py player_ui/liquid_glass.py player_ui/glass_lens.py
git diff --check
```

## Commit / push instructions
- Committing locally is fine; **push is forbidden until the user explicitly approves it**.
- Commit only intended tracked files, never the local build artifacts:
```bash
cd "/Users/max/Projects/REXA copy"
git add AGENTS.md README.md docs/LIQUID_GLASS_HANDOFF.md \
  player_ui/main.py player_ui/liquid_glass.py
git -c user.name="REXA Dev" -c user.email="dev@rexa.local" commit -m "feat(ui): simplify glass stages and strengthen ambient glow"
```
- **Only after explicit user approval** (e.g. “you can push now”):
```bash
git push origin main        # do not run until approved
```
- Do **not** create/push tags (`v*`) until separately approved; tags trigger release builds.
