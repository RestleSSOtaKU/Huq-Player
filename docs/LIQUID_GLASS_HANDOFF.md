# Liquid Glass / macOS 27 UI Work — Handoff

> **PUSH POLICY: DO NOT PUSH TO GITHUB.**
> Push only when the user explicitly says something like: “you can push now”, “push it”, or “it can be pushed”.
> Local commits are allowed and encouraged so progress survives model switches. Never create/push a `v*` tag without explicit approval too.

## Goal
- Make REXA’s `Glass macOS 27` theme look like the clean native macOS dark glass panels (visible desktop blur, thin rim, neutral tint) instead of a fake dimming layer.
- Keep the new theme applied through the **whole app** (main window, settings, dialogs, menus, cards, spectrum, cover lens), not just one widget.

## Current state (local, uncommitted work at time of writing)
- Branch: `main`.
- Working tree contains the liquid-glass work described below plus local build artifacts that must **not** be committed (`REXA-Windows.zip`, `REXA-Windows/`, `macos_tap/libhuq_tap.dylib`).
- No push has been made.

### Completed so far
- Added `player_ui/glass_lens.py`: static cached lens pill over the album cover, ported from MIT-licensed WebGPU shader math (SDF + dispersion + Fresnel/rim), drawn with NumPy + QPainter (no browser/WebGPU runtime). Per user feedback it only appears in the original `Glass` theme **and only when a real cover is loaded**; the accent-gradient fallback was removed because it painted a bright cyan pill when no cover existed. `macos27_glass` hides it to match native macOS Settings.
- Added `Glass macOS 27` as a fourth theme:
  - ID `macos27_glass`, label `Glass macOS 27`.
  - Settings → Appearance → Theme, toolbar theme cycle, persistence in `session.json`, dialog/menu/graph styling all recognize it.
  - Cycle order: `Classic → Light → Glass → Glass macOS 27`.
- Added `player_ui/LICENSES/liquid-glass-webgpu.LICENSE` and bundled it into Windows/macOS release artifacts in `.github/workflows/build-release.yml`.
- Reworked `build_liquid_qss()` in `player_ui/liquid_glass.py` to use a thin dark veil over the native material. The old `Glass Opacity` slider was replaced by a discrete `Glass Material` selector because tint/QSS dimming could not produce a true native blur change.
- Verified with `py_compile` and offscreen Qt smoke tests (theme switching, settings combo, lens visibility, session persistence).

### Native glass fix (latest, not yet verified by user)
- `pyqt_liquidglass` creates `NSGlassEffectView` with its default **`Regular`** style (`NSGlassEffectViewStyleRegular = 0`), which is the dense/mushy material. macOS Settings/Control Center panels use **`Clear`** (`= 1`). The earlier light/white direction was also wrong: references are dark panels over a picture background.
- Added `configure_macos_glass()` in `player_ui/liquid_glass.py`. Called from every place that applies native glass (main window, `SettingsDialog`, track-properties dialog, startup, and the Glass Material selector). It:
  - maps every `macos27_glass` preset to `NSGlassEffectViewStyleClear (1)` and varies native tint + QSS scrim density; original `Glass` keeps `Regular (0)` because Clear is required for the native look,
  - sets the backdrop layer's downsample scale to `0.33–0.36` via guarded private KVC for soft, bokeh-like diffusion. Native Clear is `0.5` (bright text reads through) and Regular is `0.25` (smeared); an intermediate value breaks up terminal/wallpaper text without Regular's mush. AppKit resets this value after the initial style change and on every window move/resize, so it is reapplied on a short timer series and from a Qt event filter. Failures are ignored and the public Clear style remains,
  - sets the `NSWindow` appearance to `DarkAqua` for `macos27_glass` (less bright/blue than `VibrantDark`) and `VibrantDark` for original `Glass`,
  - keeps `NSGlassEffectView.alphaValue = 1.0` — lowering it suppresses the material and makes the window flat; each mode varies native `tintColor` plus a dark QSS scrim instead,
  - sets a `14pt` corner radius and calls `setNeedsDisplay_` after style changes,
  - restores the default appearance/alpha/style when leaving glass themes.
- `Glass Opacity` was replaced by `Glass Material`: `Automatic / Default / Dark / Clear / Tinted`. macOS has no public blur-radius API and no public read for the exact native mode; these are dark tint/scrim presets, not private mode reads or exact mode detection. The only private detail is the best-effort backdrop-scale KVC above. Legacy numeric `glass_clarity` is kept in `session.json` as the Automatic/system-sync fallback.
- `macos27_glass` QSS now uses a **dark scrim** matched to the selected mode:
  - native `tintColor` alpha `0.27` Clear, `0.34` Default, `0.36` Tinted, `0.46` Dark; Automatic scales `0.24..0.50` from system clarity,
  - card fill `rgba(10,11,13,~162)` for Clear, `~174` for Default, `~184` for Tinted, `~194` for Dark; Automatic scales `140..190`,
  - list fill follows the same density ramp,
  - white text (`#f5f5f7`), thin white rim, dark combo popup, white spectrum axis labels,
  - main window/settings root are fully transparent so no square gradient layer cuts across the rounded native glass corners,
  - ambient/lava overlay is static (no timer) and much fainter in `macos27_glass` (`peak alpha 0.015 * glow`, no dither tile) so it does not muddy the native blur.
- `Glass` stays a dark, denser material (dark card fill `≈ 77`, background `≈ 94`).
- Accent handling: `macos27_glass` uses a muted dark accent branch; at `Ambient Glow = 0` it falls back to neutral gray `#8e8e93`, and `_on_ambient_color_changed()` re-derives the accent whenever the slider moves.
- Cover lens pill is hidden in `macos27_glass` and never renders the accent-gradient fallback.
- Offscreen tests pass: theme switching, settings combo, dark QSS values, neutral accent at zero glow, lens visibility rules.

### Manual system-glass sync (new)
- Double-click **Glass Material:** in Settings to call `read_system_glass_preferences()` in `player_ui/liquid_glass.py`, select `Automatic`, and set the legacy `glass_clarity` fallback from OS signals.
- macOS mapping (public APIs only): effective `NSApp.effectiveAppearance`, `AppleInterfaceStyle` + `AppleInterfaceStyleSwitchesAutomatically`, `NSWorkspace.accessibilityDisplayShouldReduceTransparency()`, and main wallpaper luminance (QImage can read macOS HEIC wallpapers in this environment). Reduce Transparency On → Dark + `100`; otherwise a best-effort mode (`automatic`/`dark`/`clear`/`tinted`/`default`) plus `12..94`.
- Windows mapping: registry `AppsUseLightTheme` + `EnableTransparency`. Transparency Off → Dark + `100`; On → Dark + `68` or Default + `48`. The existing DWM acrylic path already gives a native glass layer on Windows 11; the QSS dark scrim supplies the mode look on every platform.
- macOS does **not** publicly expose the exact `Automatic / Default / Dark / Clear / Tinted` choice. The double-click sync is an explicit approximation and shows the detected signals in a tooltip. Android is paused/experimental; it uses the in-app selector only until desktop parity work resumes.
- Offscreen tests pass: helper returns a macOS suggestion, `SettingsDialog` switches to Automatic from it, and the label double-click event emits the sync signal.

### Still unresolved
- User retesting after the timing fix. The first bokeh pass still showed terminal text because AppKit reset the private backdrop `scale` from `0.33` back to Clear's `0.5` after the layer tree settled and again on every window move/resize. `set_macos_glass_backdrop_scale()` now reapplies on a short timer series and from a Qt move/resize event filter; an on-screen PySide probe held `0.33` through layer rebuild, move, and resize. If text is still legible, lower `backdrop_scale` in small steps; if the panel gets mushy, raise it.
- More reference screenshots with picture backgrounds are welcome. Exact native mode detection remains unavailable through public AppKit APIs.

## Important constraints / gotchas
- `pyqt_liquidglass` is installed in `.venv` and reports `HAS_GLASS_EFFECT True`, `MACOS_VERSION (27, 2, 1)`.
- Offscreen macOS tests that call real native glass functions can segfault; monkeypatch `main.glass`, `apply_seamless_titlebar`, `apply_windows_glass`, and `reset_macos_window_opaque` in smoke tests.
- On macOS, never toggle `WA_TranslucentBackground` after `window.show()`; it recreates the native backing store and causes black/gray glitches. Set it once at creation, guarded by `windows_transparency_enabled()` on Windows.
- Keep QSS borders uniform (`border: 1px solid ...`) to avoid 45° corner seams with `border-radius`.
- Keep `:disabled` after sub-controls in QSS (e.g. `QSlider::sub-page:horizontal:disabled`).
- Heavy modules stay lazy; do not regress fast startup.

## Key files
- `player_ui/main.py` — theme constants (`GLASS_THEMES`, `THEME_ORDER`, `THEME_LABELS`, `is_glass_theme`), Settings combo, theme switching, native glass hooks, lens hooks, graph theme, session persistence.
- `player_ui/liquid_glass.py` — `build_liquid_qss()`, `make_glass_card()`, lava overlay, macOS/Windows glass helpers.
- `player_ui/glass_lens.py` — lens overlay widget.
- `player_ui/LICENSES/liquid-glass-webgpu.LICENSE` — upstream MIT license.
- `.github/workflows/build-release.yml` — license bundling.
- `docs/LIQUID_GLASS_HANDOFF.md` — this file.
- `~/.hrtf_audio_player/session.json` — user session; smoke tests must restore theme afterward (currently `liquid_glass`).

## Local commands (safe, no network)
```bash
cd "/Users/max/Projects/REXA copy"
git status --short
python3 -m py_compile player_ui/main.py player_ui/liquid_glass.py player_ui/glass_lens.py
QT_QPA_PLATFORM=offscreen .venv/bin/python - <<'PY'
# smoke-test pattern: monkeypatch native glass calls before constructing MainWindow
import sys
sys.path.insert(0, "player_ui")
from PySide6.QtWidgets import QApplication
import main
app = QApplication.instance() or QApplication([])
main.glass = None
main.apply_seamless_titlebar = lambda w: None
main.apply_windows_glass = lambda *a, **k: None
main.reset_macos_window_opaque = lambda w: None
w = main.MainWindow(); w.show()
print(w.current_theme, w.glass_lens.isVisible())
PY
```

## Commit / push instructions
- Committing locally is fine; **push is forbidden until the user explicitly approves it**.
- Use inline identity if repo identity is unset:
```bash
cd "/Users/max/Projects/REXA copy"
git -c user.name="REXA Dev" -c user.email="dev@rexa.local" add \
  AGENTS.md docs/LIQUID_GLASS_HANDOFF.md \
  player_ui/main.py player_ui/liquid_glass.py player_ui/glass_lens.py \
  player_ui/LICENSES/liquid-glass-webgpu.LICENSE \
  .github/workflows/build-release.yml
git -c user.name="REXA Dev" -c user.email="dev@rexa.local" commit -m "feat(ui): add Glass macOS 27 theme and cached lens overlay"
```
- **Only after explicit user approval** (e.g. “you can push now”):
```bash
git push origin main        # do not run until approved
```
- Do **not** create/push tags (`v*`) until separately approved; tags trigger release builds.
