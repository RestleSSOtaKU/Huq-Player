# Project Architecture
- `hrtf_core/`: Cross-platform Rust DSP engine (`cdylib`). Build with `cargo build --release`. Keep audio callbacks zero-allocation and real-time safe.
- `player_ui/`: Python PySide6 UI + `pyfar` SOFA loader + `pyqtgraph` spectrum visualizer. Calls `hrtf_core` via `dsp_bridge.py`.
- Never rewrite the UI or SOFA parser in Rust. Modify one module at a time.
