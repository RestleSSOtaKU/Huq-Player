<3 YES, I VIBE CODED IT ^_^

# HRTF Audio Player

A desktop audio player that applies real-time **HRTF (Head-Related Transfer Function)** convolution using **SOFA** files (`SimpleFreeFieldHRIR`), built with a native **Rust** DSP backend and a **Python (PySide6)** user interface.

---

## Overview

* **Rust DSP Engine (`hrtf_core`):** Performs 4-channel partitioned FFT convolution (`LL`, `RL`, `LR`, `RR`) and biquad shelving filters with zero heap allocations in the audio callback.
* **Python Desktop UI (`player_ui`):** Handles file scanning, metadata/album art loading, SOFA file parsing (`pyfar` / `sofar`), real-time spectrum plotting (`pyqtgraph`), and audio device I/O (`sounddevice`).
* **Native Device Output:**
  * **macOS:** Requests hardware sample-rate matching via CoreAudio (`change_device_parameters=True`, `fail_if_conversion_required=True`).
  * **Windows:** Uses WASAPI Exclusive mode (`WasapiSettings(exclusive=True)`) with automatic fallback to shared mode if the device is busy.
* **Real-Time Timing Stats:** Includes a `DSP Time` toggle to inspect per-block execution time, rolling median (`med`), and 99th percentile (`p99`) latency against the buffer deadline.
* **Format Support & Export:** Decodes FLAC, WAV, MP3, M4A, OGG, and Opus via FFmpeg, and supports offline rendering to FLAC.

---

## Repository Layout

```text
hrtf-player/
├── hrtf_core/                 # Native Rust DSP library (cdylib)
│   ├── Cargo.toml
│   └── src/
│       └── lib.rs             # FFT convolution, biquad filters, C ABI exports
├── player_ui/                 # Python UI and audio stream management
│   ├── main.py                # PySide6 interface, sounddevice callback, SOFA loader
│   ├── dsp_bridge.py          # ctypes FFI wrapper for libhrtf_core / hrtf_core.dll
│   └── default.sofa           # Bundled default SOFA dataset
└── .github/workflows/
    └── build-release.yml      # GitHub Actions CI for Windows (.zip) and macOS (.dmg)
```

---

## Prebuilt Releases (Windows & macOS)

Download the latest builds from the [**Releases**](../../releases) page:

* **Windows (`HRTF_Spatial_Player-Windows.zip`):** Extract the full archive and run `HRTF_Spatial_Player.exe`.
* **macOS Apple Silicon (`HRTF_Spatial_Player-macOS-AppleSilicon.dmg`):** Open the `.dmg`, drag the app to **Applications**, and use **Right-Click → Open** on first launch (ad-hoc signed).

---

## Building from Source

### Requirements
* **Rust** (`cargo`, `rustc`)
* **Python 3.10+**
* **FFmpeg & FFprobe** (in `PATH` or inside `player_ui/bin/`)

### 1. Compile the Rust Library
```bash
cd hrtf_core
cargo build --release
cd ..
```

### 2. Install Python Dependencies
```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install numpy scipy sounddevice soundfile pyfar pyqtgraph mutagen PySide6
```

### 3. Run the Application
```bash
python player_ui/main.py
```

---

## Controls

* **Load SOFA:** Load a custom `.sofa` (`SimpleFreeFieldHRIR`) file.
* **DSP Active / Bypass:** Switch between processed output and dry input.
* **Bit-Perfect:** Bypasses DSP, volume scaling, and clipping.
* **DSP Time:** Shows live block processing time (`Current | med | p99 / Budget ms`).
* **Double-Click Sliders:** Resets **Azimuth**, **Elevation**, **Bass**, **Treble**, or **Volume** to default.