# Liquid Glass / Glass 2.0 UI Work — Handoff

> **PUSH POLICY: DO NOT PUSH TO GITHUB.**
> Push only when the user explicitly says something like: “you can push now”, “push it”, or “it can be pushed”.
> Local commits are allowed and encouraged so progress survives model switches. Never create/push a `v*` tag without explicit approval too.

## Goal
- Make REXA’s `Glass 2.0` theme look like clean native macOS glass panels (visible desktop diffusion, thin rim, neutral tint) instead of a fake dimming layer.
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
- Added `Glass 2.0` as a fourth theme (internal key stays `macos27_glass` for session/QSS compatibility; visible label is `Glass 2.0`), with cycle order `Classic → Light → Glass → Glass 2.0` and persistence in `session.json`.
- Added `player_ui/glass_lens.py`: a static cached WebGPU-style lens pill over album art. It appears only in the original `Glass` theme and only with a real cover pixmap; the accent-gradient fallback was removed. It is hidden in `macos27_glass` to match native macOS Settings.
- Added `player_ui/LICENSES/liquid-glass-webgpu.LICENSE` and bundled it into Windows/macOS release artifacts in `.github/workflows/build-release.yml`.

### Native glass baseline
- `configure_macos_glass()` in `player_ui/liquid_glass.py` is called whenever native glass is applied: main window, `SettingsDialog`, track-properties dialog, startup, theme changes, and stage changes. It now also calls `configure_macos_panel_glass()` to add per-card native material.
- `macos27_glass` uses `DarkAqua` by default, a `14pt` corner radius, transparent root, and the public `Clear` style. Original `Glass` keeps `VibrantDark` + public `Regular (0)`. `VibrantDark` read too bright/blue next to native Settings for `macos27_glass`.
- `NSGlassEffectView.alphaValue` always stays `1.0`; lowering it hides the material and makes the window flat. Density comes from native `tintColor` plus QSS scrim.
- The guarded private-KVC backdrop downsample is now fixed: full/sharpest `0.50` in normal mode, eased to `~1/3` (`0.33`) only in Low-End CPU mode. AppKit resets it after layer rebuild and on window move/resize, so it is reapplied on a short timer series, from a Qt event filter, and whenever CPU mode changes. Failures are ignored.
- Public `Regular (0)` remains an advanced opt-in override for `macos27_glass` because it can smear over picture backgrounds.

### Per-card native glass (latest)
- QSS cannot blur the wallpaper behind a panel, so `configure_macos_panel_glass()` adds an `NSGlassEffectView` behind each visible `QFrame#glassCard`, inserted below Qt content and above the window-wide material.
- Fix: insert into `root_view.superview()` positioned `NSWindowBelow` relative to `root_view` (the same sibling-injection strategy as `pyqt_liquidglass`). Inserting into `contentView()` while passing `root_view` as an invalid sibling made AppKit stack every card glass above the entire UI. The container is cached on the window and rebuilt if a layer rebuild swaps it.
- Both `mainScroll` and `settingsScroll` connect their vertical scrollbar's `valueChanged` to `_sync_macos_panel_glass()` so native card glass follows scrolling inside the player and the non-modal Settings dialog. Only cards whose `window() is` the configured window are included, so parented dialogs do not duplicate views in the main window. Per-record error handling hides a single stale view instead of aborting the whole sync.
- `SettingsDialog.showEvent()` removes the previous window effect id and per-card views before `sync_from_main()` reapplies them. AppKit drops those native views while the dialog is hidden, so reusing the cached id/container made the second open render with no glass at all.
- Cards follow their widget geometry via an event filter + `QTimer.singleShot(0, ...)`; hidden cards are hidden natively. Dark uses panel tint alpha `0.16` with near-black RGB; Light uses `0.06` white. Both keep `alphaValue = 1.0`.
- The QSS scrim was lightened to `176` cards / `168` list in Dark so this real material is visible instead of being buried under an opaque veil.
- Offscreen tests must stub `pyqt_liquidglass._bridge.get_nsview_from_widget` as well as `get_nswindow_from_widget`; calling real native bridge functions offscreen can segfault.

### Stage-only settings (latest)
- The `Glass Material` selector (`Automatic / Default / Dark / Clear / Tinted`) was removed: it acted as a confusing modifier that changed appearance independently of the stage.
- `Glass Stage` is now the only `macos27_glass` appearance control and exposes exactly `Dark` and `Light`:
  - `Dark` → `DarkAqua`, deeper native tint (`0.52` / near-black RGB; slider capped at `52`), lighter QSS scrim (`176` cards, `168` list) so text stays readable over bright wallpapers while the per-card native blur still shows through.
  - `Light` → `Aqua`, white native tint, white QSS scrim with dark text.
- `configure_macos_glass()` and `build_liquid_qss()` derive their profiles from the stage, not from a separately selected material. `glass_material` survives only as an internal derived value via `GLASS_STAGE_MATERIAL`.
- Advanced tuning sliders persist clamped **per stage** in `glass_overrides` under `dark` / `light`: `tint_alpha`, `card_alpha` / `list_alpha`, and public `Native Style` (`clear` / `regular`). `Reset Stage Tuning` clears only the active stage. The old `Backdrop Downsample` slider was removed — full quality is always used except in Low-End CPU mode.
- Existing sessions migrate legacy material-keyed tuning (`dark` → `dark`, `clear` → `light`) and legacy material choices into the two stages. Stage is persisted as `glass_appearance`.
- Double-clicking **Glass Stage:** calls `read_system_glass_preferences()` and maps public OS appearance signals to a best-effort `Dark` / `Light` stage. It does **not** claim exact macOS `Default / Dark / Clear / Tinted` detection.
- `SettingsDialog` uses a frameless `QScrollArea`; minimum height is `420`, `_fit_to_screen()` caps it to the available screen (max width `700`, height `avail.height() - 80`), and a vertical scrollbar appears on short displays.

### Main window DSP scrolling (latest)
- The whole player now lives in `QScrollArea#mainScroll` with a transparent viewport and a transparent `QWidget#mainContent`. Both are created with `WA_TranslucentBackground` / `WA_NoSystemBackground` and `autoFillBackground=False`; otherwise a plain content widget paints the palette window color and hides the native glass. Opening DSP controls (or the Stereo scope) grows scrollable content instead of compressing the cover, transport, and spectrum.
- `update_cover_art_size()` no longer subtracts the DSP panel height and clamps the base proportion to `h = min(self.height(), 820)`; the cover keeps its stock size when the panel opens or the window grows.
- `_sync_main_content_height()` caps content at the viewport height in the default state (so stock proportions and the hidden scrollbar match the pre-scroll UI), then adds only the DSP panel's height when it opens; scrolling appears only when needed.
- Toggling DSP/Stereo on auto-scrolls to the bottom via short `QTimer.singleShot` calls; users can scroll back up to the cover and other controls.
- DSP controls always start closed; `dsp_panel_open` is no longer saved or restored, so stale session state cannot open the panel on startup.
- Default window is `1100x880`; the library starts at `760` and is explicitly top-aligned with the right panel, and the extra default height goes only to the spectrum graph (about `209px` tall after the new perf row) so the full helper tip is visible. On shorter screens the default height clamps to `max(820, available - 20)`.
- When the viewport grows past `880` (maximize/fullscreen), `_sync_main_content_height()` grows the library by the same extra height: `760 + max(0, viewport_height - 880)`. The cover/top controls keep their stock `820px`-window proportions, while the library and spectrum fill the taller window.
- Returning from maximize/fullscreen now restores the exact saved window geometry and graph height without a visible intermediate stretch. Growing the library's fixed height raised the effective minimum size, so Qt/AppKit shrank only part way and the spectrum stayed too tall. `WindowDragFilter._animate_to()` and `MainWindow.showNormal()` now settle the exact Qt geometry synchronously (bounded `setGeometry()` + `processEvents()` loop) before the next paint; the filter saves the Qt content geometry rather than the AppKit frame rect. The main-scroll smoke test includes a fullscreen round-trip assertion.
- `update_cover_art_size()` now always pins `lbl_cover` to the computed stepped size. The old conditional skipped pinning when the layout already matched, letting a later relayout collapse the cover to its `100px` minimum after the perf row was added.

### Lazy cover loading (latest)
- Cover tag/folder extraction is memoized with stamp-keyed LRU caches, so repeated album rows and shared folder art do not rescan or reparse large tags.
- Mini-cover decoding uses four workers and tracks futures per path; queued work for rows that scroll out of view is cancelled so visible rows finish at an even pace.
- `_decode_mini_thumb()` uses `QImageReader.setScaledSize()` to decode straight to a ~88px thumbnail instead of inflating a multi-megapixel cover before scaling. In a local probe this dropped 8 decodes of a 3000×3000 JPEG from ~255ms to ~27ms.
- Decoded thumbnails are cached by a content hash of the cover bytes, so every track in an album reuses one thumbnail even after `_visible_covers` evicts its row pixmaps.
- `mutagen` is imported inside the worker under a lock, not on the UI thread during the first scroll; the LRU cache also replaces the old "clear everything at 512 entries" behavior.
- All decoder failures emit an empty image so a pending path can never stay stuck.

### Ambient Glow (latest)
- `LavaLampOverlay` uses peak alpha scales:
  - `macos27_glass`: `0.09 * glow` (was `0.015`).
  - original `Glass`: `0.22 * glow` (was `0.12`).
  - `Classic` / `Light`: unchanged (`0.44` / `0.36`).
- The renderer now caches three premultiplied Gaussian blob sprites and composites them into one `256x160` frame, then upscales that frame once with `SmoothPixmapTransform`. The old per-pixel `exp()` field and full-window dither tile are gone; dither is baked into the small frame only.
- `macos27_glass` remains static with no dither tile so the stronger wash does not muddy the native material. The old ceiling was too weak for the user even at max.
- Dark `Glass Border Rim` now eases the upper half of the slider (`r_highlight = min(r, 0.5) + 0.30 * max(0, r - 0.5)`) so white rim/button/input highlights stop blowing out past the middle. Light and the original `Glass` theme keep their existing curves.
- Stock `Glass Border Rim` is now `10` via `DEFAULT_GLASS_RIM`; that single constant drives QSS generation, the Settings reset value, and fresh/missing session state.
- Panel Scrim's slider is now `50–100` only: `50%` stores `card_alpha=0` / `list_alpha=0` (fully transparent QSS) and `100%` stores alpha `255`. The old lower half was cut off instead of being a dead zone, so the whole track ramps the scrim. `_alpha_to_scrim_slider()` / `_scrim_slider_to_alpha()` map between the slider and stored alpha.
- `LavaLampOverlay._tick()` keeps the drift dynamic at 30 FPS idle and drops to a lighter 15 FPS while `engine.is_playing` is true, with a compensating phase step. Classic/Glass no longer spend a second full-window paint loop competing with the spectrum timer, but the ambient motion no longer freezes during playback.
- Session restore now rebuilds QSS from loaded `glass_overrides` even when the saved theme already matches the startup default, so Panel Scrim is correct on first paint instead of only after a Settings tweak. `save_session_state()` writes to a temp file and `os.replace()`s it, preventing a startup read from catching half-written JSON and silently falling back to stock scrim.
- Glass 2.0 also re-applies native glass on `QEvent.Show` / `QEvent.WindowActivate` (coalesced via `_flush_native_glass_refresh`) plus one delayed post-startup push. AppKit otherwise defers the material render until the user closes Settings, making startup panels look more transparent than the identical final QSS.
- `SettingsDialog.hideEvent()` now drops the dialog's own native glass effect and per-card views (`_clear_native_glass()`), matching the rebuild already done in `showEvent()`. Dialog-owned `NSGlassEffectView`s previously survived `close()` and re-composited over the main window on the first activation, which darkened the panels once; now startup, post-close, and forced-reapply captures match.

### Live graph performance readout (latest)
- The visible theme label is now `Glass 2.0`; the internal key stays `macos27_glass` so existing session/QSS values keep working.
- `lbl_perf` sits under the Graph FPS selector and reads achieved/target FPS, e.g. `Perf: 58/60 FPS · Full speed`. Settings mirrors it in `lbl_perf_status` / `lbl_perf_tips`.
- Full Quality now starts at the sustainable **60 FPS** target (`GRAPH_FPS_DEFAULT_INDEX = 2`, 16 ms `Qt.PreciseTimer`) instead of forcing 120 FPS; `120 FPS` remains opt-in. Leaving Low-End CPU Mode restores 60 FPS, not 120. This fixes the "low performance even in Classic" reading caused by Qt timer coalescing at 8 ms.
- `_track_graph_fps()` counts real `update_graph()` callbacks over a `0.7s` window and applies light EMA smoothing, so dropped frames show up even if the QTimer interval is small.
- Below `90%` of the selected target, the tooltip/tips suggest `60 FPS`, disabling Spectrum Gradient Fill, and Low-End CPU Mode; in `macos27_glass` it also notes that per-card native blur makes `120 FPS` best-effort.
- The readout resets on play/pause, playback stop, Graph FPS changes, and CPU mode changes. It never persists temporary measurements to `session.json`.

### Settings gating & white Glass 2.0 text
- Glass 2.0-only controls are disabled, **labels included**, outside the `macos27_glass` theme: `Glass Stage`, `Native Tint`, `Panel Scrim`, `Native Style`, and `Reset Stage Tuning`. `Ambient Glow` and `Glass Border Rim` stay active where they apply; these are hidden/re-enabled by `SettingsDialog.sync_from_main()` on every theme change.
- On the `macos27_glass` Light stage (and plain `Light`), muted text uses solid `#3c3c43` and disabled labels/buttons use `#6e6e73`. Inline `opacity: 0.7x` styles were replaced with explicit colors so tips, perf status, and device hints do not wash out on white panels.
- `MainWindow._refresh_muted_text()` follows theme/stage changes for the device hint and idle perf readout, but preserves live green/orange device and perf status colors. Dark themes keep the original `#8e8e93` family.
- `is_light_surface()` is now the single check for bright surfaces: plain `Light` **or** `macos27_glass` with the Light stage. `MarqueeDelegate` rows/headers, `StereoScopeWidget`, `PeakSchemeWidget`, context menus, and `_update_graph_theme()` use it so library and graph text is dark on white and bright on dark. `_update_graph_theme()` also recolors axis labels/ticks and runs when the Glass Stage changes.

### Repeated theme-switch cleanup
- `clear_macos_glass_state()` drops `window._glass_view`, the backdrop-scale token/value, and the move/resize event filter whenever `remove_glass_effect()` runs or a non-glass theme is configured. `configure_macos_glass(..., False)` no longer touches the stale AppKit view.
- `remove_macos_panel_glass()` now removes `_PanelGlassFilter` from the window and every card, bumps a generation counter so queued sync callbacks are ignored, and lets Qt delete the old filter. `schedule_macos_panel_glass_sync()` replaces raw `QTimer.singleShot()` calls from scrollbars and widget events so a burst of Move/Resize/Show/Hide collapses into one per-card sync.
- `reset_macos_window_opaque()` now uses PyObjC `className()` instead of reading `-className` as a C string via ctypes. The old code read raw `NSString` bytes and was the intermittent segfault after several theme changes.
- `_MacosGlassScaleFilter` also coalesces Move/Resize callbacks; rapid switches no longer queue dozens of stale backdrop-scale retries.

### Release metadata (v0.5.3)
- `player_ui/main.py` `APP_VERSION` is now `0.5.3`; `README.md` and `AGENTS.md` carry matching `v0.5.3` notes.
- The workflow still derives release titles and artifact names from the pushed tag, so no hardcoded workflow version was changed. A local `v0.5.3` tag is enough to publish `REXA v0.5.3` once explicitly approved.
- Same-version bugfix: track changes now apply background, accent, and lava together from the current cover. The old 600 ms ambient fade was removed because it left the previous hue visible in the wash while the accent/lava had already switched; static themes hid the mismatch, but the dynamic lava made it obvious. Glass 2.0 now also blends the sampled cover color into the native window **and per-card panel** tints (Dark keeps near-black luminance, Light keeps white, blend scales with Ambient Glow; cards use a weaker mix for readability) and reapplies it on cover changes; QSS stays transparent to avoid the square cut-off edge. The reapply is gated on the color actually changing so resize-driven `update_cover_art_size()` calls cannot rebuild panel glass and leave mirrored panel fragments at the window borders.
- Same-version bugfix: `lbl_peak` reserves a fixed width and `_set_peak_readout()` elides overflow. Limiter/clipping strings no longer increase the spectrum card's minimum width and widen the window on hot tracks.

### Verification
- `python3 -m py_compile player_ui/main.py player_ui/liquid_glass.py player_ui/glass_lens.py` passes.
- `git diff --check` passes.
- All three offscreen smoke tests pass (settings/QSS/native-glass, main-window scroll/proportions, and panel-glass z-order). The settings test asserts:
  - settings area scrolls and the dialog is capped to the screen,
  - there is no `combo_glass_material` / `lbl_material` or backdrop slider,
  - `Glass Stage` contains exactly `['Dark', 'Light']`,
  - Dark defaults are tint `52`, scrim `176`; Light defaults are tint `27`, scrim `162`,
  - tuning is stored under the `light` stage key and reset removes only that key,
  - Panel Scrim spans `50–100` with `50` transparent and `100` full alpha,
  - Glass 2.0-only controls and their labels disable in `Classic`/`Light`/original `Glass` while `Glass Border Rim` remains active,
  - white Glass 2.0 uses solid `#3c3c43` secondary text and `#6e6e73` disabled text,
  - `is_light_surface()` treats only plain `Light` and the `macos27_glass` Light stage as bright (original `Glass` / `liquid_glass` stay dark),
  - mini covers decode to an expanding 88px crop and share one cached thumbnail per cover-byte hash,
  - QSS honors the stage even when a legacy material is passed,
  - native helper receives the fixed full/low-end scale plus stage tint / public style overrides.
- The main-window test asserts `QScrollArea#mainScroll` / `QWidget#mainContent` transparency, a hidden scrollbar by default, no cover/transport/graph squish when DSP opens, a `190px` cover, a left card fixed at `760` and top-aligned with the right panel, a graph at least `200px` tall, the 60 FPS / 16 ms `PreciseTimer` default and 10 ↔ 60 CPU-mode FPS restore, that the lava overlay keeps repainting but switches from a 33 ms idle timer to a 66 ms playback timer, that the idle perf/device labels switch to `#3c3c43` on white Glass 2.0 while live status keeps its own color, and that spectrum axis pens/ticks/labels switch between `#1c1c1e` and `#ffffff` with the stage.
- Live probes confirmed a helper scale move from `0.5 → 0.35`, a held `0.33` through layer rebuild/move/resize, and a stage override applying style `0` (Regular) with white tint alpha `0.12`; the same reapply path now drives the fixed full/low-end backdrop scale.
- The main-window smoke test also toggles the theme eight times with a fake glass module and asserts apply/remove counts stay balanced with no stale `_glass_view`, backdrop scale, or scale filter.
- A native Cocoa stress test (`QT_QPA_PLATFORM=cocoa`, 12 theme changes with real `pyqt_liquidglass`) passes without a crash. It asserts the `NSGlassEffectView` count is identical on every glass theme and returns to zero on a non-glass theme; panel-glass views and `glass_effect_id` are cleared.
- Cover-tint probes confirm both window and per-card native tints follow red/blue covers and fall back to the stock preset at zero Ambient Glow; the glass-reapply probe confirms no reapply on resize and one on a real cover change.
- The peak-layout probe confirms every plain/limiter/clipping readout keeps the label at `200px`, inside its card with `QSizePolicy.Fixed`, and leaves the window's size/minimum hints unchanged. `Ignored` was the wrong policy here: it made the row place the label past the card edge, so the readout looked invisible.

## Still unresolved
- User visual check after restart: confirm only `Dark` / `Light` appear, each stage keeps its own tuning, Ambient Glow at max is strong enough, Dark rim past the middle is calmer, Dark **cards as well as the window** show native blur, Dark `Native Tint` stops at `52`, the cover/top controls keep their original proportions while the graph is taller, covers fill evenly, and the DSP panel opens at the bottom without squishing anything.
- User visual check after restart: confirm original `Glass` greys out every Glass 2.0-only row including its label, and white Glass 2.0 tips/status/library rows/spectrum labels are comfortably readable with dark text.
- There is no manual backdrop tuning now; if the full-scale look is too sharp, the only built-in reduction is Low-End CPU mode's `~1/3` scale.
- Exact native material-mode detection and a public numeric blur-radius API do not exist on macOS; keep the tuning sliders labeled best-effort/experimental.

## Important constraints / gotchas
- `pyqt_liquidglass` is installed in `.venv` and reports `HAS_GLASS_EFFECT True`, `MACOS_VERSION (27, 2, 1)`.
- Offscreen macOS tests that call real native glass functions can segfault; monkeypatch `main.glass`, `apply_seamless_titlebar`, `apply_windows_glass`, and `reset_macos_window_opaque` in smoke tests, and stub both `pyqt_liquidglass._bridge.get_nswindow_from_widget` and `get_nsview_from_widget` before calling `configure_macos_glass()`.
- `NSColor.whiteComponent()` needs a colorspace conversion in probes; use `redComponent()` / `greenComponent()` / `blueComponent()` instead.
- On macOS, never toggle `WA_TranslucentBackground` after `window.show()`; it recreates the native backing store and causes black/gray glitches. Set it once at creation, guarded by `windows_transparency_enabled()` on Windows.
- Keep QSS borders uniform (`border: 1px solid ...`) to avoid 45° corner seams with `border-radius`.
- Keep `:disabled` after sub-controls in QSS (e.g. `QSlider::sub-page:horizontal:disabled`).
- Heavy modules stay lazy; do not regress fast startup.

## Key files
- `player_ui/main.py` — theme constants, `SettingsDialog` stage-only controls, theme switching, native glass hooks, lens hooks, graph/default window sizing, scrollable player layout, lazy covers, session persistence/migration.
- `player_ui/liquid_glass.py` — `build_liquid_qss()`, `configure_macos_glass()`, `configure_macos_panel_glass()`, stage profiles/overrides/tint cap, system sync, lava overlay.
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
