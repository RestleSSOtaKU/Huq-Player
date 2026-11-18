import sys
import os
import subprocess
import json
import time
import random
import numpy as np
import sounddevice as sd
import soundfile as sf
from scipy.signal import fftconvolve, welch, firwin2, resample_poly, sosfilt, butter
from scipy.ndimage import uniform_filter1d
import pyfar as pf
import pyqtgraph as pg
import mutagen
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                               QHBoxLayout, QPushButton, QSlider, QLabel, 
                               QFileDialog, QCheckBox, QSpinBox, QMessageBox,
                               QComboBox, QListWidget, QListWidgetItem, QSplitter, QLineEdit)
from PySide6.QtCore import Qt, QTimer, Signal, QObject, QThread, QSize
from PySide6.QtGui import QPixmap, QIcon
from dsp_bridge import RustDspBridge

SUBPROCESS_FLAGS = {"creationflags": 0x08000000} if sys.platform == "win32" else {}

if getattr(sys, 'frozen', False):
    class DummyWriter:
        def write(self, *args, **kwargs): pass
        def flush(self, *args, **kwargs): pass
    sys.stdout = DummyWriter()
    sys.stderr = DummyWriter()

def get_binary_path(bin_name):
    if getattr(sys, 'frozen', False):
        bundle_dir = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
        exe_name = f"{bin_name}.exe" if sys.platform == "win32" else bin_name
        bundled = os.path.join(bundle_dir, "bin", exe_name)
        if os.path.exists(bundled):
            return bundled
    return bin_name

def get_resource_path(filename):
    if getattr(sys, 'frozen', False):
        bundle_dir = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
        return os.path.join(bundle_dir, filename)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)

class ResetSlider(QSlider):
    def __init__(self, default_value, orientation, parent=None):
        super().__init__(orientation, parent)
        self.default_value = default_value

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.setValue(self.default_value)
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

class ClickSeekSlider(QSlider):
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.width() > 0:
            ratio = max(0.0, min(1.0, event.position().x() / float(self.width())))
            val = int(round(self.minimum() + ratio * (self.maximum() - self.minimum())))
            self.setValue(val)
            self.sliderMoved.emit(val)
            event.accept()
        super().mousePressEvent(event)

class DeviceComboBox(QComboBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.refresh_callback = None

    def showPopup(self):
        if self.refresh_callback:
            self.refresh_callback()
        super().showPopup()

def _low_shelf_sos(fc, gain_db, Q, fs):
    A = 10 ** (gain_db / 40.0)
    w0 = 2 * np.pi * fc / fs
    cw, sw = np.cos(w0), np.sin(w0)
    alpha = sw / (2 * Q)
    sqA = np.sqrt(A)
    b0 =  A * ((A + 1) - (A - 1) * cw + 2 * sqA * alpha)
    b1 =  2 * A * ((A - 1) - (A + 1) * cw)
    b2 =  A * ((A + 1) - (A - 1) * cw - 2 * sqA * alpha)
    a0 =       (A + 1) + (A - 1) * cw + 2 * sqA * alpha
    a1 = -2 *  ((A - 1) + (A + 1) * cw)
    a2 =       (A + 1) + (A - 1) * cw - 2 * sqA * alpha
    return np.array([[b0/a0, b1/a0, b2/a0, 1.0, a1/a0, a2/a0]])

def _high_shelf_sos(fc, gain_db, Q, fs):
    A = 10 ** (gain_db / 40.0)
    w0 = 2 * np.pi * fc / fs
    cw, sw = np.cos(w0), np.sin(w0)
    alpha = sw / (2 * Q)
    sqA = np.sqrt(A)
    b0 =  A * ((A + 1) + (A - 1) * cw + 2 * sqA * alpha)
    b1 = -2 * A * ((A - 1) + (A + 1) * cw)
    b2 =  A * ((A + 1) + (A - 1) * cw - 2 * sqA * alpha)
    a0 =       (A + 1) - (A - 1) * cw + 2 * sqA * alpha
    a1 =  2 *  ((A - 1) - (A + 1) * cw)
    a2 =       (A + 1) - (A - 1) * cw - 2 * sqA * alpha
    return np.array([[b0/a0, b1/a0, b2/a0, 1.0, a1/a0, a2/a0]])

def _extract_cover_bytes(path):
    try:
        audio = mutagen.File(path)
        if not audio:
            return None
        if isinstance(audio, FLAC) and audio.pictures:
            return audio.pictures[0].data
        if isinstance(audio, (MP3, MP4)) and audio.tags:
            if isinstance(audio.tags, ID3):
                for tag in audio.tags.values():
                    if tag.FrameID == 'APIC':
                        return tag.data
            elif 'covr' in audio.tags:
                return bytes(audio.tags['covr'][0])
    except Exception:
        pass
    return None

class ScannerThread(QThread):
    finished_scanning = Signal(list)
    
    def __init__(self, folder):
        super().__init__()
        self.folder = folder

    def run(self):
        tracks = []
        valid_exts = ('.flac', '.wav', '.mp3', '.m4a', '.ogg')
        
        for root, _, files in os.walk(self.folder):
            for file in files:
                if file.lower().endswith(valid_exts):
                    path = os.path.join(root, file)
                    title = file
                    artist = "Unknown Artist"
                    
                    try:
                        audio = mutagen.File(path)
                        if audio:
                            if isinstance(audio, FLAC):
                                title = audio.get('title', [file])[0]
                                artist = audio.get('artist', ["Unknown Artist"])[0]
                            elif isinstance(audio, MP3) or isinstance(audio, MP4):
                                if audio.tags:
                                    if isinstance(audio.tags, ID3):
                                        title = str(audio.tags.get('TIT2', file))
                                        artist = str(audio.tags.get('TPE1', "Unknown Artist"))
                                    else:
                                        title = audio.tags.get('\xa9nam', [file])[0]
                                        artist = audio.tags.get('\xa9ART', ["Unknown Artist"])[0]
                    except Exception:
                        pass
                        
                    tracks.append({
                        'path': path,
                        'title': title,
                        'artist': artist,
                    })
        self.finished_scanning.emit(tracks)

class LoaderThread(QThread):
    finished_loading = Signal(bool, str)
    
    def __init__(self, filepath):
        super().__init__()
        self.filepath = filepath
        self.result_data = None
        self.result_fs = None
        
    def run(self):
        try:
            ffprobe_bin = get_binary_path('ffprobe')
            ffmpeg_bin = get_binary_path('ffmpeg')

            cmd = [ffprobe_bin, '-v', 'quiet', '-print_format', 'json', '-show_streams', self.filepath]
            result = subprocess.run(cmd, capture_output=True, text=True, **SUBPROCESS_FLAGS)
            info = json.loads(result.stdout)
            
            audio_stream = next((s for s in info.get('streams', []) if s.get('codec_type') == 'audio'), None)
            if not audio_stream:
                raise ValueError("No audio stream found in file.")
                
            fs = int(audio_stream.get('sample_rate', 48000))
            
            cmd = [ffmpeg_bin, '-i', self.filepath, '-f', 'f32le', '-ac', '2', '-ar', str(fs), '-']
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **SUBPROCESS_FLAGS)
            raw_data, err = process.communicate()
            
            if process.returncode != 0 and not raw_data:
                self.finished_loading.emit(False, "")
                return

            data = np.frombuffer(raw_data, dtype=np.float32)
            self.result_data = np.ascontiguousarray(data.reshape(-1, 2))
            self.result_fs = fs
            
            self.finished_loading.emit(True, self.filepath)
        except Exception:
            self.finished_loading.emit(False, "")

class ExportThread(QThread):
    finished_export = Signal(bool, str)
    
    def __init__(self, filename, engine):
        super().__init__()
        self.filename = filename
        self.engine = engine

    def run(self):
        try:
            L_in = self.engine.audio_data[:, 0]
            R_in = self.engine.audio_data[:, 1]

            if self.engine.bit_perfect:
                out_data = np.stack([L_in, R_in], axis=1)
                sf.write(self.filename, out_data, self.engine.fs, subtype='PCM_24')
                self.finished_export.emit(True, self.filename)
                return

            if self.engine.dsp_bypass:
                L_out = L_in * self.engine.volume
                R_out = R_in * self.engine.volume
                if not self.engine.float_mode:
                    np.clip(L_out, -1.0, 1.0, out=L_out)
                    np.clip(R_out, -1.0, 1.0, out=R_out)
                
                out_data = np.stack([L_out, R_out], axis=1)
                sf.write(self.filename, out_data, self.engine.fs, subtype='PCM_24')
                self.finished_export.emit(True, self.filename)
                return

            h_LL = np.fft.irfft(self.engine.h_LL_fft, n=self.engine.N)
            h_RL = np.fft.irfft(self.engine.h_RL_fft, n=self.engine.N)
            h_LR = np.fft.irfft(self.engine.h_LR_fft, n=self.engine.N)
            h_RR = np.fft.irfft(self.engine.h_RR_fft, n=self.engine.N)

            L_out = fftconvolve(L_in, h_LL) + fftconvolve(R_in, h_RL)
            R_out = fftconvolve(L_in, h_LR) + fftconvolve(R_in, h_RR)

            if self.engine.bass_gain != 0.0:
                L_out = sosfilt(self.engine.sos_bass, L_out)
                R_out = sosfilt(self.engine.sos_bass, R_out)

            if self.engine.treble_gain != 0.0:
                L_out = sosfilt(self.engine.sos_treble, L_out)
                R_out = sosfilt(self.engine.sos_treble, R_out)

            L_out *= self.engine.volume
            R_out *= self.engine.volume

            if not self.engine.float_mode:
                np.clip(L_out, -1.0, 1.0, out=L_out)
                np.clip(R_out, -1.0, 1.0, out=R_out)

            out_data = np.stack([L_out, R_out], axis=1)
            
            sf.write(self.filename, out_data, self.engine.fs, subtype='PCM_24')
            self.finished_export.emit(True, self.filename)
        except Exception as e:
            print(f"Export error: {e}")
            self.finished_export.emit(False, str(e))

class AudioEngine(QObject):
    playback_stopped = Signal()
    track_finished = Signal()

    def __init__(self):
        super().__init__()
        self.stream = None
        self.audio_data = None
        self.fs = 48000 
        self.current_frame = 0
        self.is_playing = False
        self.manual_stop = False 
        
        self.volume = 0.44 
        
        self.bass_gain = 0.0
        self.treble_gain = 0.0
        
        self.azimuth = 60.0
        self.elevation = 0.0
        self.tonal_match_enabled = True
        self.highs_enabled = True
        self.dsp_bypass = False
        self.bit_perfect = False
        self.float_mode = False 
        
        self.selected_device = None
        self.stream_mode_status = "Tip: Select your DAC directly instead of System Default for native sample-rate switching."
        self.stream_is_exclusive = False
        
        self.sofa_path = ""
        self.sofa_data = None
        self.sofa_coords = None
        self.tonal_filter = None 
        
        self.h_LL_fft = None; self.h_RL_fft = None
        self.h_LR_fft = None; self.h_RR_fft = None
        
        self.h_LL_fft_raw = None; self.h_RL_fft_raw = None
        self.h_LR_fft_raw = None; self.h_RR_fft_raw = None
        
        self.h_LL_fft_norm = None; self.h_RL_fft_norm = None
        self.h_LR_fft_norm = None; self.h_RR_fft_norm = None
        
        self.h_LL_fft_flat = None; self.h_RL_fft_flat = None
        self.h_LR_fft_flat = None; self.h_RR_fft_flat = None
        
        self.bypass_fc = 18000.0
        
        self.block_size = 4096 
        self.N = 16384
        
        self.overlap_L = np.zeros(self.N)
        self.overlap_R = np.zeros(self.N)
        
        self.L_pad = np.zeros(self.N)
        self.R_pad = np.zeros(self.N)
        
        self.viz_ring_size = 16384
        self.viz_ring_in = np.zeros(self.viz_ring_size, dtype=np.float32)
        self.viz_ring_out = np.zeros(self.viz_ring_size, dtype=np.float32)
        self.viz_ring_hrir = np.zeros(self.viz_ring_size, dtype=np.float32)
        self.viz_head = 0
        
        self.correction_f = None
        self.correction_db = None
        
        self.sos_bass = None
        self.zi_bass_L = np.zeros((1, 2))
        self.zi_bass_R = np.zeros((1, 2))

        self.sos_treble = None
        self.zi_treble_L = np.zeros((1, 2))
        self.zi_treble_R = np.zeros((1, 2))
        self.cb_out_buf = np.zeros((self.block_size, 2), dtype=np.float32)
        self.cb_viz_buf = np.zeros(self.block_size, dtype=np.float32)
        self.rust_dsp = None
        self.last_dsp_ms = 0.0
        self.dsp_hist = np.zeros(4096, dtype=np.float32)
        self.dsp_hist_idx = 0
        self.dsp_hist_count = 0
        self._rust_fs = None
        self._rust_fft_size = None
        self._ensure_rust_engine()

    def _ensure_rust_engine(self):
        if self.rust_dsp is None or self._rust_fs != self.fs or self._rust_fft_size != self.N:
            try:
                self.rust_dsp = RustDspBridge(fs=self.fs, block_size=self.block_size, fft_size=self.N)
                self._rust_fs = self.fs
                self._rust_fft_size = self.N
                self._sync_rust_params()
                self._push_rust_filters()
            except Exception as e:
                print(f"Rust DSP init failed: {e}")
                QMessageBox.warning(None, "Rust DSP Warning", f"Could not load Rust DSP engine:\n{e}")

    def _sync_rust_params(self):
        if self.rust_dsp is not None:
            self.rust_dsp.set_params(
                volume=self.volume,
                bass_db=self.bass_gain,
                treble_db=self.treble_gain,
                tonal_match=self.tonal_match_enabled,
                bypass=self.dsp_bypass,
                bit_perfect=self.bit_perfect,
                hard_clip=not self.float_mode,
            )

    def _push_rust_filters(self):
        if self.rust_dsp is not None and self.h_LL_fft is not None and self.h_LL_fft_raw is not None:
            self.rust_dsp.set_filters(
                self.h_LL_fft,
                self.h_RL_fft,
                self.h_LR_fft,
                self.h_RR_fft,
                self.h_LL_fft_raw,
                self.h_RL_fft_raw,
            )

    def update_bass(self, gain_db):
        self.bass_gain = gain_db
        if self.fs:
            self.sos_bass = _low_shelf_sos(100.0, self.bass_gain, 0.707, self.fs)
        self._sync_rust_params()

    def update_treble(self, gain_db):
        self.treble_gain = gain_db
        if self.fs:
            self.sos_treble = _high_shelf_sos(10000.0, self.treble_gain, 0.707, self.fs)
        self._sync_rust_params()

        
    def load_sofa(self, filepath):
        temp_copy = None
        try:
            read_path = filepath
            if sys.platform == "win32" and not filepath.isascii():
                import shutil, tempfile, uuid
                temp_dir = tempfile.gettempdir()
                if not temp_dir.isascii():
                    temp_dir = os.path.join(os.environ.get("PUBLIC", "C:\\Users\\Public"), "hrtf_tmp")
                    os.makedirs(temp_dir, exist_ok=True)
                temp_copy = os.path.join(temp_dir, f"hrtf_{uuid.uuid4().hex}.sofa")
                shutil.copy2(filepath, temp_copy)
                read_path = temp_copy

            self.sofa_data, self.sofa_coords, _ = pf.io.read_sofa(read_path)
            self.sofa_path = filepath
            self._update_hrirs()
        except Exception as e:
            QMessageBox.critical(None, "SOFA Load Error", f"Failed to load SOFA file:\n{str(e)}")
        finally:
            if temp_copy and os.path.exists(temp_copy):
                try:
                    os.remove(temp_copy)
                except Exception:
                    pass

    def _update_hrirs(self):
        if self.sofa_data is None or self.fs is None:
            return
        
        try:
            left_az = self.azimuth
            right_az = -self.azimuth
            
            def get_hrir(az, el):
                q = pf.Coordinates.from_spherical_elevation(np.deg2rad(az), np.deg2rad(el), 1.0)
                idx, _ = self.sofa_coords.find_nearest(q)
                idx = int(np.atleast_1d(idx).ravel()[0])
                raw = self.sofa_data.time
                if raw.ndim == 3 and raw.shape[1] == 2:
                    return raw[idx, 0, :], raw[idx, 1, :]
                elif raw.ndim == 3 and raw.shape[2] == 2:
                    return raw[idx, :, 0], raw[idx, :, 1]
                return None, None

            self.raw_ipsi_L, self.raw_contra_L = get_hrir(left_az, self.elevation)
            self.raw_contra_R, self.raw_ipsi_R = get_hrir(right_az, self.elevation)
            
            sofa_fs = int(self.sofa_data.sampling_rate)
            if sofa_fs != int(self.fs):
                from math import gcd
                g = gcd(sofa_fs, int(self.fs))
                up = int(self.fs) // g
                down = sofa_fs // g
                self.raw_ipsi_L = resample_poly(self.raw_ipsi_L, up, down)
                self.raw_contra_L = resample_poly(self.raw_contra_L, up, down)
                self.raw_contra_R = resample_poly(self.raw_contra_R, up, down)
                self.raw_ipsi_R = resample_poly(self.raw_ipsi_R, up, down)
            
            self._calculate_tonal_match()
            self._update_filters()
        except Exception as e:
            QMessageBox.critical(None, "HRIR Error", f"Failed to process HRIR:\n{str(e)}")

    def _calculate_tonal_match(self):
        if self.audio_data is None or self.raw_ipsi_L is None or self.fs is None:
            return
        
        total_len = len(self.audio_data)
        window_size = int(self.fs * 3)
        
        indices = []
        if total_len > window_size * 3:
            energy = self.audio_data[:, 0]**2 + self.audio_data[:, 1]**2
            smooth_energy = uniform_filter1d(energy, size=window_size, mode='nearest')
            peak_idx = int(np.argmax(smooth_energy))
            
            indices = [
                int(total_len * 0.1),
                max(0, peak_idx - window_size // 2),
                max(0, int(total_len * 0.9) - window_size)
            ]
        else:
            indices = [0]

        P_in_list = []
        P_out_list = []
        f = None

        for i in indices:
            L = self.audio_data[i:i+window_size, 0]
            R = self.audio_data[i:i+window_size, 1]
            
            if len(L) < 1024:
                continue

            L_out = fftconvolve(L, self.raw_ipsi_L) + fftconvolve(R, self.raw_contra_R)
            R_out = fftconvolve(L, self.raw_contra_L) + fftconvolve(R, self.raw_ipsi_R)
            
            n_fft = min(8192, len(L))
            f, P_in_L = welch(L, fs=self.fs, nperseg=n_fft)
            _, P_in_R = welch(R, fs=self.fs, nperseg=n_fft)
            P_in_list.append(0.5 * (P_in_L + P_in_R))
            
            _, P_out_L = welch(L_out, fs=self.fs, nperseg=n_fft)
            _, P_out_R = welch(R_out, fs=self.fs, nperseg=n_fft)
            P_out_list.append(0.5 * (P_out_L + P_out_R))

        if not P_in_list:
            return

        P_in_avg = np.mean(P_in_list, axis=0)
        P_out_avg = np.mean(P_out_list, axis=0)
        
        ratio_db = 10 * np.log10((P_in_avg + 1e-12) / (P_out_avg + 1e-12))
        
        ratio_db = uniform_filter1d(ratio_db, size=15, mode='nearest')
        ratio_db = np.clip(ratio_db, -24.0, 12.0)
        
        hf_mask = f > 4000
        ratio_db[hf_mask] = np.clip(ratio_db[hf_mask], -24.0, 3.0)

        ratio_db[f < 20] = 0.0
        bass_mask = f < 120.0
        ratio_db[bass_mask] = np.clip(ratio_db[bass_mask], -24.0, 0.0)

        nyquist = self.fs / 2.0
        self.bypass_fc = min(18000.0, nyquist * 0.9)
        
        taper = np.ones_like(f)
        idx_fade = (f >= self.bypass_fc) & (f <= nyquist)
        if np.any(idx_fade):
            taper[idx_fade] = (nyquist - f[idx_fade]) / (nyquist - self.bypass_fc)
        taper[f > nyquist] = 0.0
        ratio_db *= taper

        self.correction_f = f
        self.correction_db = ratio_db

        n_taps = 2049
        f_grid = np.linspace(0, 1.0, n_taps // 2 + 1)
        g_grid = np.interp(f_grid * (self.fs / 2), f, ratio_db)
        mag = 10 ** (g_grid / 20.0)
        
        self.tonal_filter = firwin2(n_taps, f_grid, mag, window='blackmanharris')

    def _update_filters(self):
        if getattr(self, 'raw_ipsi_L', None) is None or self.fs is None:
            return
            
        h_LL = self.raw_ipsi_L
        h_RL = self.raw_contra_R
        h_LR = self.raw_contra_L
        h_RR = self.raw_ipsi_R

        max_h_len = max(len(h_LL), len(h_RL), len(h_LR), len(h_RR))
        self.N = 2 ** int(np.ceil(np.log2(self.block_size + max_h_len - 1)))
        
        self.h_LL_fft_raw = np.fft.rfft(h_LL, n=self.N)
        self.h_RL_fft_raw = np.fft.rfft(h_RL, n=self.N)
        self.h_LR_fft_raw = np.fft.rfft(h_LR, n=self.N)
        self.h_RR_fft_raw = np.fft.rfft(h_RR, n=self.N)

        if self.tonal_match_enabled and self.tonal_filter is not None:
            h_LL = fftconvolve(h_LL, self.tonal_filter)
            h_RL = fftconvolve(h_RL, self.tonal_filter)
            h_LR = fftconvolve(h_LR, self.tonal_filter)
            h_RR = fftconvolve(h_RR, self.tonal_filter)

        self.h_LL_fft_norm = np.fft.rfft(h_LL, n=self.N)
        self.h_RL_fft_norm = np.fft.rfft(h_RL, n=self.N)
        self.h_LR_fft_norm = np.fft.rfft(h_LR, n=self.N)
        self.h_RR_fft_norm = np.fft.rfft(h_RR, n=self.N)

        mag_LL = np.abs(self.h_LL_fft_norm) + 1e-12
        mag_RL = np.abs(self.h_RL_fft_norm) + 1e-12
        mag_LR = np.abs(self.h_LR_fft_norm) + 1e-12
        mag_RR = np.abs(self.h_RR_fft_norm) + 1e-12
        
        phase_LL = np.angle(self.h_LL_fft_norm)
        phase_RL = np.angle(self.h_RL_fft_norm)
        phase_LR = np.angle(self.h_LR_fft_norm)
        phase_RR = np.angle(self.h_RR_fft_norm)

        self.h_LL_fft_flat = np.exp(1j * phase_LL)
        self.h_RR_fft_flat = np.exp(1j * phase_RR)
        self.h_RL_fft_flat = (mag_RL / mag_LL) * np.exp(1j * phase_RL)
        self.h_LR_fft_flat = (mag_LR / mag_RR) * np.exp(1j * phase_LR)

        f = np.fft.rfftfreq(self.N, 1/self.fs)
        hf_mask = 0.5 * (1.0 + np.tanh((f - self.bypass_fc) / 500.0))
        lf_mask = 1.0 - hf_mask

        if self.highs_enabled:
            self.h_LL_fft = (self.h_LL_fft_norm * lf_mask) + (self.h_LL_fft_flat * hf_mask)
            self.h_RL_fft = (self.h_RL_fft_norm * lf_mask) + (self.h_RL_fft_flat * hf_mask)
            self.h_LR_fft = (self.h_LR_fft_norm * lf_mask) + (self.h_LR_fft_flat * hf_mask)
            self.h_RR_fft = (self.h_RR_fft_norm * lf_mask) + (self.h_RR_fft_flat * hf_mask)
        else:
            self.h_LL_fft = self.h_LL_fft_norm
            self.h_RL_fft = self.h_RL_fft_norm
            self.h_LR_fft = self.h_LR_fft_norm
            self.h_RR_fft = self.h_RR_fft_norm

        self.overlap_L = np.zeros(self.N)
        self.overlap_R = np.zeros(self.N)
        self.L_pad = np.zeros(self.N)
        self.R_pad = np.zeros(self.N)
        self._ensure_rust_engine()
        self._push_rust_filters()

    def start(self):
        if self.audio_data is None or self.is_playing or self.fs is None:
            return

        self._ensure_rust_engine()
        self._sync_rust_params()
        self.is_playing = True
        self.manual_stop = False
        
        if self.sos_bass is None:
            self.sos_bass = _low_shelf_sos(100.0, self.bass_gain, 0.707, self.fs)
        if self.sos_treble is None:
            self.sos_treble = _high_shelf_sos(10000.0, self.treble_gain, 0.707, self.fs)

        try:
            extra_settings = None
            if sys.platform == "darwin":
                extra_settings = sd.CoreAudioSettings(
                    change_device_parameters=True,
                    fail_if_conversion_required=True
                )
            elif sys.platform == "win32":
                use_wasapi_excl = True
                if self.selected_device is not None:
                    dev_info = sd.query_devices(self.selected_device)
                    api_name = sd.query_hostapis(dev_info['hostapi'])['name']
                    use_wasapi_excl = "WASAPI" in api_name
                if use_wasapi_excl:
                    extra_settings = sd.WasapiSettings(exclusive=True)

            self.stream = sd.OutputStream(
                samplerate=self.fs,
                blocksize=self.block_size,
                channels=2,
                callback=self._audio_callback,
                finished_callback=self._on_stream_finished,
                dtype='float32',
                latency='high',
                device=self.selected_device,
                extra_settings=extra_settings,
                clip_off=True
            )
            self.stream.start()
            if sys.platform == "darwin":
                if self.selected_device is not None:
                    self.stream_is_exclusive = True
                    self.stream_mode_status = f"Direct Hardware Clock ({self.fs / 1000.0:.1f} kHz native — DAC rate locked)"
                else:
                    self.stream_is_exclusive = False
                    self.stream_mode_status = f"System Default ({self.fs / 1000.0:.1f} kHz — select your DAC directly to guarantee native hardware rate)"
            elif sys.platform == "win32" and extra_settings is not None:
                self.stream_is_exclusive = True
                self.stream_mode_status = f"WASAPI Exclusive Mode ({self.fs / 1000.0:.1f} kHz bit-exact — Windows mixer bypassed)"
            else:
                self.stream_is_exclusive = False
                self.stream_mode_status = "Shared Mode (OS mixer active — select a [WASAPI] device for Exclusive)"
        except Exception as e:
            if sys.platform == "win32" and extra_settings is not None:
                try:
                    self.stream = sd.OutputStream(
                        samplerate=self.fs,
                        blocksize=self.block_size,
                        channels=2,
                        callback=self._audio_callback,
                        finished_callback=self._on_stream_finished,
                        dtype='float32',
                        latency='high',
                        device=self.selected_device,
                        clip_off=True
                    )
                    self.stream.start()
                    self.stream_is_exclusive = False
                    self.stream_mode_status = "Shared Mode fallback (DAC busy in another app — OS resampling may occur)"
                    return
                except Exception as e2:
                    print(f"Start fallback error: {e2}")
            print(f"Start error: {e}")
            self.stream_is_exclusive = False
            self.stream_mode_status = f"Stream failed: {e}"
            self.is_playing = False
            self.playback_stopped.emit()
    def _on_stream_finished(self):
        if self.manual_stop:
            return
        self.is_playing = False
        self.playback_stopped.emit()
        self.track_finished.emit()

    def stop(self, fast=False):
        self.manual_stop = True
        if self.stream is not None:
            try:
                if fast:
                    self.stream.abort()
                else:
                    self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        if not fast and self.rust_dsp is not None:
            self.rust_dsp.reset()
        self.is_playing = False

    def seek(self, frame):
        self.current_frame = max(0, min(frame, len(self.audio_data) - 1))
        self.overlap_L.fill(0)
        self.overlap_R.fill(0)
        self.L_pad.fill(0)
        self.R_pad.fill(0)
        self.zi_bass_L.fill(0)
        self.zi_bass_R.fill(0)
        self.zi_treble_L.fill(0)
        self.zi_treble_R.fill(0)
        if self.rust_dsp is not None:
            self.rust_dsp.reset()

    def _audio_callback(self, outdata, frames, time, status):
        if not self.is_playing or self.audio_data is None:
            outdata.fill(0)
            return
        
        end_frame = self.current_frame + frames
        if end_frame > len(self.audio_data):
            chunk = self.audio_data[self.current_frame:]
            outdata[:len(chunk)] = chunk
            outdata[len(chunk):].fill(0)
            self.is_playing = False
            raise sd.CallbackStop
            
        in_block = self.audio_data[self.current_frame:end_frame]
        self.current_frame = end_frame
        
        if self.rust_dsp is not None and frames <= self.block_size:
         out_slice = self.cb_out_buf[:frames]
         viz_slice = self.cb_viz_buf[:frames]
         import time as _time
         t0 = _time.perf_counter()
         self.rust_dsp.process(in_block, out_slice, viz_slice)
         self.last_dsp_ms = (_time.perf_counter() - t0) * 1000.0
         self.dsp_hist[self.dsp_hist_idx] = self.last_dsp_ms
         self.dsp_hist_idx = (self.dsp_hist_idx + 1) & 4095
         if self.dsp_hist_count < 4096:
             self.dsp_hist_count += 1

         np.copyto(outdata, out_slice)
         out_L = out_slice[:, 0]
         hrir_L = viz_slice

        else:
            np.copyto(outdata, in_block)
            out_L = outdata[:, 0]
            hrir_L = outdata[:, 0]

        h = self.viz_head
        b = self.viz_ring_size
        f = frames

        if h + f <= b:
            self.viz_ring_in[h:h+f] = in_block[:, 0]
            self.viz_ring_out[h:h+f] = out_L
            self.viz_ring_hrir[h:h+f] = hrir_L[:f]
            self.viz_head = (h + f) % b
        else:
            wrap = b - h
            self.viz_ring_in[h:] = in_block[:wrap, 0]
            self.viz_ring_in[:f-wrap] = in_block[wrap:, 0]
            
            self.viz_ring_out[h:] = out_L[:wrap]
            self.viz_ring_out[:f-wrap] = out_L[wrap:]
            
            self.viz_ring_hrir[h:] = hrir_L[:wrap]
            self.viz_ring_hrir[:f-wrap] = hrir_L[wrap:]
            
            self.viz_head = f - wrap


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setWindowTitle("HRTF Audio Player")
        self.setMinimumSize(1100, 820)
        
        pg.setConfigOptions(antialias=False, enableExperimental=False)
        
        self.setStyleSheet("""
            QMainWindow { background-color: #1a1a1a; }
            QLabel { color: #e0e0e0; font-size: 13px; font-family: "Helvetica Neue"; }
            QListWidget { background-color: #121212; color: #e0e0e0; border: 1px solid #333; font-family: "Helvetica Neue"; font-size: 13px; }
            QListWidget::item { padding: 8px; border-bottom: 1px solid #222; }
            QListWidget::item:selected { background-color: #007aff; color: white; }
            QLineEdit { background-color: #2c2c2e; color: white; border: 1px solid #3a3a3c; border-radius: 4px; padding: 5px 8px; font-family: "Helvetica Neue"; font-size: 13px; }
            QLineEdit:focus { border: 1px solid #007aff; }
            QComboBox { background-color: #2c2c2e; color: white; border: none; border-radius: 4px; padding: 5px 24px 5px 8px; font-family: "Helvetica Neue"; }
            QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: top right; width: 20px; border: none; }
            QComboBox QAbstractItemView { background-color: #2c2c2e; color: white; selection-background-color: #007aff; }
            QPushButton { 
                background-color: #2c2c2e; color: white; 
                border: none; padding: 8px 16px; border-radius: 6px;
                font-size: 13px; font-weight: 500;
            }
            QPushButton:hover { background-color: #3a3a3c; }
            QPushButton:pressed { background-color: #007aff; }
            QPushButton#playBtn { font-size: 16px; padding: 10px 20px; }
            QPushButton:checked { background-color: #007aff; }
            QPushButton:disabled { background-color: #252525; color: #666; }
            QSlider::groove:horizontal { height: 4px; background: #3a3a3c; border-radius: 2px; }
            QSlider::sub-page:horizontal { background: #007aff; border-radius: 2px; }
            QSlider::handle:horizontal { 
                background: #fff; width: 14px; height: 14px; 
                margin: -5px 0; border-radius: 7px; 
            }
            QSpinBox {
                background-color: #2c2c2e; color: white; border: none;
                border-radius: 4px; padding: 4px; font-size: 13px;
                font-family: "Helvetica Neue"; min-width: 50px;
            }
        """)

        self.smoothed_in = None
        self.smoothed_hrir = None
        self.smoothed_out = None
        self.viz_alpha = 0.5 

        self.library_tracks = []
        self.custom_tracks = []
        self.current_track_index = -1
        self.shuffle_enabled = False
        self._raw_cover_pixmap = None

        self.engine = AudioEngine()
        self.engine.playback_stopped.connect(self.on_playback_stopped)
        self.engine.track_finished.connect(self.play_next_track)
        
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.update_ui_seekbar)

        self.viz_timer = QTimer(self)
        self.viz_timer.setInterval(16) 
        self.viz_timer.timeout.connect(self.update_graph)

        self.debounce_timer = QTimer(self)
        self.debounce_timer.setSingleShot(True)
        self.debounce_timer.setInterval(150) 
        self.debounce_timer.timeout.connect(self.apply_dsp_params)

        self.loader_thread = None
        self.export_thread = None
        self.scanner_thread = None
        self.last_sys_default = None
        
        self.init_ui()
        self.load_default_sofa()
        
        self.sys_device_timer = QTimer(self)
        self.sys_device_timer.setInterval(1000)
        self.sys_device_timer.timeout.connect(self.check_sys_default)
        self.sys_device_timer.start()
        self.load_session_state()

    def _get_macos_default_output_name(self):
        try:
            import ctypes, ctypes.util
            ca = ctypes.CDLL(ctypes.util.find_library("CoreAudio"))
            cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))

            class AudioObjectPropertyAddress(ctypes.Structure):
                _fields_ = [
                    ("mSelector", ctypes.c_uint32),
                    ("mScope", ctypes.c_uint32),
                    ("mElement", ctypes.c_uint32),
                ]

            ca.AudioObjectGetPropertyData.argtypes = [
                ctypes.c_uint32,
                ctypes.POINTER(AudioObjectPropertyAddress),
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.c_void_p,
            ]
            ca.AudioObjectGetPropertyData.restype = ctypes.c_int32

            cf.CFStringGetCString.argtypes = [
                ctypes.c_void_p,
                ctypes.c_char_p,
                ctypes.c_long,
                ctypes.c_uint32,
            ]
            cf.CFStringGetCString.restype = ctypes.c_bool
            cf.CFRelease.argtypes = [ctypes.c_void_p]
            cf.CFRelease.restype = None

            def fourcc(s):
                return (ord(s[0]) << 24) | (ord(s[1]) << 16) | (ord(s[2]) << 8) | ord(s[3])

            addr = AudioObjectPropertyAddress(fourcc("dOut"), fourcc("glob"), 0)
            dev_id = ctypes.c_uint32(0)
            size = ctypes.c_uint32(ctypes.sizeof(dev_id))
            if ca.AudioObjectGetPropertyData(1, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(dev_id)) != 0:
                return None

            addr_name = AudioObjectPropertyAddress(fourcc("lnam"), fourcc("glob"), 0)
            cf_str = ctypes.c_void_p()
            size_str = ctypes.c_uint32(ctypes.sizeof(cf_str))
            if ca.AudioObjectGetPropertyData(dev_id.value, ctypes.byref(addr_name), 0, None, ctypes.byref(size_str), ctypes.byref(cf_str)) != 0:
                return None

            buf = ctypes.create_string_buffer(256)
            ok = cf.CFStringGetCString(cf_str, buf, 256, 0x08000100)
            cf.CFRelease(cf_str)
            return buf.value.decode("utf-8", errors="ignore") if ok else None
        except Exception:
            return None

    def check_sys_default(self):
        def_name = None
        if sys.platform == "darwin":
            def_name = self._get_macos_default_output_name()
        if not def_name:
            try:
                current_def = sd.query_devices(kind='output')
                if not current_def:
                    return
                def_name = current_def['name']
            except Exception:
                return

        if self.last_sys_default is None:
            self.last_sys_default = def_name
            return

        if self.last_sys_default != def_name:
            self.last_sys_default = def_name
            self._switch_to_new_sys_default()

    def _switch_to_new_sys_default(self):
        was_playing = self.engine.is_playing
        use_default = (self.engine.selected_device is None)

        if was_playing and use_default:
            self.engine.stop(fast=True)

        if not self.engine.is_playing:
            try:
                sd._terminate()
                sd._initialize()
            except Exception:
                pass

        self.refresh_devices(initial=True)

        if was_playing and use_default:
            QTimer.singleShot(120, self._resume_after_sys_default_switch)

    def _resume_after_sys_default_switch(self):
        if self.engine.selected_device is None and not self.engine.is_playing:
            self.engine.start()
            if self.engine.is_playing:
                self.btn_play.setText("⏸ Pause")
                if not self.timer.isActive():
                    self.timer.start()
                if not self.viz_timer.isActive():
                    self.viz_timer.start()
            self.lbl_dev_status.setText(self.engine.stream_mode_status)
            if self.engine.stream_is_exclusive:
                self.lbl_dev_status.setStyleSheet("color: #34c759; font-size: 11px; font-weight: bold; margin-bottom: 4px;")
            else:
                self.lbl_dev_status.setStyleSheet("color: #ff9500; font-size: 11px; font-weight: bold; margin-bottom: 4px;")

    def load_default_sofa(self):
        default_path = get_resource_path("default.sofa")
        if os.path.exists(default_path):
            self.lbl_sofa_file.setText("default.sofa (Built-in)")
            self.engine.load_sofa(default_path)
        else:
            self.lbl_sofa_file.setText("No SOFA loaded")

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setSpacing(10)
        main_layout.setContentsMargins(20, 20, 20, 20)

        # --- LEFT SIDE: LIBRARY ---
        left_layout = QVBoxLayout()
        
        lib_top = QHBoxLayout()
        self.btn_scan = QPushButton("Scan Folder")
        self.btn_scan.clicked.connect(self.scan_folder)
        self.lbl_lib_info = QLabel("Library empty")
        lib_top.addWidget(self.btn_scan)
        lib_top.addWidget(self.lbl_lib_info, 1)
        left_layout.addLayout(lib_top)

        filter_layout = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search title or artist...")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self.filter_playlist)

        self.combo_sort = QComboBox()
        self.combo_sort.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.combo_sort.setMinimumWidth(118)
        self.combo_sort.addItems([
            "Sort: Custom",
            "Title (A-Z)",
            "Title (Z-A)",
            "Artist (A-Z)",
            "Artist (Z-A)"
        ])
        self.combo_sort.currentIndexChanged.connect(self.sort_playlist)

        filter_layout.setSpacing(6)
        filter_layout.addWidget(self.search_input, 1)
        filter_layout.addWidget(self.combo_sort, 0)
        left_layout.addLayout(filter_layout)

        self.list_widget = QListWidget()
        self.list_widget.itemDoubleClicked.connect(self.play_selected_item)
        self.list_widget.setDragDropMode(QListWidget.InternalMove)
        self.list_widget.setDefaultDropAction(Qt.MoveAction)
        self.list_widget.setSelectionMode(QListWidget.ExtendedSelection)
        self.list_widget.model().rowsMoved.connect(self.on_playlist_reordered)
        left_layout.addWidget(self.list_widget, 1)
        
        main_layout.addLayout(left_layout, stretch=1)

        # --- RIGHT SIDE: PLAYER & DSP ---
        right_layout = QVBoxLayout()
        right_layout.setSpacing(6)
        
        # Top Controls
        file_layout = QHBoxLayout()
        self.btn_load_sofa = QPushButton("Load SOFA")
        self.btn_load_sofa.clicked.connect(self.load_sofa)
        self.lbl_sofa_file = QLabel("No SOFA loaded")
        
        self.btn_export = QPushButton("Export Track")
        self.btn_export.clicked.connect(self.export_track)
        self.btn_export.setEnabled(False)
        
        file_layout.addWidget(self.btn_load_sofa)
        file_layout.addWidget(self.lbl_sofa_file, 1)
        file_layout.addWidget(self.btn_export)
        right_layout.addLayout(file_layout)

        dev_layout = QHBoxLayout()
        dev_layout.addWidget(QLabel("Output Device:"))
        self.combo_device = DeviceComboBox()
        self.combo_device.refresh_callback = self._on_device_popup
        self.refresh_devices(initial=True)
        self.combo_device.currentIndexChanged.connect(self.change_device)
        dev_layout.addWidget(self.combo_device, 1)
        
        self.lbl_sr = QLabel("Stream SR: -")
        self.lbl_sr.setStyleSheet("color: #ff9500; font-weight: bold;")
        dev_layout.addWidget(self.lbl_sr)
        right_layout.addLayout(dev_layout)

        self.lbl_dev_status = QLabel("Tip: Select your DAC directly instead of System Default for native sample-rate switching.")
        self.lbl_dev_status.setWordWrap(True)
        self.lbl_dev_status.setStyleSheet("color: #8e8e93; font-size: 11px; margin-bottom: 4px;")
        right_layout.addWidget(self.lbl_dev_status)

        # Album Art & Track Info
        info_layout = QHBoxLayout()
        self.lbl_cover = QLabel()
        self.lbl_cover.setFixedSize(120, 120)
        self.lbl_cover.setStyleSheet("background-color: #111; border: 1px solid #333;")
        
        track_text_layout = QVBoxLayout()
        self.lbl_track_title = QLabel("No Track Playing")
        self.lbl_track_title.setStyleSheet("font-size: 18px; font-weight: bold; color: white;")
        self.lbl_track_artist = QLabel("Unknown Artist")
        self.lbl_track_artist.setStyleSheet("font-size: 14px; color: #aaa;")
        track_text_layout.addStretch()
        track_text_layout.addWidget(self.lbl_track_title)
        track_text_layout.addWidget(self.lbl_track_artist)
        track_text_layout.addStretch()
        
        info_layout.addWidget(self.lbl_cover)
        info_layout.addLayout(track_text_layout, 1)

        perf_box = QVBoxLayout()
        perf_row = QHBoxLayout()
        self.btn_dsp_perf = QPushButton("DSP Time: Off")
        self.btn_dsp_perf.setCheckable(True)
        self.btn_dsp_perf.clicked.connect(self.toggle_dsp_perf)
        self.lbl_dsp_perf = QLabel("")
        self.lbl_dsp_perf.setStyleSheet("color: #0a84ff; font-weight: bold; font-family: 'Helvetica Neue';")
        self.lbl_dsp_perf.setVisible(False)
        perf_row.addWidget(self.btn_dsp_perf)
        perf_row.addWidget(self.lbl_dsp_perf)
        perf_box.addLayout(perf_row)
        perf_box.addStretch()
        info_layout.addLayout(perf_box)

        right_layout.addLayout(info_layout)

        # Seekbar
        seek_layout = QHBoxLayout()
        seek_layout.setContentsMargins(0, 8, 0, 2)
        self.lbl_time_current = QLabel("00:00")
        self.slider_seek = ClickSeekSlider(Qt.Horizontal)
        self.slider_seek.setRange(0, 1000)
        self.slider_seek.sliderMoved.connect(self.seek_audio)
        self.lbl_time_total = QLabel("00:00")
        
        seek_layout.addWidget(self.lbl_time_current)
        seek_layout.addWidget(self.slider_seek, 1)
        seek_layout.addWidget(self.lbl_time_total)
        right_layout.addLayout(seek_layout)

        # Transport Controls
        btn_layout = QHBoxLayout()
        self.btn_prev = QPushButton("⏮ Prev")
        self.btn_prev.clicked.connect(self.play_prev_track)

        self.btn_play = QPushButton("▶ Play")
        self.btn_play.setObjectName("playBtn")
        self.btn_play.setFixedWidth(120)
        self.btn_play.clicked.connect(self.toggle_play)

        self.btn_next = QPushButton("Next ⏭")
        self.btn_next.clicked.connect(self.play_next_track)

        self.btn_shuffle = QPushButton("Shuffle: Off")
        self.btn_shuffle.setCheckable(True)
        self.btn_shuffle.clicked.connect(self.toggle_shuffle)

        btn_layout.addWidget(self.btn_prev)
        btn_layout.addWidget(self.btn_play)
        btn_layout.addWidget(self.btn_next)
        btn_layout.addWidget(self.btn_shuffle)
        right_layout.addLayout(btn_layout)

        dsp_btn_layout = QHBoxLayout()
        self.btn_bypass = QPushButton("Bypass DSP")
        self.btn_bypass.setCheckable(True)
        self.btn_bypass.clicked.connect(self.toggle_bypass)

        self.btn_bit_perfect = QPushButton("Bit-Perfect: Off")
        self.btn_bit_perfect.setCheckable(True)
        self.btn_bit_perfect.clicked.connect(self.toggle_bit_perfect)

        self.btn_highs = QPushButton("Highs Fix: On")
        self.btn_highs.setCheckable(True)
        self.btn_highs.setChecked(True)
        self.btn_highs.clicked.connect(self.toggle_highs)

        self.btn_float_mode = QPushButton("Hard Clip (Exact)")
        self.btn_float_mode.setCheckable(True)
        self.btn_float_mode.setChecked(False)
        self.btn_float_mode.clicked.connect(self.toggle_float_mode)

        dsp_btn_layout.addWidget(self.btn_bypass)
        dsp_btn_layout.addWidget(self.btn_bit_perfect)
        dsp_btn_layout.addWidget(self.btn_highs)
        dsp_btn_layout.addWidget(self.btn_float_mode)
        right_layout.addLayout(dsp_btn_layout)

        # Peak Meter & FPS
        peak_layout = QHBoxLayout()
        
        peak_layout.addWidget(QLabel("Graph FPS:"))
        self.combo_fps = QComboBox()
        self.combo_fps.addItems(["10 FPS", "30 FPS", "60 FPS", "120 FPS"])
        self.combo_fps.setCurrentIndex(2) 
        self.combo_fps.setFixedWidth(120) 
        self.combo_fps.currentIndexChanged.connect(self.change_fps)
        peak_layout.addWidget(self.combo_fps)

        peak_layout.addWidget(QLabel("Smoothing:"))
        self.combo_smooth = QComboBox()
        self.combo_smooth.addItems(["Off", "Low", "Medium", "High"])
        self.combo_smooth.setCurrentIndex(1) 
        self.combo_smooth.setFixedWidth(120) 
        self.combo_smooth.currentIndexChanged.connect(self.change_smoothing)
        peak_layout.addWidget(self.combo_smooth)
        
        peak_layout.addStretch()
        self.lbl_peak = QLabel("Peak: -inf dB")
        self.lbl_peak.setStyleSheet("color: #34c759; font-weight: bold; font-family: 'Helvetica Neue';")
        peak_layout.addWidget(self.lbl_peak)
        
        right_layout.addLayout(peak_layout)

        # Graph
        self.plot_widget = pg.PlotWidget(title="Real-time Spectrum: Input vs Output")
        self.plot_widget.setMinimumHeight(150)
        self.plot_widget.setMaximumHeight(420)
        self.plot_widget.setBackground('#1a1a1a')
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self.plot_widget.setLogMode(x=True, y=False)
        
        # STATIC Y-AXIS RANGE
        self.plot_widget.setYRange(-100, 5) 
        
        self.plot_widget.setLabel('left', 'Amplitude', units='dB')
        self.plot_widget.setLabel('bottom', 'Frequency', units='Hz')
        self.plot_widget.addLegend(offset=(10, 10))
        
        # STATIC X-AXIS RANGE & TICKS
        f_ticks = [20, 30, 50, 70, 100, 200, 300, 500, 700, 1000, 2000, 3000, 5000, 7000, 10000, 20000, 30000, 40000]
        ticks = []
        for f in f_ticks:
            label = f"{f/1000:g}k" if f >= 1000 else str(f)
            ticks.append((np.log10(f), label))
        self.plot_widget.getAxis('bottom').setTicks([ticks])
        
        self.clip_line = pg.InfiniteLine(pos=0, angle=0, pen=pg.mkPen('r', style=Qt.DashLine, width=1))
        self.plot_widget.addItem(self.clip_line)
        
        self.curve_in = self.plot_widget.plot(pen=pg.mkPen('#007aff', width=2), name="Input")
        self.curve_in.setDownsampling(auto=True, method='subsample')
        self.curve_in.setClipToView(True)
        
        self.curve_hrir = self.plot_widget.plot(pen=pg.mkPen('#ff2d55', width=2), name="HRIR Only (Raw)")
        self.curve_hrir.setDownsampling(auto=True, method='subsample')
        self.curve_hrir.setClipToView(True)
        
        self.curve_out = self.plot_widget.plot(pen=pg.mkPen('#34c759', width=2), name="Processed Output")
        self.curve_out.setDownsampling(auto=True, method='subsample')
        self.curve_out.setClipToView(True)
        
        self.curve_corr = self.plot_widget.plot(pen=pg.mkPen('#ff9500', width=2, style=Qt.DashLine), name="Correction Curve")
        self.curve_corr.setDownsampling(auto=True, method='subsample')
        self.curve_corr.setClipToView(True)
        
        right_layout.addWidget(self.plot_widget, stretch=1)

        # DSP Sliders (Collapsible)
        dsp_header_layout = QHBoxLayout()
        self.btn_toggle_dsp_panel = QPushButton("DSP Controls ▼")
        self.btn_toggle_dsp_panel.setCheckable(True)
        self.btn_toggle_dsp_panel.setChecked(False)
        self.btn_toggle_dsp_panel.clicked.connect(self.toggle_dsp_panel)
        dsp_header_layout.addWidget(self.btn_toggle_dsp_panel)
        dsp_header_layout.addSpacing(20)
        dsp_header_layout.addWidget(QLabel("Volume:"))
        self.slider_vol = ResetSlider(44, Qt.Horizontal)
        self.slider_vol.setRange(0, 100)
        self.slider_vol.setValue(int(round(self.engine.volume * 100)))
        self.slider_vol.valueChanged.connect(self.update_volume)

        self.lbl_vol = QLabel(f"{int(round(self.engine.volume * 100))} %")
        self.lbl_vol.setFixedWidth(45)
        self.lbl_vol.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        dsp_header_layout.addWidget(self.slider_vol, 1)
        dsp_header_layout.addWidget(self.lbl_vol)
        right_layout.addLayout(dsp_header_layout)

        self.dsp_panel = QWidget()
        from PySide6.QtWidgets import QSizePolicy
        self.dsp_panel.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.dsp_panel.setVisible(False)
        ctrl_layout = QVBoxLayout(self.dsp_panel)
        ctrl_layout.setContentsMargins(0, 4, 0, 0)
        ctrl_layout.setSpacing(6)
        
        az_layout = QHBoxLayout()
        az_layout.addWidget(QLabel("Azimuth:"))
        self.slider_az = ResetSlider(60, Qt.Horizontal)
        self.slider_az.setRange(-90, 90)
        self.slider_az.setValue(60)
        self.slider_az.valueChanged.connect(self.slider_az_changed)
        
        self.spin_az = QSpinBox()
        self.spin_az.setRange(-90, 90)
        self.spin_az.setValue(60)
        self.spin_az.setSuffix(" °")
        self.spin_az.valueChanged.connect(self.spin_az_changed)
        
        az_layout.addWidget(self.slider_az, 1)
        az_layout.addWidget(self.spin_az)
        ctrl_layout.addLayout(az_layout)

        el_layout = QHBoxLayout()
        el_layout.addWidget(QLabel("Elevation:"))
        self.slider_el = ResetSlider(0, Qt.Horizontal)
        self.slider_el.setRange(-45, 45)
        self.slider_el.setValue(0)
        self.slider_el.valueChanged.connect(self.slider_el_changed)
        
        self.spin_el = QSpinBox()
        self.spin_el.setRange(-45, 45)
        self.spin_el.setValue(0)
        self.spin_el.setSuffix(" °")
        self.spin_el.valueChanged.connect(self.spin_el_changed)
        
        el_layout.addWidget(self.slider_el, 1)
        el_layout.addWidget(self.spin_el)
        ctrl_layout.addLayout(el_layout)
        
        bass_layout = QHBoxLayout()
        bass_layout.addWidget(QLabel("Bass Trim:"))
        self.slider_bass = ResetSlider(0, Qt.Horizontal)
        self.slider_bass.setRange(-60, 60)
        self.slider_bass.setValue(0)
        self.slider_bass.valueChanged.connect(self.slider_bass_changed)
        
        self.lbl_bass = QLabel("0.0 dB")
        self.lbl_bass.setFixedWidth(55)
        self.lbl_bass.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        
        bass_layout.addWidget(self.slider_bass, 1)
        bass_layout.addWidget(self.lbl_bass)
        ctrl_layout.addLayout(bass_layout)

        treble_layout = QHBoxLayout()
        treble_layout.addWidget(QLabel("Treble Trim:"))
        self.slider_treble = ResetSlider(0, Qt.Horizontal)
        self.slider_treble.setRange(-60, 60)
        self.slider_treble.setValue(0)
        self.slider_treble.valueChanged.connect(self.slider_treble_changed)
        
        self.lbl_treble = QLabel("0.0 dB")
        self.lbl_treble.setFixedWidth(55)
        self.lbl_treble.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        
        treble_layout.addWidget(self.slider_treble, 1)
        treble_layout.addWidget(self.lbl_treble)
        ctrl_layout.addLayout(treble_layout)

        self.chk_tonal = QCheckBox("Tonal Match (Neutralize coloration)")
        self.chk_tonal.setChecked(True)
        self.chk_tonal.stateChanged.connect(self.toggle_tonal_match)
        ctrl_layout.addWidget(self.chk_tonal)

        right_layout.addWidget(self.dsp_panel)
        
        main_layout.addLayout(right_layout, stretch=2)

    # --- LIBRARY LOGIC ---
    def scan_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Music Folder")
        if folder:
            self.btn_scan.setEnabled(False)
            self.lbl_lib_info.setText("Scanning...")
            self.scanner_thread = ScannerThread(folder)
            self.scanner_thread.finished_scanning.connect(self.on_scan_finished)
            self.scanner_thread.start()

    def on_scan_finished(self, tracks, auto_play_if_empty=False):
        was_empty = len(self.library_tracks) == 0
        existing_paths = {t['path'] for t in self.library_tracks}

        added = 0
        for track in tracks:
            if track['path'] not in existing_paths:
                self.library_tracks.append(track)
                self.custom_tracks.append(track)
                existing_paths.add(track['path'])
                item = QListWidgetItem(self._format_track_label(track))
                item.setData(Qt.UserRole, track)
                self.list_widget.addItem(item)
                added += 1

        if added > 0 and hasattr(self, "combo_sort") and self.combo_sort.currentIndex() != 0:
            self.sort_playlist(self.combo_sort.currentIndex())

        self.lbl_lib_info.setText(f"{len(self.library_tracks)} tracks in playlist")
        self.btn_scan.setEnabled(True)
        if hasattr(self, "search_input") and self.search_input.text():
            self.filter_playlist(self.search_input.text())

        if auto_play_if_empty and was_empty and self.library_tracks:
            item = self.list_widget.item(0)
            self.list_widget.setCurrentItem(item)
            self.play_selected_item(item)

        if hasattr(self, 'save_session_state'):
            self.save_session_state()

    def play_selected_item(self, item):
        track = item.data(Qt.UserRole)
        self.current_track_index = self.list_widget.row(item)
        
        self.lbl_track_title.setText(track['title'])
        self.lbl_track_artist.setText(track['artist'])
        
        cover_bytes = _extract_cover_bytes(track['path'])
        if cover_bytes:
            pixmap = QPixmap()
            pixmap.loadFromData(cover_bytes)
            self._raw_cover_pixmap = pixmap.scaled(440, 440, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self._last_cover_render_key = None
            self.update_cover_art_size()
        else:
            self._raw_cover_pixmap = None
            self.lbl_cover.clear()
            self.lbl_cover.setText("No Cover")
            self.lbl_cover.setAlignment(Qt.AlignCenter)
            
        self.load_audio_file(track['path'])

    def toggle_shuffle(self, state):
        self.shuffle_enabled = bool(state)
        self.btn_shuffle.setText("Shuffle: On" if self.shuffle_enabled else "Shuffle: Off")
        if hasattr(self, 'save_session_state'):
            self.save_session_state()

    def play_next_track(self):
        if not self.library_tracks:
            return
        n = len(self.library_tracks)
        if self.shuffle_enabled and n > 1:
            candidates = [i for i in range(n) if i != self.current_track_index]
            self.current_track_index = random.choice(candidates)
        else:
            self.current_track_index = (self.current_track_index + 1) % n
        item = self.list_widget.item(self.current_track_index)
        self.list_widget.setCurrentItem(item)
        self.play_selected_item(item)

    def play_prev_track(self):
        if not self.library_tracks:
            return
        self.current_track_index = (self.current_track_index - 1) % len(self.library_tracks)
        item = self.list_widget.item(self.current_track_index)
        self.list_widget.setCurrentItem(item)
        self.play_selected_item(item)

    # --- PLAYER LOGIC ---
    def load_audio_file(self, filepath):
        self.engine.stop()
        self.btn_play.setText("▶ Play")
        self.btn_play.setEnabled(False)
        self.btn_export.setEnabled(False)
        self.timer.stop()
        self.viz_timer.stop()
        self.smoothed_in = None
        
        self.slider_seek.setValue(0)
        self.lbl_time_current.setText("00:00")
        self.lbl_time_total.setText("00:00")
        
        # Clean up previous thread if exists
        if self.loader_thread is not None:
            try:
                self.loader_thread.finished_loading.disconnect()
            except Exception:
                pass
        
        self.loader_thread = LoaderThread(filepath)
        self.loader_thread.finished_loading.connect(self.on_audio_loaded)
        self.loader_thread.start()

    def on_audio_loaded(self, success, filepath):
        self.btn_play.setEnabled(True)
        
        if success and self.loader_thread is not None:
            self.engine.audio_data = self.loader_thread.result_data
            self.engine.fs = self.loader_thread.result_fs
            
            sr_khz = self.engine.fs / 1000.0
            self.lbl_sr.setText(f"Stream SR: {sr_khz:.1f} kHz")
            
            self.engine.current_frame = 0
            
            if self.engine.sofa_data is not None:
                self.engine._update_hrirs()
            else:
                self.engine._update_filters()
            
            total_sec = len(self.engine.audio_data) / self.engine.fs
            self.lbl_time_total.setText(self.format_time(total_sec))
            self.btn_export.setEnabled(True)
            
            self.toggle_play()

            resume_sec = getattr(self, "_pending_resume_sec", 0.0)
            if resume_sec > 0.0:
                self._pending_resume_sec = 0.0
                self.toggle_play()  # Pause on startup
                target_frame = int(resume_sec * self.engine.fs)
                self.engine.seek(target_frame)
                self.lbl_time_current.setText(self.format_time(resume_sec))
                progress = (target_frame / len(self.engine.audio_data)) * 1000
                self.slider_seek.setValue(int(progress))

        else:
            self.lbl_track_title.setText("Error loading file")

    def load_sofa(self):
        filepath, _ = QFileDialog.getOpenFileName(self, "Open SOFA", "", "SOFA Files (*.sofa)")
        if filepath:
            self.lbl_sofa_file.setText(os.path.basename(filepath))
            self.engine.load_sofa(filepath)

    def export_track(self):
        if self.engine.audio_data is None:
            QMessageBox.warning(self, "Error", "No audio loaded.")
            return

        default_filename = os.path.expanduser("~/Documents/HRTF_Processed.flac")
        filepath, _ = QFileDialog.getSaveFileName(self, "Export Processed Track", default_filename, "FLAC Files (*.flac);;All Files (*)")
        
        if not filepath:
            return
            
        self.btn_export.setEnabled(False)
        self.btn_export.setText("Exporting...")
        
        self.export_thread = ExportThread(filepath, self.engine)
        self.export_thread.finished_export.connect(self.on_export_finished)
        self.export_thread.start()

    def on_export_finished(self, success, msg):
        self.btn_export.setEnabled(True)
        self.btn_export.setText("Export Track")
        
        if success:
            QMessageBox.information(self, "Export Complete", f"Successfully exported to:\n{msg}")
        else:
            QMessageBox.critical(self, "Export Error", f"Failed to export track:\n{msg}")

    def force_refresh_devices(self):
        was_playing = self.engine.is_playing
        if was_playing:
            self.engine.stop()
            
        try:
            sd._terminate()
            sd._initialize()
        except Exception as e:
            print(f"PortAudio re-init error: {e}")
            
        self.refresh_devices()
        
        if was_playing:
            self.engine.start()

    def _on_device_popup(self):
        if not self.engine.is_playing:
            try:
                sd._terminate()
                sd._initialize()
            except Exception:
                pass
        self.refresh_devices(initial=True)

    def refresh_devices(self, initial=False):
        current_data = self.combo_device.currentData() 
        
        self.combo_device.blockSignals(True)
        try:
            devices = sd.query_devices()
            hostapis = sd.query_hostapis()
            try:
                sys_def = sd.query_devices(kind='output')
                sys_def_name = sys_def['name'] if sys_def else "Unknown"
            except Exception:
                sys_def_name = "Unknown"

            dev_list = []
            for i, d in enumerate(devices):
                if d['max_output_channels'] <= 0:
                    continue
                if sys.platform == "win32":
                    api_name = hostapis[d['hostapi']]['name']
                    if "WASAPI" not in api_name and "ASIO" not in api_name:
                        continue
                    dev_list.append((f"[{api_name.replace('Windows ', '')}] {d['name']}", i))
                else:
                    dev_list.append((d['name'], i))

            new_items = [(f"System Default ({sys_def_name})", None)] + dev_list
            
            current_items = [(self.combo_device.itemText(i), self.combo_device.itemData(i)) for i in range(self.combo_device.count())]
            
            if current_items != new_items or initial:
                self.combo_device.clear()
                for name, idx in new_items:
                    self.combo_device.addItem(name, idx)
                
                found = False
                for i in range(self.combo_device.count()):
                    if self.combo_device.itemData(i) == current_data:
                        self.combo_device.setCurrentIndex(i)
                        found = True
                        break
                
                if not found:
                    self.combo_device.setCurrentIndex(0) 
                    
                self.engine.selected_device = self.combo_device.currentData()
                
        except Exception as e:
            print(f"Error refreshing devices: {e}")
            
        self.combo_device.blockSignals(False)

    def change_device(self):
        self.engine.selected_device = self.combo_device.currentData()
        if self.engine.is_playing:
            self.engine.stop(fast=True)
            self.engine.start()
            if self.engine.is_playing:
                self.btn_play.setText("⏸ Pause")
                if not self.timer.isActive():
                    self.timer.start()
                if not self.viz_timer.isActive():
                    self.viz_timer.start()
            self.lbl_dev_status.setText(self.engine.stream_mode_status)
            if self.engine.stream_is_exclusive:
                self.lbl_dev_status.setStyleSheet("color: #34c759; font-size: 11px; font-weight: bold; margin-bottom: 4px;")
            else:
                self.lbl_dev_status.setStyleSheet("color: #ff9500; font-size: 11px; font-weight: bold; margin-bottom: 4px;")

    def change_fps(self, index):
        fps_map = {0: 100, 1: 33, 2: 16, 3: 8}
        ms = fps_map.get(index, 100)
        self.viz_timer.setInterval(ms)

    def change_smoothing(self, index):
        smooth_map = {0: 0.0, 1: 0.5, 2: 0.75, 3: 0.88}
        self.viz_alpha = smooth_map.get(index, 0.0)
        
    def toggle_dsp_perf(self, state):
        enabled = bool(state)
        self.engine.dsp_hist_idx = 0
        self.engine.dsp_hist_count = 0
        self.btn_dsp_perf.setText("DSP Time: On" if enabled else "DSP Time: Off")
        self.lbl_dsp_perf.setVisible(enabled)
        if not enabled:
            self.lbl_dsp_perf.setText("")

    def toggle_float_mode(self, state):
        self.engine.float_mode = bool(state)
        if state:
            self.btn_float_mode.setText("Float (Smooth)")
        else:
            self.btn_float_mode.setText("Hard Clip (Exact)")
        self.engine._sync_rust_params()

    def toggle_bypass(self, state):
        self.engine.dsp_bypass = bool(state)
        self.btn_bypass.setText("DSP Active" if not state else "DSP Bypassed")
        
        if state and self.engine.bit_perfect:
            self.engine.bit_perfect = False
            self.btn_bit_perfect.setChecked(False)
            self.btn_bit_perfect.setText("Bit-Perfect: Off")
        self.engine._sync_rust_params()

    def toggle_bit_perfect(self, state):
        self.engine.bit_perfect = bool(state)
        if state:
            self.btn_bit_perfect.setText("Bit-Perfect: On")
            self.engine.dsp_bypass = True
            self.btn_bypass.setChecked(True)
            self.btn_bypass.setText("DSP Bypassed")
        else:
            self.btn_bit_perfect.setText("Bit-Perfect: Off")
        self.engine._sync_rust_params()

    def toggle_highs(self, state):
        self.engine.highs_enabled = bool(state)
        if state:
            self.btn_highs.setText("Highs Fix: On")
        else:
            self.btn_highs.setText("Highs Fix: Off")
        self.engine._update_filters()

    def slider_bass_changed(self, value):
        gain_db = value / 10.0
        self.lbl_bass.setText(f"{gain_db:+.1f} dB")
        self.engine.update_bass(gain_db)

    def slider_treble_changed(self, value):
        gain_db = value / 10.0
        self.lbl_treble.setText(f"{gain_db:+.1f} dB")
        self.engine.update_treble(gain_db)

    def slider_az_changed(self, value):
        self.spin_az.blockSignals(True)
        self.spin_az.setValue(value)
        self.spin_az.blockSignals(False)
        self.debounce_timer.start()

    def spin_az_changed(self, value):
        self.slider_az.blockSignals(True)
        self.slider_az.setValue(value)
        self.slider_az.blockSignals(False)
        self.debounce_timer.start()

    def slider_el_changed(self, value):
        self.spin_el.blockSignals(True)
        self.spin_el.setValue(value)
        self.spin_el.blockSignals(False)
        self.debounce_timer.start()

    def spin_el_changed(self, value):
        self.slider_el.blockSignals(True)
        self.slider_el.setValue(value)
        self.slider_el.blockSignals(False)
        self.debounce_timer.start()

    def apply_dsp_params(self):
        az = self.spin_az.value()
        el = self.spin_el.value()
        
        if abs(self.engine.azimuth - az) > 0 or abs(self.engine.elevation - el) > 0:
            self.engine.azimuth = az
            self.engine.elevation = el
            if self.engine.sofa_path:
                self.engine._update_hrirs()

    def update_graph(self):
        try:
            if not self.engine.is_playing or self.engine.audio_data is None:
                return
                
            # Use welch for much smoother, professional-looking graph
            n_fft = 4096
            b = self.engine.viz_ring_size
            h = self.engine.viz_head
            
            if not hasattr(self, 'chunk_in_buf'):
                self.chunk_in_buf = np.zeros(n_fft)
                self.chunk_hrir_buf = np.zeros(n_fft)
                self.chunk_out_buf = np.zeros(n_fft)
                
            if h >= n_fft:
                np.copyto(self.chunk_in_buf, self.engine.viz_ring_in[h-n_fft:h])
                np.copyto(self.chunk_hrir_buf, self.engine.viz_ring_hrir[h-n_fft:h])
                np.copyto(self.chunk_out_buf, self.engine.viz_ring_out[h-n_fft:h])
            else:
                wrap = n_fft - h
                self.chunk_in_buf[:wrap] = self.engine.viz_ring_in[-wrap:]
                self.chunk_in_buf[wrap:] = self.engine.viz_ring_in[:h]
                
                self.chunk_hrir_buf[:wrap] = self.engine.viz_ring_hrir[-wrap:]
                self.chunk_hrir_buf[wrap:] = self.engine.viz_ring_hrir[:h]
                
                self.chunk_out_buf[:wrap] = self.engine.viz_ring_out[-wrap:]
                self.chunk_out_buf[wrap:] = self.engine.viz_ring_out[:h]
            
            peak = np.max(np.abs(self.chunk_out_buf))
            peak_db = 20 * np.log10(peak + 1e-12)
            
            if peak_db > 0.0:
                self.lbl_peak.setText(f"Peak: {peak_db:.1f} dB (CLIPPING!)")
                self.lbl_peak.setStyleSheet("color: #ff3b30; font-weight: bold; font-family: 'Helvetica Neue';")
            else:
                self.lbl_peak.setText(f"Peak: {peak_db:.1f} dB")
                self.lbl_peak.setStyleSheet("color: #34c759; font-weight: bold; font-family: 'Helvetica Neue';")
            
            # Use welch for a smooth frequency response
            f, P_in = welch(self.chunk_in_buf, fs=self.engine.fs, nperseg=n_fft)
            _, P_hrir = welch(self.chunk_hrir_buf, fs=self.engine.fs, nperseg=n_fft)
            _, P_out = welch(self.chunk_out_buf, fs=self.engine.fs, nperseg=n_fft)
            
            P_in_db = 10 * np.log10(P_in + 1e-12)
            P_hrir_db = 10 * np.log10(P_hrir + 1e-12)
            P_out_db = 10 * np.log10(P_out + 1e-12)
            
            # STATIC Y-AXIS (Removed dynamic scaling)
            
            if self.smoothed_in is None or len(self.smoothed_in) != len(P_in_db):
                self.smoothed_in = P_in_db.copy()
                self.smoothed_hrir = P_hrir_db.copy()
                self.smoothed_out = P_out_db.copy()
            else:
                alpha = self.viz_alpha
                if alpha == 0.0:
                    np.copyto(self.smoothed_in, P_in_db)
                    np.copyto(self.smoothed_hrir, P_hrir_db)
                    np.copyto(self.smoothed_out, P_out_db)
                else:
                    beta = 1.0 - alpha
                    self.smoothed_in *= alpha
                    self.smoothed_in += P_in_db * beta
                    
                    self.smoothed_hrir *= alpha
                    self.smoothed_hrir += P_hrir_db * beta
                    
                    self.smoothed_out *= alpha
                    self.smoothed_out += P_out_db * beta

            # Slice from 1 to ignore DC offset
            self.curve_in.setData(f[1:], self.smoothed_in[1:])
            self.curve_hrir.setData(f[1:], self.smoothed_hrir[1:])
            self.curve_out.setData(f[1:], self.smoothed_out[1:])
            
            if self.engine.correction_f is not None:
                self.curve_corr.setData(self.engine.correction_f, self.engine.correction_db)
        except Exception:
            pass

    def toggle_play(self):
        if self.engine.is_playing:
            self.engine.stop()
            self.btn_play.setText("▶ Play")
            self.timer.stop()
            self.viz_timer.stop()
            self.smoothed_in = None
            self.lbl_dev_status.setText("Paused — Select your DAC directly instead of System Default for native sample-rate switching.")
            self.lbl_dev_status.setStyleSheet("color: #8e8e93; font-size: 11px; margin-bottom: 4px;")
        else:
            self.engine.start()
            self.btn_play.setText("⏸ Pause")
            self.timer.start()
            self.viz_timer.start()
            self.lbl_dev_status.setText(self.engine.stream_mode_status)
            if self.engine.stream_is_exclusive:
                self.lbl_dev_status.setStyleSheet("color: #34c759; font-size: 11px; font-weight: bold; margin-bottom: 4px;")
            else:
                self.lbl_dev_status.setStyleSheet("color: #ff9500; font-size: 11px; font-weight: bold; margin-bottom: 4px;")

    def seek_audio(self, value):
        if self.engine.audio_data is not None:
            total_frames = len(self.engine.audio_data)
            target_frame = int((value / 1000.0) * total_frames)
            self.engine.seek(target_frame)
            self.smoothed_in = None
            self.lbl_time_current.setText(self.format_time(target_frame / self.engine.fs))

    def update_ui_seekbar(self):
        try:
            if self.engine.audio_data is not None and self.engine.is_playing:
                self.slider_seek.blockSignals(True)
                progress = (self.engine.current_frame / len(self.engine.audio_data)) * 1000
                self.slider_seek.setValue(int(progress))
                self.slider_seek.blockSignals(False)

                current_sec = self.engine.current_frame / self.engine.fs
                self.lbl_time_current.setText(self.format_time(current_sec))
                if self.btn_dsp_perf.isChecked() and self.engine.dsp_hist_count > 0:
                    budget_ms = (self.engine.block_size / self.engine.fs) * 1000.0
                    samples = self.engine.dsp_hist[:self.engine.dsp_hist_count]
                    med_ms = float(np.median(samples))
                    p99_ms = float(np.percentile(samples, 99))
                    self.lbl_dsp_perf.setText(
                        f"{self.engine.last_dsp_ms:.2f} | med {med_ms:.2f} | p99 {p99_ms:.2f} / {budget_ms:.1f} ms"
                    )
        except Exception:
            pass

    def format_time(self, seconds):
        m, s = divmod(int(seconds), 60)
        return f"{m:02d}:{s:02d}"

    def on_playback_stopped(self):
        self.btn_play.setText("▶ Play")
        self.timer.stop()
        self.viz_timer.stop()
        self.smoothed_in = None
        self.lbl_dev_status.setText("Stopped — Select your DAC directly instead of System Default for native sample-rate switching.")
        self.lbl_dev_status.setStyleSheet("color: #8e8e93; font-size: 11px; margin-bottom: 4px;")

    def toggle_dsp_panel(self, checked):
        visible = bool(checked)
        self.dsp_panel.setVisible(visible)
        self.btn_toggle_dsp_panel.setText("DSP Controls ▲" if visible else "DSP Controls ▼")
        if hasattr(self, "lbl_cover"):
            self.update_cover_art_size()
        if hasattr(self, 'save_session_state'):
            self.save_session_state()

    def toggle_tonal_match(self, state):
        self.engine.tonal_match_enabled = bool(state)
        self.engine._sync_rust_params()
        if self.engine.audio_data is not None:
            self.engine._update_filters()

    def update_volume(self, val):
        self.engine.volume = val / 100.0
        self.lbl_vol.setText(f"{val} %")
        self.engine._sync_rust_params()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if not urls:
            return

        valid_exts = ('.flac', '.wav', '.mp3', '.m4a', '.ogg', '.opus')
        dropped_files = []
        dropped_folder = None
        dropped_sofa = None

        for url in urls:
            path = url.toLocalFile()
            if not path or not os.path.exists(path):
                continue
            if os.path.isdir(path):
                dropped_folder = path
            elif path.lower().endswith('.sofa'):
                dropped_sofa = path
            elif path.lower().endswith(valid_exts):
                dropped_files.append(path)

        if dropped_sofa:
            self.lbl_sofa_file.setText(os.path.basename(dropped_sofa))
            self.engine.load_sofa(dropped_sofa)

        if dropped_folder:
            self.btn_scan.setEnabled(False)
            self.lbl_lib_info.setText("Scanning...")
            self.scanner_thread = ScannerThread(dropped_folder)
            self.scanner_thread.finished_scanning.connect(self.on_dropped_folder_scanned)
            self.scanner_thread.start()
            event.acceptProposedAction()
            return

        if dropped_files:
            self.load_dropped_tracks(dropped_files)
            event.acceptProposedAction()

    def on_dropped_folder_scanned(self, tracks):
        self.on_scan_finished(tracks, auto_play_if_empty=True)

    def load_dropped_tracks(self, file_paths):
        tracks = []
        for path in sorted(file_paths):
            file = os.path.basename(path)
            title = file
            artist = "Unknown Artist"
            try:
                audio = mutagen.File(path)
                if audio:
                    if isinstance(audio, FLAC):
                        title = audio.get('title', [file])[0]
                        artist = audio.get('artist', ["Unknown Artist"])[0]
                    elif isinstance(audio, (MP3, MP4)) and audio.tags:
                        if isinstance(audio.tags, ID3):
                            title = str(audio.tags.get('TIT2', file))
                            artist = str(audio.tags.get('TPE1', "Unknown Artist"))
                        else:
                            title = audio.tags.get('\xa9nam', [file])[0]
                            artist = audio.tags.get('\xa9ART', ["Unknown Artist"])[0]
            except Exception:
                pass

            tracks.append({
                'path': path,
                'title': title,
                'artist': artist,
            })

        self.on_scan_finished(tracks, auto_play_if_empty=True)
        
    def _state_file_path(self):
        state_dir = os.path.join(os.path.expanduser("~"), ".hrtf_audio_player")
        os.makedirs(state_dir, exist_ok=True)
        return os.path.join(state_dir, "session.json")

    def filter_playlist(self, text):
        query = text.strip().lower()
        visible_count = 0
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            track = item.data(Qt.UserRole) or {}
            haystack = f"{track.get('title', '')} {track.get('artist', '')}".lower()
            match = (query in haystack)
            item.setHidden(not match)
            if match:
                visible_count += 1

        if query:
            self.list_widget.setDragDropMode(QListWidget.NoDragDrop)
            self.lbl_lib_info.setText(f"Showing {visible_count} of {len(self.library_tracks)} tracks")
        else:
            self.list_widget.setDragDropMode(QListWidget.InternalMove)
            self.lbl_lib_info.setText(
                f"{len(self.library_tracks)} tracks in playlist" if self.library_tracks else "Library empty"
            )

    def sort_playlist(self, index):
        if not self.library_tracks:
            return

        current_path = None
        if 0 <= self.current_track_index < len(self.library_tracks):
            current_path = self.library_tracks[self.current_track_index]['path']

        if index == 0:
            self.library_tracks = list(self.custom_tracks)
        elif index == 1:
            self.library_tracks.sort(key=lambda t: (t.get('title', '').lower(), t.get('artist', '').lower()))
        elif index == 2:
            self.library_tracks.sort(key=lambda t: (t.get('title', '').lower(), t.get('artist', '').lower()), reverse=True)
        elif index == 3:
            self.library_tracks.sort(key=lambda t: (t.get('artist', '').lower(), t.get('title', '').lower()))
        elif index == 4:
            self.library_tracks.sort(key=lambda t: (t.get('artist', '').lower(), t.get('title', '').lower()), reverse=True)

        self._rebuild_playlist_widget(current_path)
        self.save_session_state()

    def _format_track_label(self, track):
        if hasattr(self, "combo_sort") and self.combo_sort.currentIndex() in (3, 4):
            return f"{track['artist']} - {track['title']}"
        return f"{track['title']} - {track['artist']}"

    def _refresh_playlist_labels(self):
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            track = item.data(Qt.UserRole)
            if track:
                item.setText(self._format_track_label(track))

    def _rebuild_playlist_widget(self, current_path=None):
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        self.current_track_index = -1
        for i, track in enumerate(self.library_tracks):
            item = QListWidgetItem(self._format_track_label(track))
            item.setData(Qt.UserRole, track)
            self.list_widget.addItem(item)
            if current_path and track['path'] == current_path:
                self.current_track_index = i
                self.list_widget.setCurrentItem(item)
        self.list_widget.blockSignals(False)
        if hasattr(self, "search_input"):
            self.filter_playlist(self.search_input.text())

    def on_playlist_reordered(self, *args):
        current_path = None
        if 0 <= self.current_track_index < len(self.library_tracks):
            current_path = self.library_tracks[self.current_track_index]['path']

        new_tracks = []
        for i in range(self.list_widget.count()):
            track = self.list_widget.item(i).data(Qt.UserRole)
            if track:
                new_tracks.append(track)
        self.library_tracks = new_tracks
        self.custom_tracks = list(new_tracks)

        if current_path:
            for i, t in enumerate(self.library_tracks):
                if t['path'] == current_path:
                    self.current_track_index = i
                    break
        if hasattr(self, "combo_sort") and self.combo_sort.currentIndex() != 0:
            self.combo_sort.blockSignals(True)
            self.combo_sort.setCurrentIndex(0)
            self.combo_sort.blockSignals(False)
            self._refresh_playlist_labels()
        self.save_session_state()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace) and self.list_widget.hasFocus():
            self.remove_selected_tracks()
            event.accept()
            return
        super().keyPressEvent(event)

    def remove_selected_tracks(self):
        selected_rows = sorted([self.list_widget.row(item) for item in self.list_widget.selectedItems()], reverse=True)
        if not selected_rows:
            return

        current_path = None
        if 0 <= self.current_track_index < len(self.library_tracks):
            current_path = self.library_tracks[self.current_track_index]['path']

        playing_removed = self.current_track_index in selected_rows

        removed_paths = set()
        for row in selected_rows:
            self.list_widget.takeItem(row)
            if 0 <= row < len(self.library_tracks):
                removed = self.library_tracks.pop(row)
                removed_paths.add(removed['path'])
        if removed_paths:
            self.custom_tracks = [t for t in self.custom_tracks if t['path'] not in removed_paths]

        self.lbl_lib_info.setText(f"{len(self.library_tracks)} tracks" if self.library_tracks else "Library empty")

        if playing_removed:
            self.engine.stop()
            self.current_track_index = -1
            self.lbl_track_title.setText("No Track Playing")
            self.lbl_track_artist.setText("Unknown Artist")
            self._raw_cover_pixmap = None
            self.lbl_cover.clear()
            self.slider_seek.setValue(0)
            self.lbl_time_current.setText("00:00")
            self.lbl_time_total.setText("00:00")
        elif current_path:
            for i, t in enumerate(self.library_tracks):
                if t['path'] == current_path:
                    self.current_track_index = i
                    break

        self.save_session_state()

    def save_session_state(self):
        try:
            pos_sec = 0.0
            if self.engine.audio_data is not None and self.engine.fs:
                pos_sec = self.engine.current_frame / float(self.engine.fs)

            state = {
                "tracks": self.library_tracks,
                "custom_tracks": self.custom_tracks,
                "sort_mode": self.combo_sort.currentIndex() if hasattr(self, "combo_sort") else 0,
                "current_index": self.current_track_index,
                "position_sec": pos_sec,
                "volume": int(round(self.engine.volume * 100)),
                "shuffle": self.shuffle_enabled,
                "dsp_panel_open": self.btn_toggle_dsp_panel.isChecked(),
                "sofa_path": self.engine.sofa_path if (self.engine.sofa_path and os.path.exists(self.engine.sofa_path) and os.path.abspath(self.engine.sofa_path) != os.path.abspath(get_resource_path("default.sofa"))) else "",
            }
            with open(self._state_file_path(), "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            print(f"Failed to save session: {e}")

    def load_session_state(self):
        state_path = self._state_file_path()
        if not os.path.exists(state_path):
            return
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                state = json.load(f)

            vol = state.get("volume")
            if isinstance(vol, int) and 0 <= vol <= 100:
                self.slider_vol.setValue(vol)

            shuf = bool(state.get("shuffle", False))
            self.btn_shuffle.setChecked(shuf)
            self.toggle_shuffle(shuf)

            panel_open = bool(state.get("dsp_panel_open", False))
            self.btn_toggle_dsp_panel.setChecked(panel_open)
            self.toggle_dsp_panel(panel_open)

            sofa = state.get("sofa_path", "")
            if sofa and os.path.exists(sofa) and sofa != self.engine.sofa_path:
                self.lbl_sofa_file.setText(os.path.basename(sofa))
                self.engine.load_sofa(sofa)

            saved_tracks = [t for t in state.get("tracks", []) if os.path.exists(t.get("path", ""))]
            if not saved_tracks:
                return

            saved_custom = [t for t in state.get("custom_tracks", []) if os.path.exists(t.get("path", ""))]
            sort_mode = state.get("sort_mode", 0)
            if hasattr(self, "combo_sort") and isinstance(sort_mode, int) and 0 <= sort_mode < self.combo_sort.count():
                self.combo_sort.blockSignals(True)
                self.combo_sort.setCurrentIndex(sort_mode)
                self.combo_sort.blockSignals(False)

            self.on_scan_finished(saved_tracks)
            if saved_custom:
                self.custom_tracks = saved_custom
            idx = state.get("current_index", -1)
            pos_sec = float(state.get("position_sec", 0.0))

            if 0 <= idx < len(self.library_tracks):
                item = self.list_widget.item(idx)
                self.list_widget.setCurrentItem(item)
                self.current_track_index = idx
                track = self.library_tracks[idx]
                self.lbl_track_title.setText(track['title'])
                self.lbl_track_artist.setText(track['artist'])

                cover_bytes = _extract_cover_bytes(track['path'])
                if cover_bytes:
                    pixmap = QPixmap()
                    pixmap.loadFromData(cover_bytes)
                    self._raw_cover_pixmap = pixmap.scaled(440, 440, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    self._last_cover_render_key = None
                    self.update_cover_art_size()

                self._pending_resume_sec = pos_sec
                self.load_audio_file(track['path'])
        except Exception as e:
            print(f"Failed to load session: {e}")

    def update_cover_art_size(self):
        h = self.height()
        panel_h = self.dsp_panel.sizeHint().height() if (hasattr(self, "dsp_panel") and self.dsp_panel.isVisible()) else 0
        free_for_cover = self.height() - panel_h - 535
        size = max(110, min(440, int(free_for_cover)))
        stepped_size = (size // 4) * 4

        if self.lbl_cover.width() != stepped_size:
            self.lbl_cover.setFixedSize(stepped_size, stepped_size)

        raw = getattr(self, "_raw_cover_pixmap", None)
        render_key = (id(raw), stepped_size)
        if getattr(self, "_last_cover_render_key", None) != render_key:
            self._last_cover_render_key = render_key
            if raw is not None and not raw.isNull():
                self.lbl_cover.setPixmap(
                    raw.scaled(stepped_size, stepped_size, Qt.KeepAspectRatio, Qt.FastTransformation)
                )

        scale = max(1.0, min(1.5, h / 820.0))
        title_px = int(round(18 * scale))
        if getattr(self, "_last_title_px", None) == title_px:
            return
        self._last_title_px = title_px

        artist_px = int(round(13 * scale))
        if hasattr(self, "lbl_track_title"):
            f = self.lbl_track_title.font()
            f.setPixelSize(title_px)
            f.setBold(True)
            self.lbl_track_title.setFont(f)
        if hasattr(self, "lbl_track_artist"):
            f = self.lbl_track_artist.font()
            f.setPixelSize(artist_px)
            self.lbl_track_artist.setFont(f)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "lbl_cover"):
            self.update_cover_art_size()

    def closeEvent(self, event):
        self.save_session_state()
        self.engine.stop()
        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
