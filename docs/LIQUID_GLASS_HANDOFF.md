# Liquid Glass / macOS 27 UI Work — Handoff

> **PUSH POLICY: DO NOT PUSH TO GITHUB.**
> Push only when the user explicitly says something like: “you can push now”, “push it”, or “it can be pushed”.
> Local commits are allowed and encouraged so progress survives model switches. Never create/push a `v*` tag without explicit approval too.

## Goal
- Make REXA’s `Glass macOS 27` theme look like the clean native macOS Settings material (bright, low-contrast veil, thin rim, visible desktop blur) instead of the current muddy/dark look.
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
- Reworked `build_liquid_qss()` twice in `player_ui/liquid_glass.py` to reduce QSS alpha (thin white veil instead of dark opaque fill), weaken lava/ambient tint, and add tooltips explaining `Glass Opacity` (lower = cleaner native glass).
- Verified with `py_compile` and offscreen Qt smoke tests (theme switching, settings combo, lens visibility, session persistence).

### Native glass fix (latest, not yet verified by user)
- Root cause found: `pyqt_liquidglass` creates `NSGlassEffectView` with its default **`Regular`** style (`NSGlassEffectViewStyleRegular = 0`). The user’s latest references are **dark-mode** macOS panels over a picture background, not light panels, so the earlier light/white direction was wrong.
- Added `configure_macos_glass()` in `player_ui/liquid_glass.py`. Called from every place that applies native glass (main window, `SettingsDialog`, track-properties dialog, startup, and both Glass Opacity sliders). It:
  - keeps `NSGlassEffectView.style = 0` (`Regular`) for a native dark look,
  - sets the `NSWindow` appearance to `VibrantDark` for both glass themes,
  - keeps `NSGlassEffectView.alphaValue = 1.0` — lowering it suppresses the material and makes the window flat; the slider varies the QSS veil instead,
  - sets a `14pt` corner radius to avoid square cut-off lines,
  - restores the default appearance/alpha/style when leaving glass themes.
- `macos27_glass` QSS is now a **dark native material** with a wide range driven by `glass_clarity` (`Glass Opacity` 0–100). The native material stays fully active underneath:
  - card fill `rgba(26,26,28,40→240)`,
  - list fill `rgba(20,20,22,30→220)`,
  - white text (`#f5f5f7`), thin white rim, dark combo popup, white spectrum axis labels,
  - main window/settings root are fully transparent so no square gradient layer cuts across the rounded native glass corners.
- `Glass` stays a dark, denser material (dark card fill `≈ 77`, background `≈ 94`).
- Accent handling: `macos27_glass` uses a muted dark accent branch; at `Ambient Glow = 0` it falls back to neutral gray `#8e8e93`, and `_on_ambient_color_changed()` re-derives the accent whenever the slider moves.
- Cover lens pill is hidden in `macos27_glass` and never renders the accent-gradient fallback.
- Offscreen tests pass: theme switching, settings combo, dark QSS values, neutral accent at zero glow, lens visibility rules.

### Still unresolved
- User is testing the dark clear→frosted range now. If it is still not matching the references, next suspects are: `NSGlassEffectView` needing `setTintColor_` / custom `contentStyle`, or the native view alpha not being honored on this macOS build. More reference screenshots with picture backgrounds are welcome.
- Earlier REXA screenshot was whiteish (`mean ≈ [159,160,163]`); reference panels are dark (`mean ≈ [26–41]`).

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
