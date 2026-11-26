 VIBE CODED 🖤

# REXA — Reflectionless Engine for X-dimensional Audio

A zero-echo, real-time **HRTF (Head-Related Transfer Function)** binaural desktop audio player powered by a native **Rust** DSP backend (`hrtf_core`) and a **Python (PySide6)** interface.

| Liquid Glass |
| :---: |
| ![Liquid Glass Theme](docs/preview-glass.png) |

| Classic (Dark) | Light |
| :---: | :---: |
| ![Classic Theme](docs/preview-classic.png) | ![Light Theme](docs/preview-light.png) |

## Overview

- **Anechoic Engine (`hrtf_core`):** Rust-based 4-channel partitioned FFT convolution and biquad shelving.
- **Driver-Free System Audio (`Go Live`):** User-space macOS CoreAudio Process Tap (`macos_tap/huq_tap.m`) with automatic native sample-rate locking and live adaptive `Tonal Match`.
- **Audiophile Safety & Precision:** True-peak `-0.5 dBFS` auto-headroom limiter with live `LIM -X.X dB` gain-reduction readout, `Float (32-bit Raw)` output mode, and RME-style 2.0s Left-Click / Instant Right-Click `Bit-Perfect` unity pass-through.
- **Python Desktop UI (`player_ui`):** Fast library scanning, `Open File`, drag-and-drop, global keyboard shortcuts, hold-to-cycle letter navigation, SOFA parsing, and real-time spectrum plotting.
- **Adaptive Hardware Performance:** Includes hardware benchmarking and a `CPU: Full Quality / Low-End` toggle.
- **Format Support:** Decodes FLAC, WAV, MP3, M4A, OGG, and Opus via FFmpeg.

## Prebuilt Releases

For prebuilt Windows (`REXA-Windows.zip`) and macOS Apple Silicon (`REXA-macOS-AppleSilicon.dmg`) binaries, visit the project repository Releases page.

## Status & Platform Focus

- **Desktop (Windows & macOS Apple Silicon):** Active primary targets with full feature parity and prebuilt release binaries.
- **Android (`android_app` / `android_core`):** Early experimental prototype — currently paused and not yet in a usable state for daily listening.

## User Interface & Theming

* **Four-Theme Engine (`player_ui/liquid_glass.py`):**
  * **Classic (Dark):** Deep slate radial ambient wash (`20%` default Ambient Glow) with subtle translucent cards.
  * **Light:** Clean daylight surface (`30%` default Ambient Glow) with perceptual HLS-balanced album accents and high-contrast spectrum curves.
  * **Glass (Liquid Glass):** Native macOS 26 `NSGlassEffectView` / `NSVisualEffectView` refraction via `pyqt_liquidglass` (`100%` default Ambient Glow, `20%` Glass Border Rim) with a Windows 10/11 DWM Acrylic fallback (`apply_windows_glass`).
  * **Glass macOS 27:** Dark native `DarkAqua` Clear-style glass with a discrete **Glass Material** selector (`Automatic` / `Default` / `Dark` / `Clear` / `Tinted`), soft bokeh-like backdrop diffusion, and dark tint/scrim presets instead of a fake blur slider.
* **Dedicated Settings Window (`SettingsDialog` via `⚙`):**
  * Houses **Theme** (`Classic` / `Light` / `Glass` / `Glass macOS 27`), per-theme **Ambient Glow**, **Glass Material**, **Glass Border Rim** (automatically disabled outside `Glass` modes), and **Low-End CPU Mode**.
  * Sliders are `ResetSlider` instances—double-click any slider to snap back to that theme's stock default. Double-click **Glass Material:** to sync `Automatic` from macOS/Windows public appearance signals.
  * Styled with a diagonal bottom-right (`10%` up) to top-left ambient gradient (`QFrame#settingsRoot`) and persisted to `~/.hrtf_audio_player/session.json`.
* **Adaptive Cover Art & Marquee Typography:**
  * Dynamically samples dominant album-art colors (`sample_ambient_rgb`) with smooth `600 ms` `QVariantAnimation` transitions.
  * Long track titles and artist names use `MarqueeLabel` (`QSizePolicy.Ignored`) so cover art never shrinks, scrolling smoothly at 60 FPS on hover.
* **Cross-Platform Window & Safe Checks:**
  * macOS: Seamless full-bleed titlebar (`apply_seamless_titlebar`) and `WindowDragFilter` (click-drag anywhere, double-click zoom, triple-click fullscreen).
  * Windows: `windows_transparency_enabled()` checks `EnableTransparency` in the registry at startup so VMs or basic GPUs never render a black backing store in `Glass` mode.

## What's New in v0.5.2
- **One-Click Last.fm Browser Scrobbling:** Passwordless browser OAuth flow with Pano-style dual-threshold delay sliders (*Min Track Length*, *Percentage*, and *Minutes*) and a live scrobble progress bar.
- **Built-In In-App Auto-Updater:** Checks GitHub Releases directly from **Settings** and seamlessly updates & relaunches on both macOS and Windows.
- **Album-Grouped Library & Cover Art Inspector:** `Artist • Album` sorting with `44×44` album cover dividers, disc/track ordering, right-click **Show in Finder / Explorer**, and **Track Properties** with a full-window + `72%` rectangular loupe cover viewer.
