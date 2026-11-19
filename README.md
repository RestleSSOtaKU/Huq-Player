<3 YES, I VIBE CODED IT ^_^

# Huq — Anechoic Engine

A zero-echo, real-time **HRTF (Head-Related Transfer Function)** binaural desktop audio player powered by a native **Rust** DSP backend (`hrtf_core`) and a **Python (PySide6)** interface.

## Overview

- **Anechoic Engine (`hrtf_core`):** Rust-based 4-channel partitioned FFT convolution and biquad shelving.
- **Python Desktop UI (`player_ui`):** Features fast library scanning, playlist management, SOFA parsing, and real-time spectrum plotting.
- **Adaptive Hardware Performance:** Includes hardware benchmarking and a `CPU: Full Quality / Low-End` toggle.
- **Format Support:** Decodes FLAC, WAV, MP3, M4A, OGG, and Opus via FFmpeg.

## Prebuilt Releases

For prebuilt Windows (`Huq-Windows.zip`) and macOS Apple Silicon (`Huq-macOS-AppleSilicon.dmg`) binaries, visit the project repository.
