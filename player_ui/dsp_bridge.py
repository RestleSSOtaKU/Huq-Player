import ctypes
import os
import sys
import numpy as np

def _default_lib_path():
    if getattr(sys, "frozen", False):
        bundle_dir = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        if sys.platform == "win32":
            if hasattr(os, "add_dll_directory") and os.path.isdir(bundle_dir):
                os.add_dll_directory(bundle_dir)
            return os.path.join(bundle_dir, "hrtf_core.dll")
        elif sys.platform == "darwin":
            return os.path.join(bundle_dir, "libhrtf_core.dylib")
        return os.path.join(bundle_dir, "libhrtf_core.so")

    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hrtf_core", "target", "release"))
    if sys.platform == "win32":
        return os.path.join(base_dir, "hrtf_core.dll")
    elif sys.platform == "darwin":
        return os.path.join(base_dir, "libhrtf_core.dylib")
    return os.path.join(base_dir, "libhrtf_core.so")


class DspParams(ctypes.Structure):
    _fields_ = [
        ("volume", ctypes.c_float),
        ("bass_db", ctypes.c_float),
        ("treble_db", ctypes.c_float),
        ("tonal_match", ctypes.c_bool),
        ("bypass", ctypes.c_bool),
        ("bit_perfect", ctypes.c_bool),
        ("hard_clip", ctypes.c_bool),
    ]

class RustDspBridge:
    def __init__(self, fs, block_size=4096, fft_size=16384, lib_path=None):
        path = lib_path or _default_lib_path()
        self.lib = ctypes.CDLL(path)
        print(f"[Rust DSP] Loaded native engine: {path}")

        self.lib.hrtf_engine_create.argtypes = [ctypes.c_float, ctypes.c_size_t, ctypes.c_size_t]
        self.lib.hrtf_engine_create.restype = ctypes.c_void_p

        self.lib.hrtf_engine_destroy.argtypes = [ctypes.c_void_p]
        self.lib.hrtf_engine_set_params.argtypes = [ctypes.c_void_p, DspParams]
        self.lib.hrtf_engine_reset.argtypes = [ctypes.c_void_p]

        c64_ptr = np.ctypeslib.ndpointer(dtype=np.complex64, flags="C_CONTIGUOUS")
        self.lib.hrtf_engine_set_filters.argtypes = [
            ctypes.c_void_p, c64_ptr, c64_ptr, c64_ptr, c64_ptr, c64_ptr, c64_ptr, ctypes.c_size_t
        ]

        f32_ptr = np.ctypeslib.ndpointer(dtype=np.float32, flags="C_CONTIGUOUS")
        self.lib.hrtf_engine_process.argtypes = [
            ctypes.c_void_p, f32_ptr, f32_ptr, f32_ptr, ctypes.c_size_t
        ]

        self.fft_size = fft_size
        self.handle = self.lib.hrtf_engine_create(float(fs), block_size, fft_size)

    def set_filters(self, h_ll, h_rl, h_lr, h_rr, h_ll_raw, h_rl_raw):
        ll = np.ascontiguousarray(h_ll, dtype=np.complex64)
        rl = np.ascontiguousarray(h_rl, dtype=np.complex64)
        lr = np.ascontiguousarray(h_lr, dtype=np.complex64)
        rr = np.ascontiguousarray(h_rr, dtype=np.complex64)
        ll_r = np.ascontiguousarray(h_ll_raw, dtype=np.complex64)
        rl_r = np.ascontiguousarray(h_rl_raw, dtype=np.complex64)
        self.lib.hrtf_engine_set_filters(self.handle, ll, rl, lr, rr, ll_r, rl_r, len(ll))

    def set_params(self, volume=0.49, bass_db=0.0, treble_db=0.0,
        tonal_match=True, bypass=False, bit_perfect=False, hard_clip=True):
        p = DspParams(volume, bass_db, treble_db, tonal_match, bypass, bit_perfect, hard_clip)
        self.lib.hrtf_engine_set_params(self.handle, p)

    def process(self, in_block_f32, out_block_f32, hrir_l_viz_f32):
        in_c = np.ascontiguousarray(in_block_f32, dtype=np.float32)
        frames = in_c.shape[0] if in_c.ndim == 2 else in_c.size // 2
        self.lib.hrtf_engine_process(self.handle, in_c, out_block_f32, hrir_l_viz_f32, frames)

    def reset(self):
        self.lib.hrtf_engine_reset(self.handle)

    def __del__(self):
        if getattr(self, "handle", None):
            self.lib.hrtf_engine_destroy(self.handle)
