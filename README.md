 VIBE CODED 🖤

# Huq — Anechoic Engine

A zero-echo, real-time **HRTF (Head-Related Transfer Function)** binaural desktop audio player powered by a native **Rust** DSP backend (`hrtf_core`) and a **Python (PySide6)** interface.

## Overview

- **Anechoic Engine (`hrtf_core`):** Rust-based 4-channel partitioned FFT convolution and biquad shelving.
- **Driver-Free System Audio (`Go Live`):** User-space macOS CoreAudio Process Tap (`macos_tap/huq_tap.m`) with automatic native sample-rate locking and live adaptive `Tonal Match`.
- **Audiophile Safety & Precision:** True-peak `-0.5 dBFS` auto-headroom limiter with live `LIM -X.X dB` gain-reduction readout, `Float (32-bit Raw)` output mode, and RME-style 2.0s Left-Click / Instant Right-Click `Bit-Perfect` unity pass-through.
- **Python Desktop UI (`player_ui`):** Fast library scanning, `Open File`, drag-and-drop, global keyboard shortcuts, hold-to-cycle letter navigation, SOFA parsing, and real-time spectrum plotting.
- **Adaptive Hardware Performance:** Includes hardware benchmarking and a `CPU: Full Quality / Low-End` toggle.
- **Format Support:** Decodes FLAC, WAV, MP3, M4A, OGG, and Opus via FFmpeg.

## Prebuilt Releases

For prebuilt Windows (`Huq-Windows.zip`) and macOS Apple Silicon (`Huq-macOS-AppleSilicon.dmg`) binaries, visit the project repository Releases page.

## Status & Platform Focus

- **Desktop (Windows & macOS Apple Silicon):** Active primary targets with full feature parity and prebuilt release binaries.
- **Android (`android_app` / `android_core`):** Early experimental prototype — currently paused and not yet in a usable state for daily listening.
