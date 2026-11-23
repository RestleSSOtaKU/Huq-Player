import sys
import os
import subprocess
import json
import time
import random
import numpy as np
import colorsys
import sounddevice as sd
import pyqtgraph as pg
try:
    import pyqt_liquidglass as glass
except ImportError:
    glass = None

_pf = None
_resample_poly = None
_mutagen_mods = None

def _get_pyfar():
    global _pf
    if _pf is None:
        import pyfar as pf
        _pf = pf
    return _pf

def _get_resample_poly():
    global _resample_poly
    if _resample_poly is None:
        from scipy.signal import resample_poly
        _resample_poly = resample_poly
    return _resample_poly

def _get_mutagen():
    global _mutagen_mods
    if _mutagen_mods is None:
        import mutagen
        from mutagen.flac import FLAC
        from mutagen.id3 import ID3
        from mutagen.mp3 import MP3
        from mutagen.mp4 import MP4
        _mutagen_mods = (mutagen, FLAC, ID3, MP3, MP4)
    return _mutagen_mods

def _fast_fftconvolve(a, b):
    n = len(a) + len(b) - 1
    n_fft = 1 << (n - 1).bit_length()
    return np.fft.irfft(np.fft.rfft(a, n=n_fft) * np.fft.rfft(b, n=n_fft), n=n_fft)[:n]

def _fast_welch(x, fs, nperseg):
    x = np.asarray(x, dtype=np.float64)
    nperseg = min(int(nperseg), len(x))
    if nperseg < 16:
        return np.fft.rfftfreq(16, 1.0 / fs), np.zeros(9)
    step = max(1, nperseg // 2)
    win = np.hanning(nperseg)
    scale = 1.0 / (fs * np.sum(win * win))
    n_segs = max(1, (len(x) - nperseg) // step + 1)
    acc = np.zeros(nperseg // 2 + 1, dtype=np.float64)
    for idx in range(n_segs):
        seg = x[idx * step : idx * step + nperseg]
        if len(seg) < nperseg:
            break
        seg = (seg - np.mean(seg)) * win
        acc += np.abs(np.fft.rfft(seg)) ** 2
    acc = (acc / n_segs) * scale
    if len(acc) > 2:
        acc[1:-1] *= 2.0
    elif len(acc) == 2:
        acc *= 2.0
    return np.fft.rfftfreq(nperseg, 1.0 / fs), acc

def _fast_uniform_filter1d(arr, size):
    if size <= 1 or len(arr) == 0:
        return arr.copy()
    pad_l = size // 2
    padded = np.pad(arr, (pad_l, size - 1 - pad_l), mode='edge')
    csum = np.empty(len(padded) + 1, dtype=np.float64)
    csum[0] = 0.0
    np.cumsum(padded, dtype=np.float64, out=csum[1:])
    return (csum[size:] - csum[:-size]) / float(size)

def _fast_firwin2(numtaps, freq, gain):
    n_fft = 1 << max(12, int(np.ceil(np.log2(numtaps * 4))))
    f_dense = np.linspace(0.0, 1.0, n_fft // 2 + 1)
    mag_dense = np.interp(f_dense, freq, gain)
    k = np.arange(len(f_dense))
    phase = np.exp(-1j * np.pi * k * (((numtaps - 1) / 2.0) / (n_fft / 2.0)))
    return np.fft.irfft(mag_dense * phase, n=n_fft)[:numtaps] * np.blackman(numtaps)

def _fast_resample(x, orig_fs, target_fs):
    if orig_fs == target_fs:
        return x
    new_len = max(1, int(round(len(x) * float(target_fs) / float(orig_fs))))
    return np.interp(np.linspace(0.0, 1.0, new_len, endpoint=False), np.linspace(0.0, 1.0, len(x), endpoint=False), x)

def _fast_sosfilt(sos, x):
    b0, b1, b2, _, a1, a2 = sos
    y = np.empty_like(x, dtype=np.float64)
    z1 = z2 = 0.0
    for i in range(len(x)):
        xi = float(x[i])
        yi = b0 * xi + z1
        z1 = b1 * xi - a1 * yi + z2
        z2 = b2 * xi - a2 * yi
        y[i] = yi
    return y
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                               QHBoxLayout, QPushButton, QSlider, QLabel, 
                               QFileDialog, QCheckBox, QSpinBox, QMessageBox,
                               QComboBox, QListWidget, QListWidgetItem, QSplitter, QLineEdit, QStyledItemDelegate, QStyle, QStyleOptionViewItem, QAbstractItemView)
from PySide6.QtCore import Qt, QTimer, Signal, QObject, QThread, QSize, QEvent, QVariantAnimation, QEasingCurve
from PySide6.QtGui import QPixmap, QIcon, QColor, QPainter, QPalette
from dsp_bridge import RustDspBridge
from liquid_glass import make_glass_card, sample_ambient_rgb, build_liquid_qss, apply_seamless_titlebar, apply_windows_glass, windows_transparency_enabled, WindowDragFilter


_mac_tap_lib = None
def _get_mac_tap():
    global _mac_tap_lib
    if _mac_tap_lib is None and sys.platform == "darwin":
        import ctypes
        dylib = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "macos_tap", "libhuq_tap.dylib")
        if not os.path.exists(dylib):
            dylib = get_resource_path("libhuq_tap.dylib")
        if os.path.exists(dylib):
            lib = ctypes.CDLL(dylib)
            lib.huq_tap_start.restype = ctypes.c_double
            lib.huq_tap_stop.restype = None
            lib.huq_tap_read.argtypes = [ctypes.POINTER(ctypes.c_float), ctypes.c_uint32]
            lib.huq_tap_read.restype = ctypes.c_uint32
            _mac_tap_lib = lib
    return _mac_tap_lib

SUBPROCESS_FLAGS = {"creationflags": 0x08000000} if sys.platform == "win32" else {}

if getattr(sys, 'frozen', False):
    class DummyWriter:
        def write(self, *args, **kwargs): pass
        def flush(self, *args, **kwargs): pass
    sys.stdout = DummyWriter()
    sys.stderr = DummyWriter()

def get_binary_path(bin_name):
    exe_name = f"{bin_name}.exe" if sys.platform == "win32" else bin_name
    if getattr(sys, 'frozen', False):
        bundle_dir = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
        bundled = os.path.join(bundle_dir, "bin", exe_name)
        if os.path.exists(bundled):
            return bundled
    if sys.platform == "darwin":
        for prefix in ("/opt/homebrew/bin", "/usr/local/bin"):
            cand = os.path.join(prefix, exe_name)
            if os.path.exists(cand):
                return cand
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

class RightClickPushButton(QPushButton):
    rightClicked = Signal()
    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            self.rightClicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)

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
        mutagen, FLAC, ID3, MP3, MP4 = _get_mutagen()
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


class MarqueeLabel(QLabel):
    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setMouseTracking(True)
        from PySide6.QtWidgets import QSizePolicy
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._hovered = False
        self._offset = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._step)

    def setText(self, text):
        super().setText(text)
        self._offset = 0.0
        self.update()

    def enterEvent(self, event):
        self._hovered = True
        self._offset = 0.0
        self._timer.start()
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self._offset = 0.0
        self._timer.stop()
        self.update()
        super().leaveEvent(event)

    def _step(self):
        if not self._hovered:
            self._timer.stop()
            return
        self._offset += 1.5
        self.update()

    def paintEvent(self, event):
        text = self.text()
        if not text:
            return
        painter = QPainter(self)
        painter.setFont(self.font())
        painter.setPen(self.palette().color(QPalette.WindowText))
        rect = self.contentsRect()
        tw = painter.fontMetrics().horizontalAdvance(text)
        if self._hovered and tw > rect.width():
            painter.setClipRect(rect)
            gap = 60
            total = tw + gap
            off = self._offset % total
            x1 = rect.left() - off
            x2 = x1 + total
            painter.drawText(int(x1), rect.top(), int(tw), rect.height(), Qt.AlignVCenter | Qt.AlignLeft, text)
            painter.drawText(int(x2), rect.top(), int(tw), rect.height(), Qt.AlignVCenter | Qt.AlignLeft, text)
        else:
            elided = painter.fontMetrics().elidedText(text, Qt.ElideRight, rect.width())
            painter.drawText(rect, Qt.AlignVCenter | Qt.AlignLeft, elided)
        painter.end()

class MarqueeDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._hover_index = None
        self._offset = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(16) # 60 FPS
        self._timer.timeout.connect(self._scroll_step)

    def sizeHint(self, option, index):
        return QSize(100, 36)

    def set_hover(self, index):
        if self._hover_index != index:
            self._hover_index = index
            self._offset = 0.0
            self._timer.stop()
            if self._hover_index is not None:
                self._timer.start()
            if self.parent():
                self.parent().viewport().update()

    def _scroll_step(self):
        if self._hover_index is None:
            self._timer.stop()
            return
        self._offset += 1.5
        if self.parent():
            self.parent().viewport().update()

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = opt.text
        
        # Empty text so the native painter draws only the background/selection
        opt.text = "" 
        
        widget = opt.widget
        style = widget.style() if widget else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, widget)
        
        if not text:
            return
            
        painter.save()
        text_rect = option.rect.adjusted(8, 0, -8, 0)
        text_width = painter.fontMetrics().horizontalAdvance(text)
        
        is_selected = bool(option.state & QStyle.State_Selected)
        if is_selected:
            accent = getattr(self.parent().window(), 'current_accent', '#0a84ff')
            try:
                h = accent.lstrip('#')
                ar, ag, ab = tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
                brightness = (ar * 299 + ag * 587 + ab * 114) / 1000
                painter.setPen(QColor("#000000") if brightness > 160 else QColor("#ffffff"))
            except Exception:
                painter.setPen(QColor("#ffffff"))
        else:
            is_light = getattr(self.parent().window(), 'current_theme', 'dark') == 'light'
            painter.setPen(QColor("#1c1c1e") if is_light else QColor("#eef2f8"))
            
        try:
            if self._hover_index == index and text_width > text_rect.width():
                painter.setClipRect(text_rect)
                gap = 60 
                total_width = text_width + gap
                offset = self._offset % total_width
                
                x1 = text_rect.left() - offset
                x2 = x1 + total_width
                
                # Use the (x, y, w, h) overload to avoid needing the QRect import entirely
                painter.drawText(int(x1), text_rect.top(), int(text_width), text_rect.height(), Qt.AlignVCenter | Qt.AlignLeft, text)
                painter.drawText(int(x2), text_rect.top(), int(text_width), text_rect.height(), Qt.AlignVCenter | Qt.AlignLeft, text)
            else:
                elided = painter.fontMetrics().elidedText(text, Qt.ElideRight, text_rect.width())
                painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, elided)
        finally:
            painter.restore()

class MarqueeListWidget(QListWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self._delegate = MarqueeDelegate(self)
        self.setItemDelegate(self._delegate)
        
        # Make the list completely transparent
        self.setStyleSheet("QListWidget { background: transparent; border: none; }")
        self.viewport().setAutoFillBackground(False)
        self.viewport().setStyleSheet("background: transparent;")
        
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.verticalScrollBar().valueChanged.connect(self.viewport().update)

    def mouseMoveEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        self._delegate.set_hover(index if index.isValid() else None)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._delegate.set_hover(None)
        super().leaveEvent(event)

class HoverPlotWidget(pg.PlotWidget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.legend = None
        self.viewport().setMouseTracking(True)

    def mouseMoveEvent(self, event):
        if self.legend:
            # Map the mouse position to the scene to see if it's inside the legend's box
            scene_pos = self.mapToScene(event.position().toPoint())
            if self.legend.sceneBoundingRect().contains(scene_pos):
                self.legend.setOpacity(1.0)
            else:
                self.legend.setOpacity(0.1) # Almost transparent
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        if self.legend:
            self.legend.setOpacity(0.1)
        super().leaveEvent(event)

class MetadataRepairThread(QThread):
    finished_repair = Signal()
    def __init__(self, tracks):
        super().__init__()
        self.tracks = tracks
    def run(self):
        mutagen, FLAC, ID3, MP3, MP4 = _get_mutagen()
        for t in self.tracks:
            try:
                audio = mutagen.File(t['path'])
                if audio:
                    if isinstance(audio, FLAC):
                        album = audio.get('album', ["Unknown Album"])[0]
                        t['album'] = album if album else "Unknown Album"
                    elif isinstance(audio, (MP3, MP4)):
                        if audio.tags:
                            if isinstance(audio.tags, ID3):
                                album = str(audio.tags.get('TALB', "Unknown Album"))
                                t['album'] = album if album else "Unknown Album"
                            else:
                                album = audio.tags.get('\xa9alb', ["Unknown Album"])[0]
                                t['album'] = album if album else "Unknown Album"
            except Exception:
                pass
        self.finished_repair.emit()

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
                    album = "Unknown Album"
                    
                    try:
                        mutagen, FLAC, ID3, MP3, MP4 = _get_mutagen()
                        audio = mutagen.File(path)
                        if audio:
                            if isinstance(audio, FLAC):
                                title = audio.get('title', [file])[0]
                                artist = audio.get('artist', ["Unknown Artist"])[0]
                                album = audio.get('album', ["Unknown Album"])[0]
                            elif isinstance(audio, MP3) or isinstance(audio, MP4):
                                if audio.tags:
                                    if isinstance(audio.tags, ID3):
                                        title = str(audio.tags.get('TIT2', file))
                                        artist = str(audio.tags.get('TPE1', "Unknown Artist"))
                                        album = str(audio.tags.get('TALB', "Unknown Album"))
                                    else:
                                        title = audio.tags.get('\xa9nam', [file])[0]
                                        artist = audio.tags.get('\xa9ART', ["Unknown Artist"])[0]
                                        album = audio.tags.get('\xa9alb', ["Unknown Album"])[0]
                    except Exception:
                        pass
                        
                    tracks.append({
                        'path': path,
                        'title': title,
                        'artist': artist,
                        'album': album,
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
            
            cmd = [ffmpeg_bin, '-nostdin', '-v', 'error', '-i', self.filepath, '-f', 'f32le', '-ac', '2', '-ar', str(fs), '-']
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **SUBPROCESS_FLAGS)
            raw_data, err = process.communicate()
            
            if process.returncode != 0 and not raw_data:
                self.finished_loading.emit(False, "")
                return

            data = np.frombuffer(raw_data, dtype=np.float32)
            self.result_data = np.ascontiguousarray(data.reshape(-1, 2))
            self.result_fs = fs
            
            self.finished_loading.emit(True, self.filepath)
        except Exception as e:
            print(f"LoaderThread error: {e}")
            self.finished_loading.emit(False, "")

class ExportThread(QThread):
    finished_export = Signal(bool, str)
    
    def __init__(self, filename, engine):
        super().__init__()
        self.filename = filename
        self.engine = engine

    def run(self):
        try:
            import soundfile as sf
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

            L_out = _fast_fftconvolve(L_in, h_LL) + _fast_fftconvolve(R_in, h_RL)
            R_out = _fast_fftconvolve(L_in, h_LR) + _fast_fftconvolve(R_in, h_RR)

            if self.engine.bass_gain != 0.0:
                L_out = _fast_sosfilt(self.engine.sos_bass, L_out)
                R_out = _fast_sosfilt(self.engine.sos_bass, R_out)

            if self.engine.treble_gain != 0.0:
                L_out = _fast_sosfilt(self.engine.sos_treble, L_out)
                R_out = _fast_sosfilt(self.engine.sos_treble, R_out)

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

import logging
class RustDspLogFilter(logging.Filter):
    def filter(self, record):
        return "Loaded native engine" not in record.getMessage()

logging.getLogger().addFilter(RustDspLogFilter())

class _FastSofaData:
    def __init__(self, time_arr, sampling_rate):
        self.time = time_arr
        self.sampling_rate = int(sampling_rate)

class _FastSofaCoords:
    def __init__(self, cart_xyz):
        self.cart = np.asarray(cart_xyz, dtype=np.float64)

    def find_nearest(self, q_xyz):
        dots = self.cart @ np.asarray(q_xyz, dtype=np.float64)
        return int(np.argmax(dots)), None

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
        self.low_end_mode = False
        
        self.selected_device = None
        self.input_device = None
        self.use_mac_tap = False
        self.auto_trim_gain = 1.0
        self.input_stream = None
        self.live_ring = np.zeros((65536, 2), dtype=np.float32)
        self.live_write_pos = 0
        self.live_read_pos = 0
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
        
        self.viz_ring_size = 32768
        self.viz_ring_in = np.zeros(self.viz_ring_size, dtype=np.float32)
        self.viz_ring_out = np.zeros(self.viz_ring_size, dtype=np.float32)
        self.viz_ring_hrir = np.zeros(self.viz_ring_size, dtype=np.float32)
        self.viz_head = 0
        self.viz_state = (0, self.block_size, 0.0)
        
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
                hard_clip=False,
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

        
    def _sofa_cache_path(self, filepath):
        import hashlib
        st = os.stat(filepath)
        key = f"{os.path.abspath(filepath)}:{st.st_size}:{int(st.st_mtime)}".encode("utf-8", errors="ignore")
        h = hashlib.sha256(key).hexdigest()[:16]
        cache_dir = os.path.join(os.path.expanduser("~"), ".hrtf_audio_player", "sofa_cache")
        os.makedirs(cache_dir, exist_ok=True)
        return os.path.join(cache_dir, f"{h}.npz")

    def load_sofa(self, filepath):
        temp_copy = None
        try:
            cache_file = self._sofa_cache_path(filepath)
            if os.path.exists(cache_file):
                npz = np.load(cache_file)
                self.sofa_data = _FastSofaData(npz["time"], int(npz["fs"]))
                self.sofa_coords = _FastSofaCoords(npz["cart"])
                self.sofa_path = filepath
                self._update_hrirs()
                return

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

            s_data, s_coords, _ = _get_pyfar().io.read_sofa(read_path)
            raw_cart = s_coords.cartesian() if callable(s_coords.cartesian) else s_coords.cartesian
            cart = np.asarray(raw_cart, dtype=np.float64)
            norms = np.linalg.norm(cart, axis=1, keepdims=True)
            cart = cart / np.maximum(norms, 1e-12)
            time_arr = np.asarray(s_data.time, dtype=np.float32)
            sr_val = int(s_data.sampling_rate)
            try:
                np.savez(cache_file, time=time_arr, cart=cart, fs=np.array(sr_val, dtype=np.int64))
            except Exception:
                pass
            self.sofa_data = _FastSofaData(time_arr, sr_val)
            self.sofa_coords = _FastSofaCoords(cart)
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
                if isinstance(self.sofa_coords, _FastSofaCoords):
                    az_r, el_r = np.deg2rad(az), np.deg2rad(el)
                    q = np.array([np.cos(el_r) * np.cos(az_r), np.cos(el_r) * np.sin(az_r), np.sin(el_r)])
                else:
                    q = _get_pyfar().Coordinates.from_spherical_elevation(np.deg2rad(az), np.deg2rad(el), 1.0)
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
                if True:
                    self.raw_ipsi_L = _fast_resample(self.raw_ipsi_L, sofa_fs, int(self.fs))
                    self.raw_contra_L = _fast_resample(self.raw_contra_L, sofa_fs, int(self.fs))
                    self.raw_contra_R = _fast_resample(self.raw_contra_R, sofa_fs, int(self.fs))
                    self.raw_ipsi_R = _fast_resample(self.raw_ipsi_R, sofa_fs, int(self.fs))
                else:
                    from math import gcd
                    rp = _get_resample_poly()
                    g = gcd(sofa_fs, int(self.fs))
                    up, down = int(self.fs) // g, sofa_fs // g
                    self.raw_ipsi_L = rp(self.raw_ipsi_L, up, down)
                    self.raw_contra_L = rp(self.raw_contra_L, up, down)
                    self.raw_contra_R = rp(self.raw_contra_R, up, down)
                    self.raw_ipsi_R = rp(self.raw_ipsi_R, up, down)
            
            self._calculate_tonal_match()
            self._update_filters()
        except Exception as e:
            QMessageBox.critical(None, "HRIR Error", f"Failed to process HRIR:\n{str(e)}")

    def _calculate_tonal_match(self):
        if self.audio_data is None or self.raw_ipsi_L is None or self.fs is None:
            return
        
        total_len = len(self.audio_data)
        window_size = int(self.fs * (1.5 if self.low_end_mode else 3.0))
        
        indices = []
        if self.low_end_mode:
            indices = [int(total_len * 0.25)] if total_len > window_size * 2 else [0]
        elif total_len > window_size * 3:
            energy = self.audio_data[:, 0]**2 + self.audio_data[:, 1]**2
            smooth_energy = _fast_uniform_filter1d(energy, window_size)
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

            L_out = _fast_fftconvolve(L, self.raw_ipsi_L) + _fast_fftconvolve(R, self.raw_contra_R)
            R_out = _fast_fftconvolve(L, self.raw_contra_L) + _fast_fftconvolve(R, self.raw_ipsi_R)
            
            n_fft = min(2048 if self.low_end_mode else 8192, len(L))
            f, P_in_L = _fast_welch(L, fs=self.fs, nperseg=n_fft)
            _, P_in_R = _fast_welch(R, fs=self.fs, nperseg=n_fft)
            P_in_list.append(0.5 * (P_in_L + P_in_R))
            
            _, P_out_L = _fast_welch(L_out, fs=self.fs, nperseg=n_fft)
            _, P_out_R = _fast_welch(R_out, fs=self.fs, nperseg=n_fft)
            P_out_list.append(0.5 * (P_out_L + P_out_R))

        if not P_in_list:
            return

        P_in_avg = np.mean(P_in_list, axis=0)
        P_out_avg = np.mean(P_out_list, axis=0)
        
        ratio_db = 10 * np.log10((P_in_avg + 1e-12) / (P_out_avg + 1e-12))
        
        ratio_db = _fast_uniform_filter1d(ratio_db, 15)
        ratio_db = np.clip(ratio_db, -24.0, 12.0)
        
        hf_mask = f > 4000
        ratio_db[hf_mask] = np.clip(ratio_db[hf_mask], -24.0, 6.0)

        ratio_db[f < 20] = 0.0
        bass_mask = f < 120.0
        ratio_db[bass_mask] = np.clip(ratio_db[bass_mask], -24.0, 0.0)

        nyquist = self.fs / 2.0
        self.bypass_fc = min(15500.0 if self.highs_enabled else 12000.0, nyquist * 0.85)
        end_fc = nyquist if self.highs_enabled else min(15000.0, nyquist * 0.92)
        
        taper = np.ones_like(f)
        idx_fade = (f >= self.bypass_fc) & (f <= end_fc)
        if np.any(idx_fade):
            taper[idx_fade] = (end_fc - f[idx_fade]) / max(1.0, end_fc - self.bypass_fc)
        taper[f > end_fc] = 0.0
        ratio_db *= taper

        self.correction_f = f
        self.correction_db = ratio_db

        n_taps = 2049
        f_grid = np.linspace(0, 1.0, n_taps // 2 + 1)
        g_grid = np.interp(f_grid * (self.fs / 2), f, ratio_db)
        mag = 10 ** (g_grid / 20.0)
        
        self.tonal_filter = _fast_firwin2(n_taps, f_grid, mag)

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
            h_LL = _fast_fftconvolve(h_LL, self.tonal_filter)
            h_RL = _fast_fftconvolve(h_RL, self.tonal_filter)
            h_LR = _fast_fftconvolve(h_LR, self.tonal_filter)
            h_RR = _fast_fftconvolve(h_RR, self.tonal_filter)

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

        f = np.fft.rfftfreq(self.N, 1.0 / self.fs)
        # Anchor spatial ILD & crossfeed ratios at 10-13 kHz (before SOFA HF roll-off)
        anchor_band = (f >= 10000.0) & (f <= 13000.0)
        if np.any(anchor_band):
            ipsi_ref_L = float(np.median(mag_LL[anchor_band]))
            ipsi_ref_R = float(np.median(mag_RR[anchor_band]))
            xf_ratio_RL = float(np.median(mag_RL[anchor_band] / mag_LL[anchor_band]))
            xf_ratio_LR = float(np.median(mag_LR[anchor_band] / mag_RR[anchor_band]))
        else:
            ipsi_ref_L = ipsi_ref_R = 1.0
            xf_ratio_RL = xf_ratio_LR = 0.25

        # Synthesize full-bandwidth HRIR extension with locked spatial ILD, crossfeed, and ITD phase
        h_LL_ext = ipsi_ref_L * np.exp(1j * phase_LL)
        h_RR_ext = ipsi_ref_R * np.exp(1j * phase_RR)
        h_RL_ext = (ipsi_ref_L * xf_ratio_RL) * np.exp(1j * phase_RL)
        h_LR_ext = (ipsi_ref_R * xf_ratio_LR) * np.exp(1j * phase_LR)

        hf_mask = 0.5 * (1.0 + np.tanh((f - 14000.0) / 1500.0))
        lf_mask = 1.0 - hf_mask
        self._hf_eq_f = None
        self._hf_eq_db = None

        if self.highs_enabled:
            self.h_LL_fft = (self.h_LL_fft_norm * lf_mask) + (h_LL_ext * hf_mask)
            self.h_RL_fft = (self.h_RL_fft_norm * lf_mask) + (h_RL_ext * hf_mask)
            self.h_LR_fft = (self.h_LR_fft_norm * lf_mask) + (h_LR_ext * hf_mask)
            self.h_RR_fft = (self.h_RR_fft_norm * lf_mask) + (h_RR_ext * hf_mask)
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

    def _input_callback(self, indata, frames, time_info, status):
        n = len(indata)
        cap = len(self.live_ring)
        w = self.live_write_pos % cap
        if w + n <= cap:
            self.live_ring[w:w+n] = indata[:, :2]
        else:
            part = cap - w
            self.live_ring[w:] = indata[:part, :2]
            self.live_ring[:n-part] = indata[part:, :2]
        self.live_write_pos += n

    def start(self):
        if (self.audio_data is None and self.input_device is None and not self.use_mac_tap) or self.is_playing or self.fs is None:
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
            if self.use_mac_tap:
                tap = _get_mac_tap()
                if tap is None:
                    raise RuntimeError("macOS Process Tap library missing")
                native_sr = tap.huq_tap_start()
                if native_sr <= 0.0:
                    raise RuntimeError("macOS Process Tap unavailable or permission denied")
                self.tap_native_fs = int(round(native_sr))
            elif self.input_device is not None:
                self.live_ring.fill(0)
                self.live_write_pos = 0
                self.live_read_pos = 0
                self.input_stream = sd.InputStream(
                    samplerate=self.fs,
                    blocksize=self.block_size,
                    channels=2,
                    dtype='float32',
                    device=self.input_device,
                    callback=self._input_callback,
                    latency='low',
                )
                self.input_stream.start()
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
        if self.use_mac_tap:
            tap = _get_mac_tap()
            if tap is not None:
                tap.huq_tap_stop()
        if self.input_stream is not None:
            try:
                self.input_stream.stop()
                self.input_stream.close()
            except Exception:
                pass
            self.input_stream = None
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
        if self.rust_dsp is not None:
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
        if not self.is_playing or (self.audio_data is None and self.input_device is None and not self.use_mac_tap):
            outdata.fill(0)
            return

        if self.use_mac_tap:
            tap = _get_mac_tap()
            in_block = np.zeros((frames, 2), dtype=np.float32)
            if tap is not None:
                import ctypes
                ptr = in_block.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
                got = tap.huq_tap_read(ptr, frames)
                if got == 0:
                    outdata.fill(0)
                    return
        elif self.input_device is not None:
            avail = self.live_write_pos - self.live_read_pos
            if avail < frames:
                outdata.fill(0)
                return
            if avail > self.block_size * 4:
                self.live_read_pos = self.live_write_pos - self.block_size * 2
            cap = len(self.live_ring)
            r = self.live_read_pos % cap
            if r + frames <= cap:
                in_block = self.live_ring[r:r+frames].copy()
            else:
                part = cap - r
                in_block = np.vstack((self.live_ring[r:], self.live_ring[:frames-part]))
            self.live_read_pos += frames
        else:
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

         if not self.float_mode and not self.bit_perfect:
             ceiling = 0.94406  # -0.5 dBFS headroom ceiling
             pk = float(np.max(np.abs(out_slice)))
             if pk * self.auto_trim_gain > ceiling:
                 self.auto_trim_gain = ceiling / max(pk, 1e-9)
             elif self.auto_trim_gain < 1.0:
                 self.auto_trim_gain = min(1.0, self.auto_trim_gain * 1.015)
             if self.auto_trim_gain < 0.9999:
                 np.multiply(out_slice, self.auto_trim_gain, out=out_slice)
             np.clip(out_slice, -1.0, 1.0, out=out_slice)
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
            new_h = (h + f) % b
        else:
            wrap = b - h
            self.viz_ring_in[h:] = in_block[:wrap, 0]
            self.viz_ring_in[:f-wrap] = in_block[wrap:, 0]
            
            self.viz_ring_out[h:] = out_L[:wrap]
            self.viz_ring_out[:f-wrap] = out_L[wrap:]
            
            self.viz_ring_hrir[h:] = hrir_L[:wrap]
            self.viz_ring_hrir[:f-wrap] = hrir_L[wrap:]
            
            new_h = f - wrap

        self.viz_head = new_h
        self.viz_state = (new_h, f, __import__('time').perf_counter())




def reset_macos_window_opaque(window):
    if sys.platform != "darwin":
        return
    try:
        import ctypes
        objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]

        def msg(obj, sel, res=ctypes.c_void_p, types=(), *args):
            fn = ctypes.cast(objc.objc_msgSend, ctypes.CFUNCTYPE(res, ctypes.c_void_p, ctypes.c_void_p, *types))
            return fn(obj, objc.sel_registerName(sel.encode()), *args)

        ns_win = msg(ctypes.c_void_p(int(window.winId())), "window")
        if not ns_win: return

        # Force window to be Opaque
        msg(ns_win, "setOpaque:", None, (ctypes.c_bool,), True)
        
        # Reset background color to the default system window color
        ns_color = msg(objc.objc_getClass(b"NSColor"), "windowBackgroundColor")
        msg(ns_win, "setBackgroundColor:", None, (ctypes.c_void_p,), ns_color)

        # Physically remove the Native Blur View from the window hierarchy
        content_view = msg(ns_win, "contentView")
        subviews = msg(content_view, "subviews")
        count = msg(subviews, "count", ctypes.c_ulong)
        
        for i in reversed(range(count)):
            view = msg(subviews, "objectAtIndex:", ctypes.c_void_p, (ctypes.c_ulong,), i)
            class_name = msg(view, "className", ctypes.c_char_p)
            if class_name and b"VisualEffectView" in class_name:
                msg(view, "removeFromSuperview")
    except Exception as e:
        print(f"Failed to reset window opacity: {e}")

from PySide6.QtWidgets import QDialog, QGridLayout, QFrame

class SettingsDialog(QDialog):
    def __init__(self, mw):
        super().__init__(mw)
        self.mw = mw
        self._glass_id = None
        self.setWindowTitle("Settings")
        self.setAttribute(Qt.WA_TranslucentBackground, windows_transparency_enabled())
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_ContentsMarginsRespectsSafeArea, False)
        self.setMinimumSize(460, 360)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        root = QFrame(self)
        root.setObjectName("settingsRoot")
        outer.addWidget(root)

        lay = QVBoxLayout(root)
        lay.setContentsMargins(18, 40 if sys.platform == "darwin" else 18, 18, 18)
        lay.setSpacing(12)

        app_card = make_glass_card()
        g = QGridLayout(app_card)
        g.setContentsMargins(16, 14, 16, 14)
        g.setHorizontalSpacing(14)
        g.setVerticalSpacing(12)
        g.setColumnStretch(1, 1)

        hdr1 = QLabel("Appearance")
        hdr1.setStyleSheet("font-weight: bold; font-size: 14px;")
        g.addWidget(hdr1, 0, 0, 1, 2)

        g.addWidget(QLabel("Theme:"), 1, 0)
        self.combo_theme = QComboBox()
        self.combo_theme.addItems(["Classic", "Light", "Glass"])

        self.combo_theme.currentIndexChanged.connect(self._on_theme)
        g.addWidget(self.combo_theme, 1, 1)

        g.addWidget(QLabel("Ambient Glow:"), 2, 0)
        self.s_glow = ResetSlider(30, Qt.Horizontal)
        self.s_glow.setRange(0, 100)
        self.s_glow.valueChanged.connect(mw.update_ambient_opacity)
        g.addWidget(self.s_glow, 2, 1)

        self.lbl_trans = QLabel("Glass Opacity:")
        g.addWidget(self.lbl_trans, 3, 0)
        self.s_trans = ResetSlider(45, Qt.Horizontal)
        self.s_trans.setRange(0, 100)
        self.s_trans.valueChanged.connect(self._on_glass)
        g.addWidget(self.s_trans, 3, 1)

        self.lbl_rim = QLabel("Glass Border Rim:")
        g.addWidget(self.lbl_rim, 4, 0)
        self.s_rim = ResetSlider(20, Qt.Horizontal)
        self.s_rim.setRange(0, 100)
        self.s_rim.valueChanged.connect(self._on_glass)
        g.addWidget(self.s_rim, 4, 1)
        lay.addWidget(app_card)

        perf_card = make_glass_card()
        p_lay = QVBoxLayout(perf_card)
        p_lay.setContentsMargins(16, 14, 16, 14)
        p_lay.setSpacing(8)
        hdr2 = QLabel("Engine & Future Modules")
        hdr2.setStyleSheet("font-weight: bold; font-size: 14px;")
        p_lay.addWidget(hdr2)

        self.chk_low_cpu = QCheckBox("Low-End CPU Mode (Faster startup & 10 FPS graph)")
        self.chk_low_cpu.toggled.connect(mw.toggle_cpu_mode)
        p_lay.addWidget(self.chk_low_cpu)

        note = QLabel("Layout presets & Last.fm / ListenBrainz scrobbling coming soon.")
        note.setWordWrap(True)
        note.setStyleSheet("opacity: 0.7; font-size: 12px;")
        p_lay.addWidget(note)
        lay.addWidget(perf_card)
        lay.addStretch()
        self.sync_from_main()

    def showEvent(self, event):
        super().showEvent(event)
        self.sync_from_main()

    def sync_from_main(self):
        idx_map = {"classic": 0, "light": 1, "liquid_glass": 2}
        self.combo_theme.blockSignals(True)
        self.combo_theme.setCurrentIndex(idx_map.get(self.mw.current_theme, 0))
        self.combo_theme.blockSignals(False)
        for s, val in ((self.s_glow, int(self.mw.ambient_opacity * 100)),
                       (self.s_trans, self.mw.glass_clarity),
                       (self.s_rim, self.mw.glass_rim)):
            s.blockSignals(True); s.setValue(val); s.blockSignals(False)
        self.chk_low_cpu.blockSignals(True)
        self.chk_low_cpu.setChecked(self.mw.engine.low_end_mode)
        self.chk_low_cpu.blockSignals(False)
        is_g = self.mw.current_theme == "liquid_glass"
        self.s_glow.default_value = 100 if is_g else (20 if self.mw.current_theme == "classic" else 30)
        self.lbl_trans.setEnabled(is_g)
        self.lbl_rim.setEnabled(is_g)
        self.s_trans.setEnabled(is_g)
        self.s_rim.setEnabled(is_g)
        if self.isVisible():
            if is_g:
                if glass and self._glass_id is None:
                    self._glass_id = glass.apply_glass_to_window(self)
                apply_windows_glass(self, True, False)
                apply_seamless_titlebar(self)
            else:
                if glass and self._glass_id is not None:
                    try:
                        glass.remove_glass_effect(self._glass_id)
                    except Exception:
                        pass
                    self._glass_id = None
                reset_macos_window_opaque(self)
                apply_windows_glass(self, False, self.mw.current_theme == 'light')
                apply_seamless_titlebar(self)
        self.setStyleSheet(self.mw.styleSheet())

    def _on_theme(self, idx):
        targets = ["classic", "light", "liquid_glass"]
        target = targets[idx]
        if self.mw.current_theme != target:
            self.mw.current_theme = targets[(idx - 1) % 3]
            self.mw.toggle_theme()
        self.sync_from_main()

    def _on_glass(self, _=None):
        self.mw.glass_clarity = self.s_trans.value()
        self.mw.glass_rim = self.s_rim.value()
        self.mw._on_ambient_color_changed(self.mw._current_ambient_color)
        self.mw.save_session_state()

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        QApplication.instance().installEventFilter(self)
        self.setWindowTitle("REXA — Reflectionless Engine for X-dimensional Audio")
        _ico_path = get_resource_path("assets/icon.png")
        if os.path.exists(_ico_path):
            self.setWindowIcon(QIcon(_ico_path))
        self.setAttribute(Qt.WA_TranslucentBackground, windows_transparency_enabled())
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_ContentsMarginsRespectsSafeArea, False)

        self.setMinimumSize(1100, 820)
        
        pg.setConfigOptions(antialias=False, enableExperimental=False)
        
        self.glass_frost = 50
        self.setStyleSheet(build_liquid_qss())

        self.smoothed_in = None
        self.smoothed_hrir = None
        self.smoothed_out = None
        self.viz_alpha = 20.0 

        self.library_tracks = []
        self.custom_tracks = []
        self.current_track_index = -1
        self.shuffle_enabled = False
        self._raw_cover_pixmap = None
        
        # Ambient background animation
        self._current_ambient_color = QColor(26, 36, 58)
        self.ambient_anim = QVariantAnimation(self)
        self.ambient_anim.setDuration(600)
        self.ambient_anim.setEasingCurve(QEasingCurve.InOutQuad)
        self.ambient_anim.valueChanged.connect(self._on_ambient_color_changed)

        self.engine = AudioEngine()
        self.engine.playback_stopped.connect(self.on_playback_stopped)
        self.engine.track_finished.connect(self.play_next_track)
        
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.update_ui_seekbar)

        self.viz_timer = QTimer(self)
        self.viz_timer.setInterval(8) 
        self.viz_timer.timeout.connect(self.update_graph)

        self.debounce_timer = QTimer(self)
        self.debounce_timer.setSingleShot(True)
        self.debounce_timer.setInterval(150) 
        self.debounce_timer.timeout.connect(self.apply_dsp_params)

        self.live_tonal_timer = QTimer(self)
        self.live_tonal_timer.setInterval(1500)
        self.live_tonal_timer.timeout.connect(self._update_live_tonal_match)

        self._bp_ramp_timer = QTimer(self)
        self._bp_ramp_timer.setInterval(25)
        self._bp_ramp_timer.timeout.connect(self._step_bit_perfect_ramp)

        self.loader_thread = None
        self.export_thread = None
        self.scanner_thread = None
        self.last_sys_default = None
        
        self.current_theme = "classic"
        self.glass_effect_id = None
        self.current_accent, self.current_accent_end = "#0a84ff", "#64d2ff"
        self.theme_glow = {"classic": 0.20, "light": 0.30, "liquid_glass": 1.00}
        self.ambient_opacity = self.theme_glow["classic"]
        self.glass_clarity = 45
        self.glass_rim = 20
        pg.setConfigOption("foreground", "#eef2f8")
        self.init_ui()
        
        self.sys_device_timer = QTimer(self)
        self.sys_device_timer.setInterval(1000)
        self.sys_device_timer.timeout.connect(self.check_sys_default)
        QTimer.singleShot(120, self._deferred_startup)

    def _on_glass_tuned(self, _=None):
        self.glass_clarity = self.slider_glass_clarity.value()
        self.glass_rim = self.slider_glass_rim.value()
        self._on_ambient_color_changed(self._current_ambient_color)
        self.save_session_state()

    def open_settings(self):
        if not hasattr(self, "_settings_dlg") or self._settings_dlg is None:
            self._settings_dlg = SettingsDialog(self)
        self._settings_dlg.sync_from_main()
        self._settings_dlg.show()
        self._settings_dlg.raise_()
        self._settings_dlg.activateWindow()

    def update_ambient_opacity(self, val):
        self.ambient_opacity = val / 100.0
        self.theme_glow[self.current_theme] = self.ambient_opacity
        self._on_ambient_color_changed(self._current_ambient_color)
        self.save_session_state()

    def _update_graph_theme(self):
        is_light = getattr(self, "current_theme", "classic") == "light"
        is_glass = getattr(self, "current_theme", "classic") == "liquid_glass"
        axis_color = '#1c1c1e' if is_light else '#eef2f8'
        if is_glass: axis_color = '#ffffff'
        
        pg.setConfigOption('foreground', axis_color)

        # Update axes and grid
        for axis_name in ['left', 'bottom']:
            axis = self.plot_widget.getAxis(axis_name)
            axis.setPen(pg.mkPen(axis_color))
            axis.setTextPen(pg.mkPen(axis_color))
        
        # Stronger grid in light mode for visibility
        self.plot_widget.showGrid(x=True, y=True, alpha=0.35 if is_light else 0.25)
        self.plot_widget.setTitle("Real-time Spectrum: Input vs Output", color=axis_color)

        # Safely initialize/update the legend WITHOUT creating duplicates
        if not hasattr(self, 'legend') or self.legend is None:
            self.legend = self.plot_widget.addLegend(offset=(10, 10))
            self.legend.setOpacity(0.1)
            self.plot_widget.legend = self.legend
        self.legend.setOpacity(0.1)
        self.plot_widget.legend = self.legend
        
        self.legend.clear()  # Clears old labels so they don't duplicate
        self.legend.setLabelTextColor(axis_color)
        
        # Give the legend a solid background for readability
        legend_bg = pg.mkBrush(255, 255, 255, 220) if is_light else pg.mkBrush(15, 18, 25, 180)
        self.legend.setBrush(legend_bg)
        
        # Re-add the items to the legend
        for name, curve in [("Input", self.curve_in), ("HRIR Only (Raw)", self.curve_hrir), ("Processed Output", self.curve_out), ("Correction Curve", self.curve_corr)]:
            self.legend.addItem(curve, name)

        # Update curve colors and fills based on theme
        from PySide6.QtGui import QColor
        if is_light:
            self.curve_in.setPen(pg.mkPen('#0066ff', width=2))
            self.curve_in.setBrush(pg.mkBrush(QColor(0, 102, 255, 40)))
            self.curve_hrir.setPen(pg.mkPen('#ff0055', width=2))
            self.curve_out.setPen(pg.mkPen('#00c7be', width=2))
            self.curve_out.setBrush(pg.mkBrush(QColor(0, 199, 190, 40)))
            self.curve_corr.setPen(pg.mkPen('#ff9500', width=2, style=Qt.DashLine))
        else:
            self.curve_in.setPen(pg.mkPen('#007aff', width=2))
            self.curve_in.setBrush(pg.mkBrush(QColor(0, 122, 255, 35)))
            self.curve_hrir.setPen(pg.mkPen('#ff2d55', width=2))
            self.curve_out.setPen(pg.mkPen('#34c759', width=2))
            self.curve_out.setBrush(pg.mkBrush(QColor(52, 199, 89, 40)))
            self.curve_corr.setPen(pg.mkPen('#ff9500', width=2, style=Qt.DashLine))

    def _get_accent_color(self, r, g, b):
        h, l, s = colorsys.rgb_to_hls(r/255.0, g/255.0, b/255.0)
        if s < 0.04:
            return ("#6e6e73", "#98989d") if getattr(self, "current_theme", "classic") == "light" else ("#8e8e93", "#c7c7cc")

        is_light = getattr(self, "current_theme", "classic") == "light"
        # Greens/yellows (h ~ 0.14..0.46) have high perceptual brightness, so tame saturation & luminance
        is_bright_hue = 0.13 <= h <= 0.46
        if is_light:
            s = min(0.72 if is_bright_hue else 0.82, max(0.48, s * 1.1))
            l = min(0.36 if is_bright_hue else 0.44, max(0.28, l))
        else:
            s = min(0.84 if is_bright_hue else 0.94, max(0.52, s * 1.25))
            l = min(0.56 if is_bright_hue else 0.68, max(0.44, l))

        nr, ng, nb = colorsys.hls_to_rgb(h, l, s)
        accent_hex = f"#{int(nr*255):02x}{int(ng*255):02x}{int(nb*255):02x}"
        l_end = min(0.86, l + 0.14)
        er, eg, eb = colorsys.hls_to_rgb(h, l_end, s * 0.92)
        accent_end_hex = f"#{int(er*255):02x}{int(eg*255):02x}{int(eb*255):02x}"
        return accent_hex, accent_end_hex

    def toggle_theme(self):
        if self.current_theme == "classic":
            self.current_theme = "light"
        elif self.current_theme == "light":
            self.current_theme = "liquid_glass"
        else:
            self.current_theme = "classic"

        self.ambient_opacity = self.theme_glow.get(self.current_theme, 1.0 if self.current_theme == "liquid_glass" else (0.20 if self.current_theme == "classic" else 0.30))
        theme_names = {"classic": "Classic", "light": "Light", "liquid_glass": "Glass"}
        if hasattr(self, "btn_theme"): self.btn_theme.setText(f"Theme: {theme_names[self.current_theme]}")
        if hasattr(self, "glass_tune_row"):
            self.glass_tune_row.setVisible(self.current_theme == "liquid_glass")

        if self.current_theme == "liquid_glass":
            if glass and getattr(self, "glass_effect_id", None) is None:
                self.glass_effect_id = glass.apply_glass_to_window(self)
            apply_windows_glass(self, True, False)
            apply_seamless_titlebar(self)
        else:
            if glass and getattr(self, "glass_effect_id", None) is not None:
                try:
                    glass.remove_glass_effect(self.glass_effect_id)
                except Exception:
                    pass
                self.glass_effect_id = None
            reset_macos_window_opaque(self)
            apply_windows_glass(self, False, self.current_theme == 'light')
            apply_seamless_titlebar(self)

        self._update_graph_theme()
        c = self._current_ambient_color
        
        self.setStyleSheet(build_liquid_qss(
            c.red(), c.green(), c.blue(), 
            theme=self.current_theme, 
            ambient_opacity=self.ambient_opacity, 
            accent=getattr(self, 'current_accent', '#0a84ff'), 
            accent_end=getattr(self, 'current_accent_end', '#64d2ff'),
            glass_clarity=self.glass_clarity,
            glass_rim=self.glass_rim
        ))
        
        # Force layout recalculation and repaint
        self.centralWidget().layout().activate()
        self.update()
        self.repaint()
        if getattr(self, "_settings_dlg", None) and self._settings_dlg.isVisible():
            self._settings_dlg.sync_from_main()
        self.save_session_state()

    def _on_ambient_color_changed(self, color):
        self.setStyleSheet(build_liquid_qss(color.red(), color.green(), color.blue(), theme=self.current_theme, ambient_opacity=self.ambient_opacity, accent=self.current_accent, accent_end=self.current_accent_end, glass_clarity=self.glass_clarity, glass_rim=self.glass_rim))
        if getattr(self, "_settings_dlg", None) and self._settings_dlg.isVisible():
            self._settings_dlg.setStyleSheet(self.styleSheet())

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

        self.combo_device.addItem("System Default", None)

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
            self._set_dev_status(self.engine.stream_mode_status, self.engine.stream_is_exclusive)

    def _deferred_startup(self):
        self.refresh_devices(initial=True)
        self.sys_device_timer.start()
        t0 = time.perf_counter()
        self.load_default_sofa()
        self.load_session_state()
        self._check_cpu_benchmark((time.perf_counter() - t0) * 1000.0)

    def _check_cpu_benchmark(self, startup_ms):
        state_path = self._state_file_path()
        if os.path.exists(state_path):
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    if "low_end_mode" in json.load(f):
                        return
            except Exception:
                pass
        dsp_ms = 0.0
        if self.engine.rust_dsp is not None:
            d_in = np.zeros((self.engine.block_size, 2), dtype=np.float32)
            d_out = np.zeros((self.engine.block_size, 2), dtype=np.float32)
            d_viz = np.zeros(self.engine.block_size, dtype=np.float32)
            # 2 warmup runs to fault pages and wake CPU boost clocks
            for _ in range(2):
                self.engine.rust_dsp.process(d_in, d_out, d_viz)
            samples = []
            for _ in range(16):
                tb = time.perf_counter()
                self.engine.rust_dsp.process(d_in, d_out, d_viz)
                samples.append((time.perf_counter() - tb) * 1000.0)
            dsp_ms = float(np.median(samples))
            self.engine.rust_dsp.reset()
        if dsp_ms > 8.0:
            reply = QMessageBox.question(
                self, "Performance Check",
                f"Slower CPU detected (DSP block time {dsp_ms:.1f} ms).\nEnable 'Low-End CPU' mode? (Real-time HRTF stays active; switch anytime in Settings.)",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            if reply == QMessageBox.Yes:
                if hasattr(self, "btn_cpu_mode"): self.btn_cpu_mode.setChecked(True)
                self.toggle_cpu_mode(True)
                return
        self.save_session_state()

    def toggle_cpu_mode(self, checked):
        low = bool(checked)
        self.engine.low_end_mode = low
        if hasattr(self, "btn_cpu_mode"): self.btn_cpu_mode.setText("CPU: Low-End" if low else "CPU: Full Quality")
        self.combo_fps.setCurrentIndex(0 if low else 3)
        if self.engine.sofa_data is not None:
            self.engine._update_hrirs()
        self.save_session_state()

    def load_default_sofa(self):
        default_path = get_resource_path("default.sofa")
        if os.path.exists(default_path):
            is_cached = os.path.exists(self.engine._sofa_cache_path(default_path))
            if not is_cached:
                self.lbl_sofa_file.setText("Caching HRTF (first launch)...")
                self._set_dev_status("First launch: caching HRTF acoustic profile — subsequent startups will be instant.", None)
                QApplication.processEvents()
            t_s = time.perf_counter()
            self.engine.load_sofa(default_path)
            dt_s = (time.perf_counter() - t_s) * 1000.0
            try:
                with open(os.path.join(os.path.expanduser("~"), ".hrtf_audio_player", "startup.log"), "a") as lf:
                    lf.write(f"cached={is_cached} load_sofa_ms={dt_s:.1f}\n")
            except Exception:
                pass
            self.lbl_sofa_file.setText("default.sofa (Built-in)")
            if not is_cached:
                self._set_dev_status(f"First launch complete ({dt_s/1000.0:.1f}s) — HRTF cached for instant future startups!", True)
                QTimer.singleShot(5000, lambda: self._set_dev_status("Tip: Select your DAC directly instead of System Default for native sample-rate switching.", None))
        else:
            self.lbl_sofa_file.setText("No SOFA loaded")

    def init_ui(self):
        central = QWidget()
        central.setObjectName("centralWidget")
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setSpacing(10)
        main_layout.setContentsMargins(20, 40 if sys.platform == 'darwin' else 20, 20, 20)

        # --- LEFT SIDE: LIBRARY ---
        left_card = make_glass_card()
        left_layout = QVBoxLayout(left_card)
        left_layout.setContentsMargins(14, 14, 14, 14)
        
        lib_top = QHBoxLayout()
        self.btn_open_file = QPushButton("Open File")
        self.btn_open_file.clicked.connect(self.open_single_file)
        self.btn_scan = QPushButton("Scan Folder")
        self.btn_scan.clicked.connect(self.scan_folder)
        self.lbl_lib_info = QLabel("Library empty")
        lib_top.addWidget(self.btn_open_file)
        lib_top.addWidget(self.btn_scan)
        lib_top.addWidget(self.lbl_lib_info, 1)
        left_layout.addLayout(lib_top)

        filter_layout = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search title or artist...")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setFocusPolicy(Qt.ClickFocus)
        self.search_input.returnPressed.connect(self.setFocus)
        self.search_input.textChanged.connect(self.filter_playlist)

        self.combo_group = QComboBox()
        self.combo_group.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.combo_group.setMinimumWidth(116)
        self.combo_group.view().setMinimumWidth(126)
        self.combo_group.addItems([
            "Custom",
            "Tracks",
            "Artist",
            "Album"
        ])
        self.combo_group.currentIndexChanged.connect(self.apply_sort)

        self.btn_sort_dir = QPushButton("A-Z")
        self.btn_sort_dir.setCheckable(True)
        self.btn_sort_dir.setFixedWidth(50)
        self.btn_sort_dir.setStyleSheet("padding: 6px 4px;")
        self.btn_sort_dir.toggled.connect(self.apply_sort)

        filter_layout.setSpacing(6)
        filter_layout.addWidget(self.search_input, 1)
        filter_layout.addWidget(self.combo_group, 0)
        filter_layout.addWidget(self.btn_sort_dir, 0)
        left_layout.addLayout(filter_layout)

        self.list_widget = MarqueeListWidget()
        self.list_widget.itemDoubleClicked.connect(self.play_selected_item)
        self.list_widget.setDragDropMode(QListWidget.InternalMove)
        self.list_widget.setDefaultDropAction(Qt.MoveAction)
        self.list_widget.setSelectionMode(QListWidget.ExtendedSelection)
        self.list_widget.model().rowsMoved.connect(self.on_playlist_reordered)
        left_layout.addWidget(self.list_widget, 1)
        
        main_layout.addWidget(left_card, stretch=1)

        # --- RIGHT SIDE: PLAYER & DSP ---
        right_layout = QVBoxLayout()
        right_layout.setSpacing(10)
        top_card = make_glass_card()
        top_card_layout = QVBoxLayout(top_card)
        top_card_layout.setContentsMargins(14, 14, 14, 14)
        top_card_layout.setSpacing(8)
        
        # Top Controls
        file_layout = QHBoxLayout()
        self.btn_load_sofa = QPushButton("Load SOFA")
        self.btn_load_sofa.clicked.connect(self.load_sofa)
        self.lbl_sofa_file = QLabel("No SOFA loaded")
        from PySide6.QtWidgets import QSizePolicy
        self.lbl_sofa_file.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        
        self.btn_export = QPushButton("Export Track")
        self.btn_export.clicked.connect(self.export_track)
        self.btn_export.setEnabled(False)
        
        self.btn_go_live = QPushButton("Go Live")
        self.btn_go_live.setCheckable(True)
        self.btn_go_live.setVisible(_get_mac_tap() is not None)
        self.btn_go_live.clicked.connect(self.toggle_go_live)

        file_layout.addWidget(self.btn_load_sofa)
        file_layout.addWidget(self.lbl_sofa_file, 1)
        file_layout.addWidget(self.btn_go_live)
        file_layout.addWidget(self.btn_export)

        self.btn_settings = QPushButton("⚙")
        self.btn_settings.setToolTip("Appearance & App Settings")
        self.btn_settings.setFixedWidth(40)
        self.btn_settings.clicked.connect(self.open_settings)
        file_layout.addWidget(self.btn_settings)
        top_card_layout.addLayout(file_layout)

        dev_layout = QHBoxLayout()
        dev_layout.addWidget(QLabel("Output Device:"))
        self.combo_device = DeviceComboBox()
        self.combo_device.refresh_callback = self._on_device_popup
        self.combo_device.addItem("System Default", None)
        self.combo_device.currentIndexChanged.connect(self.change_device)
        dev_layout.addWidget(self.combo_device, 1)
        
        self.lbl_sr = QLabel("Stream SR: -")
        self.lbl_sr.setStyleSheet("color: #ff9500; font-weight: bold;")
        dev_layout.addWidget(self.lbl_sr)
        top_card_layout.addLayout(dev_layout)

        _is_first = not os.path.exists(self.engine._sofa_cache_path(get_resource_path("default.sofa"))) if os.path.exists(get_resource_path("default.sofa")) else False
        self.lbl_dev_status = QLabel("First launch: caching HRTF acoustic profile — subsequent startups will be instant." if _is_first else "Tip: Select your DAC directly instead of System Default for native sample-rate switching.")
        self.lbl_dev_status.setWordWrap(True)
        self.lbl_dev_status.setStyleSheet("color: #8e8e93; font-size: 11px; margin-bottom: 4px;")
        top_card_layout.addWidget(self.lbl_dev_status)

        # Album Art & Track Info
        info_layout = QHBoxLayout()
        self.lbl_cover = QLabel()
        self.lbl_cover.setFixedSize(120, 120)
        self.lbl_cover.setStyleSheet("background-color: rgba(0,0,0,90); border: 1px solid rgba(255,255,255,45); border-radius: 12px;")
        
        track_text_layout = QVBoxLayout()
        self.lbl_track_title = MarqueeLabel("No Track Playing")
        self.lbl_track_title.setObjectName("trackTitle")
        self.lbl_track_artist = MarqueeLabel("Unknown Artist")
        self.lbl_track_artist.setObjectName("trackArtist")
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

        top_card_layout.addLayout(info_layout)

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
        top_card_layout.addLayout(seek_layout)

        # Transport Controls
        btn_layout = QHBoxLayout()
        self.btn_prev = QPushButton("⏮ Prev")
        self.btn_prev.clicked.connect(self.play_prev_track)

        self.btn_play = QPushButton("▶ Play")
        self.btn_play.setObjectName("playBtn")
        self.btn_play.setFixedWidth(120)
        self.btn_play.setFocusPolicy(Qt.NoFocus)
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
        top_card_layout.addLayout(btn_layout)

        dsp_btn_layout = QHBoxLayout()
        self.btn_bypass = QPushButton("Bypass DSP")
        self.btn_bypass.setCheckable(True)
        self.btn_bypass.clicked.connect(self.toggle_bypass)

        self.btn_bit_perfect = RightClickPushButton("Bit-Perfect: Off")
        self.btn_bit_perfect.setCheckable(True)
        self.btn_bit_perfect.setToolTip("Left-click: Safe gradual volume ramp to 100%\nRight-click: Instant 100% Bit-Perfect")
        self.btn_bit_perfect.clicked.connect(self.toggle_bit_perfect)
        self.btn_bit_perfect.rightClicked.connect(self.instant_bit_perfect)

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
        top_card_layout.addLayout(dsp_btn_layout)

        right_layout.addWidget(top_card)

        # Peak Meter & FPS
        spec_card = make_glass_card()
        spec_layout = QVBoxLayout(spec_card)
        spec_layout.setContentsMargins(14, 12, 14, 12)
        spec_layout.setSpacing(6)
        peak_layout = QHBoxLayout()
        
        peak_layout.addWidget(QLabel("Graph FPS:"))
        self.combo_fps = QComboBox()
        self.combo_fps.addItems(["10 FPS", "30 FPS", "60 FPS", "120 FPS"])
        self.combo_fps.setCurrentIndex(3) 
        self.combo_fps.setFixedWidth(120) 
        self.combo_fps.currentIndexChanged.connect(self.change_fps)
        peak_layout.addWidget(self.combo_fps)

        peak_layout.addWidget(QLabel("Time Decay:"))
        self.combo_smooth = QComboBox()
        self.combo_smooth.addItems(["Instant", "Fast", "Medium", "Slow"])
        self.combo_smooth.setCurrentIndex(1) 
        self.combo_smooth.setFixedWidth(120) 
        self.combo_smooth.currentIndexChanged.connect(self.change_smoothing)
        peak_layout.addWidget(self.combo_smooth)
        
        peak_layout.addStretch()
        self.lbl_peak = QLabel("Peak: -inf dB")
        self.lbl_peak.setStyleSheet("color: #34c759; font-weight: bold; font-family: 'Helvetica Neue';")
        peak_layout.addWidget(self.lbl_peak)
        
        spec_layout.addLayout(peak_layout)

        # Graph
        self.plot_widget = HoverPlotWidget(title="Real-time Spectrum: Input vs Output")
        self.plot_widget.setMinimumHeight(150)
        self.plot_widget.setBackground((0, 0, 0, 0))
        self.plot_widget.setStyleSheet('background: transparent; border: none;')
        self.plot_widget.viewport().setAutoFillBackground(False)
        self.plot_widget.getPlotItem().getViewBox().setBackgroundColor((0, 0, 0, 0))
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self.plot_widget.setLogMode(x=True, y=False)
        self.plot_widget.hideButtons()
        self.plot_widget.setMouseEnabled(x=False, y=False)
        
        # STATIC Y-AXIS RANGE
        self.plot_widget.setYRange(-100, 5) 
        
        self.plot_widget.setLabel('left', 'Amplitude', units='dB')
        self.plot_widget.setLabel('bottom', 'Frequency (Hz)')
        self.legend = self.plot_widget.addLegend(offset=(10, 10))
        self.legend.setOpacity(0.1)
        self.plot_widget.legend = self.legend
        
        # STATIC X-AXIS RANGE & TICKS
        f_ticks = [20, 30, 50, 70, 100, 200, 300, 500, 700, 1000, 2000, 3000, 5000, 7000, 10000, 20000, 30000, 40000]
        ticks = []
        for f in f_ticks:
            label = f"{f/1000:g}k" if f >= 1000 else str(f)
            ticks.append((np.log10(f), label))
        self.plot_widget.getAxis('bottom').setTicks([ticks])
        
        self.clip_line = pg.InfiniteLine(pos=0, angle=0, pen=pg.mkPen('r', style=Qt.DashLine, width=1))
        self.plot_widget.addItem(self.clip_line)
        
        from PySide6.QtGui import QColor
        self.curve_in = self.plot_widget.plot(pen=pg.mkPen('#007aff', width=2), name="Input", fillLevel=-120, brush=pg.mkBrush(QColor(0, 122, 255, 35)))
        self.curve_in.setDownsampling(auto=True, method='subsample')
        self.curve_in.setClipToView(True)
        
        self.curve_hrir = self.plot_widget.plot(pen=pg.mkPen('#ff2d55', width=2), name="HRIR Only (Raw)")
        self.curve_hrir.setDownsampling(auto=True, method='subsample')
        self.curve_hrir.setClipToView(True)
        
        self.curve_out = self.plot_widget.plot(pen=pg.mkPen('#34c759', width=2), name="Processed Output", fillLevel=-120, brush=pg.mkBrush(QColor(52, 199, 89, 40)))
        self.curve_out.setDownsampling(auto=True, method='subsample')
        self.curve_out.setClipToView(True)
        
        self.curve_corr = self.plot_widget.plot(pen=pg.mkPen('#ff9500', width=2, style=Qt.DashLine), name="Correction Curve")
        self.curve_corr.setDownsampling(auto=True, method='subsample')
        self.curve_corr.setClipToView(True)
        
        spec_layout.addWidget(self.plot_widget, stretch=1)
        right_layout.addWidget(spec_card, stretch=1)

        # DSP Sliders (Collapsible)
        dsp_card = make_glass_card()
        dsp_card_layout = QVBoxLayout(dsp_card)
        dsp_card_layout.setContentsMargins(14, 10, 14, 10)
        dsp_card_layout.setSpacing(6)
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

        dsp_card_layout.addLayout(dsp_header_layout)

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

        dsp_card_layout.addWidget(self.dsp_panel)
        right_layout.addWidget(dsp_card)
        
        main_layout.addLayout(right_layout, stretch=2)
        self._update_graph_theme()

    # --- LIBRARY LOGIC ---
    def scan_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Music Folder")
        if folder:
            self.btn_scan.setEnabled(False)
            self.lbl_lib_info.setText("Scanning...")
            if self.scanner_thread is not None and self.scanner_thread.isRunning():
                self.scanner_thread.quit()
                self.scanner_thread.wait()
            self.scanner_thread = ScannerThread(folder)
            self.scanner_thread.finished_scanning.connect(self.on_scan_finished)
            self.scanner_thread.start()

    def on_scan_finished(self, tracks, auto_play_if_empty=False):
        was_empty = len(self.library_tracks) == 0
        existing_map = {t['path']: i for i, t in enumerate(self.library_tracks)}

        added = 0
        for track in tracks:
            if track['path'] not in existing_map:
                self.library_tracks.append(track)
                self.custom_tracks.append(track)
                existing_map[track['path']] = len(self.library_tracks) - 1
                added += 1
            else:
                # Update metadata for existing tracks
                idx = existing_map[track['path']]
                old_track = self.library_tracks[idx]
                if old_track.get('album') != track.get('album') and track.get('album') != "Unknown Album":
                    old_track['album'] = track.get('album')
                if old_track.get('artist') != track.get('artist') and track.get('artist') != "Unknown Artist":
                    old_track['artist'] = track.get('artist')

        # Rebuild the entire widget from scratch to fix missing tracks and layout glitches
        self._rebuild_playlist_widget()
        
        self.lbl_lib_info.setText(f"{len(self.library_tracks)} tracks in playlist")
        self.btn_scan.setEnabled(True)
        
        if hasattr(self, "search_input") and self.search_input.text():
            self.filter_playlist(self.search_input.text())

        if auto_play_if_empty and was_empty and self.library_tracks:
            item = self.list_widget.item(0)
            self.list_widget.setCurrentItem(item)
            self.play_selected_item(item)

        self.save_session_state()

    def play_selected_item(self, item):
        self._pending_load_path = None
        self._pending_resume_sec = 0.0
        if getattr(self.engine, "use_mac_tap", False):
            self.btn_go_live.blockSignals(True)
            self.btn_go_live.setChecked(False)
            self.btn_go_live.setText("Go Live")
            self.btn_go_live.blockSignals(False)
            self.engine.use_mac_tap = False
            self.engine.auto_trim_gain = 1.0
            self.slider_seek.setEnabled(True)
            self.btn_prev.setEnabled(True)
            self.btn_next.setEnabled(True)
            self.btn_export.setEnabled(True)
        track = item.data(Qt.UserRole)
        self.current_track_index = self.list_widget.row(item)
        
        self.lbl_track_title.setText(track['title'])
        self.lbl_track_artist.setText(track['artist'])
        
        cover_bytes = _extract_cover_bytes(track['path'])
        if cover_bytes:
            pixmap = QPixmap()
            pixmap.loadFromData(cover_bytes)
            self._raw_cover_pixmap = pixmap.scaled(440, 440, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            self._last_cover_render_key = None
            self.update_cover_art_size()
        else:
            self._set_cover_badge("No Cover", "#666666", 26)
            
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
        self.engine.stop(fast=True)
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
            old_thread = self.loader_thread
            self.loader_thread = None
            try:
                old_thread.finished_loading.disconnect()
            except Exception:
                pass
            if old_thread.isRunning():
                old_thread.quit()
                old_thread.wait()
        
        self.loader_thread = LoaderThread(filepath)
        self.loader_thread.finished_loading.connect(self.on_audio_loaded)
        self.loader_thread.start()

    def on_audio_loaded(self, success, filepath):
        if self.sender() is not None and self.sender() is not self.loader_thread:
            return
        self.btn_play.setEnabled(True)
        
        if success and self.loader_thread is not None:
            if self.loader_thread.result_fs is None:
                return # Ignore stale signals from old threads
            self.engine.audio_data = self.loader_thread.result_data
            self.engine.fs = self.loader_thread.result_fs
            self.engine.file_fs = self.loader_thread.result_fs
            
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
            
            resume_sec = getattr(self, "_pending_resume_sec", 0.0)
            if resume_sec > 0.0:
                self._pending_resume_sec = 0.0
                target_frame = int(resume_sec * self.engine.fs)
                self.engine.seek(target_frame)
                self.lbl_time_current.setText(self.format_time(resume_sec))
                progress = (target_frame / len(self.engine.audio_data)) * 1000
                self.slider_seek.setValue(int(progress))
            self.toggle_play()

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
        
        if self.export_thread is not None and self.export_thread.isRunning():
            self.export_thread.quit()
            self.export_thread.wait()
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

    def toggle_go_live(self, checked):
        is_live = bool(checked)
        was_playing = self.engine.is_playing
        if was_playing:
            self.engine.stop(fast=True)
        self.engine.use_mac_tap = is_live
        if is_live:
            self.engine.tonal_filter = None
            self.engine.correction_f = None
            self.engine.correction_db = None
            self.curve_corr.setData([], [])
            self.engine._update_filters()
        self.engine.input_device = None
        self.btn_go_live.setText("Live: ON" if is_live else "Go Live")
        self.slider_vol.default_value = 80 if is_live else 44
        if is_live:
            self._pre_live_vol = self.slider_vol.value()
            if not self.engine.bit_perfect and self.slider_vol.value() < 75:
                self.slider_vol.setValue(80)
        elif hasattr(self, "_pre_live_vol") and not self.engine.bit_perfect:
            self.slider_vol.setValue(self._pre_live_vol)
        self.slider_vol.default_value = 80 if is_live else 44
        self.slider_seek.setEnabled(not is_live)
        self.btn_prev.setEnabled(not is_live)
        self.btn_next.setEnabled(not is_live)
        if is_live:
            self.btn_export.setEnabled(False)
            if self.engine.selected_device is None:
                for i in range(1, self.combo_device.count()):
                    self.combo_device.setCurrentIndex(i)
                    break
            self._set_cover_badge("LIVE", "#0a84ff", 44)
            self.lbl_track_title.setText("Live System Audio")
            self.lbl_track_artist.setText("macOS Driver-Free Process Tap")
            self.btn_play.setEnabled(True)
            tap = _get_mac_tap()
            if tap is not None:
                nat_sr = tap.huq_tap_start()
                if nat_sr > 0.0:
                    nat = int(round(nat_sr))
                    self.engine.tap_native_fs = nat
                    if self.engine.fs != nat:
                        self.engine.fs = nat
                        if self.engine.sofa_data is not None:
                            self.engine._update_hrirs()
                        else:
                            self.engine._update_filters()
            self.smoothed_in = None
            if not self.engine.is_playing:
                self.toggle_play()
            else:
                self.btn_play.setText("⏸ Pause")
                if not self.viz_timer.isActive():
                    self.viz_timer.start()
            self.live_tonal_timer.start()
            QTimer.singleShot(450, self._update_live_tonal_match)
            self.lbl_sr.setText(f"Stream SR: {self.engine.fs / 1000.0:.1f} kHz (Native)")
        else:
            self.live_tonal_timer.stop()
            self.engine.auto_trim_gain = 1.0
            saved_fs = getattr(self.engine, "file_fs", None)
            if saved_fs:
                self.engine.fs = saved_fs
                if self.engine.sofa_data is not None:
                    self.engine._update_hrirs()
                else:
                    self.engine._update_filters()
            if self.engine.audio_data is not None and self.engine.fs:
                self.lbl_sr.setText(f"Stream SR: {self.engine.fs / 1000.0:.1f} kHz")
            if 0 <= self.current_track_index < len(self.library_tracks):
                tr = self.library_tracks[self.current_track_index]
                self.lbl_track_title.setText(tr['title'])
                self.lbl_track_artist.setText(tr['artist'])
                cb = _extract_cover_bytes(tr['path'])
                if cb:
                    pm = QPixmap()
                    pm.loadFromData(cb)
                    self._raw_cover_pixmap = pm.scaled(440, 440, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                    self._last_cover_render_key = None
                    self.update_cover_art_size()
                else:
                    self._set_cover_badge("No Cover", "#666666", 26)
            else:
                self.lbl_track_title.setText("No Track Playing")
                self.lbl_track_artist.setText("Unknown Artist")
            self.btn_export.setEnabled(self.engine.audio_data is not None)
            self.btn_play.setText("▶ Play")
            self.timer.stop()
            self.viz_timer.stop()
            self._set_dev_status("Live Stopped — Press Play or select a track.", None)

    def _set_dev_status(self, text, exclusive):
        self.lbl_dev_status.setText(text)
        if exclusive is None:
            self.lbl_dev_status.setStyleSheet("color: #8e8e93; font-size: 11px; margin-bottom: 4px;")
        else:
            color = "#34c759" if exclusive else "#ff9500"
            self.lbl_dev_status.setStyleSheet(f"color: {color}; font-size: 11px; font-weight: bold; margin-bottom: 4px;")

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
            self._set_dev_status(self.engine.stream_mode_status, self.engine.stream_is_exclusive)

    def change_fps(self, index):
        fps_map = {0: 100, 1: 33, 2: 16, 3: 8}
        ms = fps_map.get(index, 100)
        self.viz_timer.setInterval(ms)

    def change_smoothing(self, index):
        taus_ms = [0.0, 20.0, 42.0, 70.0]
        self.viz_alpha = taus_ms[index] if 0 <= index < len(taus_ms) else 20.0

    def toggle_dsp_perf(self, checked):
        enabled = bool(checked)
        self.btn_dsp_perf.setText("DSP Time: On" if enabled else "DSP Time: Off")
        self.lbl_dsp_perf.setVisible(enabled)
        if enabled:
            self.engine.dsp_hist_idx = 0
            self.engine.dsp_hist_count = 0
            self.lbl_dsp_perf.setText("measuring...")

    def toggle_float_mode(self, state):
        self.engine.float_mode = bool(state)
        if state:
            self.btn_float_mode.setText("Float (32-bit Raw)")
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

    def _step_bit_perfect_ramp(self):
        cur = getattr(self, "_bp_ramp_float", float(self.slider_vol.value()))
        if cur < 99.9:
            nxt_f = min(100.0, cur + getattr(self, "_bp_ramp_step", 0.7))
            self._bp_ramp_float = nxt_f
            nxt = int(round(nxt_f))
            self.slider_vol.blockSignals(True)
            self.slider_vol.setValue(nxt)
            self.slider_vol.blockSignals(False)
            self.engine.volume = nxt_f / 100.0
            self.lbl_vol.setText(f"{nxt} %")
            self.engine._sync_rust_params()
        else:
            self._bp_ramp_timer.stop()
            self.slider_vol.blockSignals(True)
            self.slider_vol.setValue(100)
            self.slider_vol.blockSignals(False)
            self.engine.volume = 1.0
            self.engine.bit_perfect = True
            self.btn_bit_perfect.setText("Bit-Perfect: On")
            self.lbl_vol.setText("100 %")
            self.engine._sync_rust_params()

    def instant_bit_perfect(self):
        turn_on = not self.btn_bit_perfect.isChecked()
        self.btn_bit_perfect.blockSignals(True)
        self.btn_bit_perfect.setChecked(turn_on)
        self.btn_bit_perfect.blockSignals(False)
        self.toggle_bit_perfect(turn_on, instant=True)

    def toggle_bit_perfect(self, state, instant=False):
        self._bp_ramp_timer.stop()
        if state:
            cur_vol = self.slider_vol.value()
            self._pre_bp_vol = cur_vol
            self.engine.auto_trim_gain = 1.0
            self.engine.dsp_bypass = True
            self.btn_bypass.setChecked(True)
            self.btn_bypass.setText("DSP Bypassed")
            self.slider_vol.setEnabled(False)
            if instant or cur_vol >= 100:
                self.engine.bit_perfect = True
                self.btn_bit_perfect.setText("Bit-Perfect: On")
                self.slider_vol.blockSignals(True)
                self.slider_vol.setValue(100)
                self.slider_vol.blockSignals(False)
                self.engine.volume = 1.0
                self.lbl_vol.setText("100 %")
            else:
                self.engine.bit_perfect = False
                self._bp_ramp_float = float(cur_vol)
                self._bp_ramp_step = max(0.25, (100.0 - float(cur_vol)) / 80.0)  # 80 * 25ms = exact 2.0s ramp
                self.btn_bit_perfect.setText("Bit-Perfect: Ramp...")
                self._bp_ramp_timer.start()
        else:
            self.engine.bit_perfect = False
            self.btn_bit_perfect.setText("Bit-Perfect: Off")
            self.slider_vol.setEnabled(True)
            safe_def = 80 if getattr(self.engine, 'use_mac_tap', False) else 44
            restore_vol = getattr(self, "_pre_bp_vol", safe_def)
            if restore_vol >= 95:
                restore_vol = safe_def
            self.slider_vol.setValue(restore_vol)
        self.engine._sync_rust_params()

    def toggle_highs(self, state):
        self.engine.highs_enabled = bool(state)
        if state:
            self.btn_highs.setText("Highs Fix: On")
        else:
            self.btn_highs.setText("Highs Fix: Off")
        if self.engine.use_mac_tap:
            self.engine.correction_db = None
            self._update_live_tonal_match()
        elif self.engine.audio_data is not None:
            self.engine._calculate_tonal_match()
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

    def _update_live_tonal_match(self):
        if not (self.engine.is_playing and self.engine.use_mac_tap and self.engine.tonal_match_enabled):
            return
        if self.engine.dsp_bypass or self.engine.bit_perfect or self.engine.h_LL_fft_raw is None:
            return
        try:
            n_win = min(self.engine.viz_ring_size, int(self.engine.fs * 0.65))
            head = self.engine.viz_head
            if head >= n_win:
                seg = self.engine.viz_ring_in[head - n_win:head].copy()
            else:
                seg = np.concatenate((self.engine.viz_ring_in[-(n_win - head):], self.engine.viz_ring_in[:head]))
            if float(np.sqrt(np.mean(seg * seg))) < 1e-4:
                return  # Silence/paused stream, keep current curve

            n_fft = min(4096, len(seg))
            f, P_in = _fast_welch(seg, fs=self.engine.fs, nperseg=n_fft)
            f_raw = np.fft.rfftfreq(self.engine.N, 1.0 / self.engine.fs)
            gain_raw = (np.abs(self.engine.h_LL_fft_raw) ** 2) + (np.abs(self.engine.h_RL_fft_raw) ** 2)
            P_out = P_in * np.interp(f, f_raw, gain_raw)

            ratio_db = 10.0 * np.log10((P_in + 1e-12) / (P_out + 1e-12))
            ratio_db = _fast_uniform_filter1d(ratio_db, 15)
            ratio_db = np.clip(ratio_db, -24.0, 12.0)
            ratio_db[f > 4000.0] = np.clip(ratio_db[f > 4000.0], -24.0, 6.0)
            ratio_db[f < 20.0] = 0.0
            ratio_db[f < 120.0] = np.clip(ratio_db[f < 120.0], -24.0, 0.0)

            nyquist = self.engine.fs / 2.0
            fc = min(15500.0 if self.engine.highs_enabled else 12000.0, nyquist * 0.85)
            end_fc = nyquist if self.engine.highs_enabled else min(15000.0, nyquist * 0.92)
            taper = np.ones_like(f)
            idx_fade = (f >= fc) & (f <= end_fc)
            if np.any(idx_fade):
                taper[idx_fade] = (end_fc - f[idx_fade]) / max(1.0, end_fc - fc)
            taper[f > end_fc] = 0.0
            ratio_db *= taper

            if self.engine.correction_db is not None and len(self.engine.correction_db) == len(ratio_db):
                ratio_db = 0.65 * self.engine.correction_db + 0.35 * ratio_db
                if float(np.max(np.abs(ratio_db - self.engine.correction_db))) < 0.25:
                    return

            self.engine.correction_f = f
            self.engine.correction_db = ratio_db

            n_taps = 2049
            f_grid = np.linspace(0, 1.0, n_taps // 2 + 1)
            g_grid = np.interp(f_grid * (self.engine.fs / 2.0), f, ratio_db)
            self.engine.tonal_filter = _fast_firwin2(n_taps, f_grid, 10.0 ** (g_grid / 20.0))
            self.engine._update_filters()
        except Exception:
            pass

    def update_graph(self):
        try:
            if not self.engine.is_playing or (self.engine.audio_data is None and self.engine.input_device is None and not self.engine.use_mac_tap):
                return
                
            # Use welch for much smoother, professional-looking graph
            sr_mult = max(1, int(round(float(self.engine.fs) / 48000.0)))
            n_fft = min(4096 * sr_mult, self.engine.viz_ring_size // 2)
            w_seg = max(2048, n_fft // 2)
            b = self.engine.viz_ring_size
            head, last_frames, write_time = self.engine.viz_state
            now = time.perf_counter()
            block_dur = max(1e-4, last_frames / float(self.engine.fs))
            frac = min(1.0, max(0.0, (now - write_time) / block_dur))

            if not hasattr(self, 'chunk_in_buf') or len(self.chunk_in_buf) != n_fft:
                self.chunk_in_buf = np.zeros(n_fft)
                self.chunk_hrir_buf = np.zeros(n_fft)
                self.chunk_out_buf = np.zeros(n_fft)

            lag = int((1.0 - frac) * last_frames)
            h_audio = (head - lag) % b
            if h_audio >= n_fft:
                np.copyto(self.chunk_in_buf, self.engine.viz_ring_in[h_audio-n_fft:h_audio])
                np.copyto(self.chunk_out_buf, self.engine.viz_ring_out[h_audio-n_fft:h_audio])
            else:
                wrap = n_fft - h_audio
                self.chunk_in_buf[:wrap] = self.engine.viz_ring_in[-wrap:]
                self.chunk_in_buf[wrap:] = self.engine.viz_ring_in[:h_audio]
                self.chunk_out_buf[:wrap] = self.engine.viz_ring_out[-wrap:]
                self.chunk_out_buf[wrap:] = self.engine.viz_ring_out[:h_audio]

            f, P_in = _fast_welch(self.chunk_in_buf, fs=self.engine.fs, nperseg=w_seg)
            _, P_out = _fast_welch(self.chunk_out_buf, fs=self.engine.fs, nperseg=w_seg)
            P_in_db = np.maximum(10 * np.log10(P_in + 1e-12), -110.0)
            P_out_db = np.maximum(10 * np.log10(P_out + 1e-12), -110.0)

            if (
                self.engine.dsp_bypass
                or self.engine.bit_perfect
                or self.engine.h_LL_fft_raw is None
                or self.engine.h_RL_fft_raw is None
            ):
                P_hrir_db = P_in_db.copy()
            else:
                # Compute exact raw HRIR response directly from current Input PSD (100% time-aligned, zero seam jumps)
                if getattr(self, '_cached_raw_hrir_id', None) != id(self.engine.h_LL_fft_raw):
                    self._cached_raw_hrir_id = id(self.engine.h_LL_fft_raw)
                    f_raw = np.fft.rfftfreq(self.engine.N, 1.0 / self.engine.fs)
                    gain_raw = (np.abs(self.engine.h_LL_fft_raw) ** 2) + (np.abs(self.engine.h_RL_fft_raw) ** 2)
                    self._cached_hrir_gain = np.interp(f, f_raw, gain_raw)
                P_hrir_db = np.maximum(10 * np.log10((P_in * self._cached_hrir_gain) + 1e-12), -110.0)

            peak = np.max(np.abs(self.chunk_out_buf))
            peak_db = 20 * np.log10(peak + 1e-12)
            if peak_db > 0.0:
                if self.engine.float_mode:
                    self.lbl_peak.setText(f"Peak: +{peak_db:.1f} dB (32f OVER)")
                    self.lbl_peak.setStyleSheet("color: #ff9500; font-weight: bold; font-family: 'Helvetica Neue';")
                else:
                    self.lbl_peak.setText(f"Peak: +{peak_db:.1f} dB (CLIPPING!)")
                    self.lbl_peak.setStyleSheet("color: #ff3b30; font-weight: bold; font-family: 'Helvetica Neue';")
            else:
                trim = getattr(self.engine, "auto_trim_gain", 1.0)
                if trim < 0.995 and not self.engine.float_mode and not self.engine.bit_perfect:
                    gr_db = 20.0 * np.log10(max(trim, 1e-9))
                    self.lbl_peak.setText(f"Peak: {peak_db:.1f} dB (LIM {gr_db:.1f} dB)")
                    self.lbl_peak.setStyleSheet("color: #ff9500; font-weight: bold; font-family: 'Helvetica Neue';")
                else:
                    self.lbl_peak.setText(f"Peak: {peak_db:.1f} dB")
                    self.lbl_peak.setStyleSheet("color: #34c759; font-weight: bold; font-family: 'Helvetica Neue';")

            if getattr(self, 'smoothed_in', None) is None:
                self.smoothed_in = P_in_db
                self.smoothed_hrir = P_hrir_db
                self.smoothed_out = P_out_db
            else:
                dt_ms = max(1.0, float(self.viz_timer.interval()))
                tau_ms = self.viz_alpha
                if tau_ms <= 0.0:
                    self.smoothed_in = P_in_db
                    self.smoothed_hrir = P_hrir_db
                    self.smoothed_out = P_out_db
                else:
                    alpha = float(np.exp(-dt_ms / tau_ms))
                    self.smoothed_in = alpha * self.smoothed_in + (1.0 - alpha) * P_in_db
                    self.smoothed_hrir = alpha * self.smoothed_hrir + (1.0 - alpha) * P_hrir_db
                    self.smoothed_out = alpha * self.smoothed_out + (1.0 - alpha) * P_out_db

            self.plot_widget.setXRange(np.log10(20), np.log10(max(20000.0, float(self.engine.fs) * 0.5)), padding=0)
            # Slice [1:-1] and anchor at 20 Hz so the curve always touches the left axis border
            f_vis = f[1:-1]
            if len(f_vis) > 0 and f_vis[0] > 20.0:
                f_vis = np.insert(f_vis, 0, 20.0)
                in_vis = np.insert(self.smoothed_in[1:-1], 0, self.smoothed_in[1])
                hrir_vis = np.insert(self.smoothed_hrir[1:-1], 0, self.smoothed_hrir[1])
                out_vis = np.insert(self.smoothed_out[1:-1], 0, self.smoothed_out[1])
            else:
                in_vis = self.smoothed_in[1:-1]
                hrir_vis = self.smoothed_hrir[1:-1]
                out_vis = self.smoothed_out[1:-1]
            bypassed = bool(self.engine.dsp_bypass or self.engine.bit_perfect)
            self.curve_in.setData(f_vis, in_vis)
            self.curve_hrir.setData([], []) if bypassed else self.curve_hrir.setData(f_vis, hrir_vis)
            self.curve_out.setData(f_vis, out_vis)
            
            if self.engine.correction_f is not None:
                corr_db = self.engine.correction_db
                if self.engine.highs_enabled and getattr(self.engine, '_hf_eq_f', None) is not None:
                    corr_db = corr_db + np.interp(self.engine.correction_f, self.engine._hf_eq_f, self.engine._hf_eq_db)
                self.curve_corr.setData(self.engine.correction_f, corr_db)
        except Exception:
            pass

    def toggle_play(self):
        pending_path = getattr(self, "_pending_load_path", None)
        if pending_path and self.engine.audio_data is None and not self.engine.use_mac_tap:
            self._pending_load_path = None
            self._auto_play_after_load = True
            self.load_audio_file(pending_path)
            return
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
            self._set_dev_status(self.engine.stream_mode_status, self.engine.stream_is_exclusive)

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
        self._set_dev_status("Stopped — Select your DAC directly instead of System Default for native sample-rate switching.", None)

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
        new_v = val / 100.0
        if new_v < self.engine.volume:
            self.engine.auto_trim_gain = 1.0
        self.engine.volume = new_v
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
            album = "Unknown Album"
            try:
                mutagen, FLAC, ID3, MP3, MP4 = _get_mutagen()
                audio = mutagen.File(path)
                if audio:
                    if isinstance(audio, FLAC):
                        title = audio.get('title', [file])[0]
                        artist = audio.get('artist', ["Unknown Artist"])[0]
                        album = audio.get('album', ["Unknown Album"])[0]
                    elif isinstance(audio, (MP3, MP4)) and audio.tags:
                        if isinstance(audio.tags, ID3):
                            title = str(audio.tags.get('TIT2', file))
                            artist = str(audio.tags.get('TPE1', "Unknown Artist"))
                            album = str(audio.tags.get('TALB', "Unknown Album"))
                        else:
                            title = audio.tags.get('\xa9nam', [file])[0]
                            artist = audio.tags.get('\xa9ART', ["Unknown Artist"])[0]
                            album = audio.tags.get('\xa9alb', ["Unknown Album"])[0]
            except Exception:
                pass

            tracks.append({
                'path': path,
                'title': title,
                'artist': artist,
                'album': album,
            })

        self.on_scan_finished(tracks, auto_play_if_empty=True)
        
    def on_repair_finished(self):
        self._refresh_playlist_labels()
        if hasattr(self, "combo_group") and self.combo_group.currentIndex() != 0:
            self.apply_sort()
        self.save_session_state()

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

    def apply_sort(self, *args):
        if not self.library_tracks:
            return

        current_path = None
        if 0 <= self.current_track_index < len(self.library_tracks):
            current_path = self.library_tracks[self.current_track_index]['path']

        group_idx = self.combo_group.currentIndex()
        reverse = self.btn_sort_dir.isChecked()
        self.btn_sort_dir.setText("Z-A" if reverse else "A-Z")
        
        # Disable the A-Z button in Custom mode since custom is manually sorted
        self.btn_sort_dir.setEnabled(group_idx != 0)

        if group_idx == 0:
            self.library_tracks = list(self.custom_tracks)
        elif group_idx == 1:
            self.library_tracks.sort(key=lambda t: t.get('title', '').lower(), reverse=reverse)
        elif group_idx == 2:
            self.library_tracks.sort(key=lambda t: (t.get('artist', '').lower(), t.get('title', '').lower()), reverse=reverse)
        elif group_idx == 3:
            self.library_tracks.sort(key=lambda t: (t.get('album', '').lower(), t.get('title', '').lower()), reverse=reverse)

        self._refresh_playlist_labels()
        self._rebuild_playlist_widget(current_path)
        self.save_session_state()

    def _format_track_label(self, track):
        group_idx = self.combo_group.currentIndex() if hasattr(self, "combo_group") else 0
        if group_idx == 3: # Album
            return f"{track.get('album', 'Unknown Album')} - {track['title']}"
        elif group_idx == 2: # Artist
            return f"{track['artist']} - {track['title']}"
        return f"{track['title']} - {track['artist']}"

    def _refresh_playlist_labels(self):
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            track = item.data(Qt.UserRole)
            if track:
                item.setText(self._format_track_label(track))

    def _rebuild_playlist_widget(self, current_path=None):
        self.list_widget.setUpdatesEnabled(False)
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
        self.list_widget.setUpdatesEnabled(True)
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
        if hasattr(self, "combo_group") and self.combo_group.currentIndex() != 0:
            self.combo_group.blockSignals(True)
            self.combo_group.setCurrentIndex(0)
            self.combo_group.blockSignals(False)
            self._refresh_playlist_labels()
        self.save_session_state()

    def open_single_file(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Open Audio File(s)", "", "Audio Files (*.flac *.wav *.mp3 *.m4a *.ogg *.opus)"
        )
        if files:
            self.load_dropped_tracks(files)

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.KeyPress, QEvent.ShortcutOverride):
            fw = QApplication.focusWidget()
            if isinstance(fw, (QLineEdit, QSpinBox)):
                return False
            key = event.key()
            handled = (Qt.Key_Space, Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down, Qt.Key_M, Qt.Key_B)
            if key in handled:
                if event.type() == QEvent.ShortcutOverride:
                    event.accept()
                    return True
                if key == Qt.Key_Space and self.btn_play.isEnabled():
                    self.toggle_play()
                elif key == Qt.Key_Left and self.engine.audio_data is not None:
                    self.engine.seek(self.engine.current_frame - int(5 * self.engine.fs))
                    self.smoothed_in = None
                elif key == Qt.Key_Right and self.engine.audio_data is not None:
                    self.engine.seek(self.engine.current_frame + int(5 * self.engine.fs))
                    self.smoothed_in = None
                elif key == Qt.Key_Up:
                    self.slider_vol.setValue(min(100, self.slider_vol.value() + 5))
                elif key == Qt.Key_Down:
                    self.slider_vol.setValue(max(0, self.slider_vol.value() - 5))
                elif key == Qt.Key_M:
                    if self.slider_vol.value() > 0:
                        self._pre_mute_vol = self.slider_vol.value()
                        self.slider_vol.setValue(0)
                    else:
                        self.slider_vol.setValue(getattr(self, "_pre_mute_vol", 44))
                elif key == Qt.Key_B:
                    self.btn_bypass.toggle()
                    self.toggle_bypass(self.btn_bypass.isChecked())
                return True
            ch = event.text().strip().lower()
            no_cmd = not (event.modifiers() & (Qt.ControlModifier | Qt.MetaModifier | Qt.AltModifier))
            if len(ch) == 1 and ch.isalnum() and no_cmd:
                if event.type() == QEvent.ShortcutOverride:
                    event.accept()
                    return True
                matches = [
                    r for r in range(self.list_widget.count())
                    if not self.list_widget.item(r).isHidden()
                    and self.list_widget.item(r).text().lstrip().lower().startswith(ch)
                ]
                if matches:
                    cur = self.list_widget.currentRow()
                    if cur in matches:
                        nxt = matches[(matches.index(cur) + 1) % len(matches)]
                    else:
                        nxt = matches[0]
                    self.list_widget.setCurrentRow(nxt)
                    self.list_widget.scrollToItem(self.list_widget.item(nxt))
                return True
        return False

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
        self.list_widget.setUpdatesEnabled(False)
        for row in selected_rows:
            self.list_widget.takeItem(row)
            if 0 <= row < len(self.library_tracks):
                removed = self.library_tracks.pop(row)
                removed_paths.add(removed['path'])
        self.list_widget.setUpdatesEnabled(True)
        
        if removed_paths:
            self.custom_tracks = [t for t in self.custom_tracks if t['path'] not in removed_paths]
            
        self.list_widget.doItemsLayout() # Force layout recalculation

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

        self.list_widget.viewport().update()
        self.list_widget.viewport().repaint() # Force synchronous clear
        self.save_session_state()

    def save_session_state(self):
        try:
            pos_sec = getattr(self, "_pending_resume_sec", 0.0)
            if self.engine.audio_data is not None and self.engine.fs:
                pos_sec = self.engine.current_frame / float(self.engine.fs)

            state = {
                "tracks": self.library_tracks,
                "custom_tracks": self.custom_tracks,
                "group_mode": self.combo_group.currentIndex() if hasattr(self, "combo_group") else 0,
                "sort_dir": self.btn_sort_dir.isChecked() if hasattr(self, "btn_sort_dir") else False,
                "current_index": self.current_track_index,
                "position_sec": pos_sec,
                "volume": getattr(self, "_pre_bp_vol", getattr(self, "_pre_live_vol", int(round(self.engine.volume * 100)))) if (self.engine.bit_perfect or self._bp_ramp_timer.isActive() or self.engine.use_mac_tap) else int(round(self.engine.volume * 100)),
                "shuffle": self.shuffle_enabled,
                "dsp_panel_open": self.btn_toggle_dsp_panel.isChecked(),
                "theme": getattr(self, "current_theme", "classic"),
                "ambient_opacity": getattr(self, "ambient_opacity", 0.30),
                "theme_glow": getattr(self, "theme_glow", {"classic": 0.30, "light": 0.30, "liquid_glass": 1.00}),
                "glass_clarity": getattr(self, "glass_clarity", 45),
                "glass_rim": getattr(self, "glass_rim", 20),
                "low_end_mode": self.engine.low_end_mode,
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

            low = bool(state.get("low_end_mode", False))
            self.engine.low_end_mode = low
            if hasattr(self, "btn_cpu_mode"):
                self.btn_cpu_mode.blockSignals(True)
                self.btn_cpu_mode.setChecked(low)
                if hasattr(self, "btn_cpu_mode"): self.btn_cpu_mode.setText("CPU: Low-End" if low else "CPU: Full Quality")
                self.btn_cpu_mode.blockSignals(False)
                if low:
                    self.combo_fps.setCurrentIndex(0)
            theme = state.get("theme", "classic")
            saved_glow = state.get("theme_glow")
            if isinstance(saved_glow, dict):
                self.theme_glow.update({k: float(v) for k, v in saved_glow.items() if k in self.theme_glow})
            self.ambient_opacity = self.theme_glow.get(theme, float(state.get("ambient_opacity", 1.0 if theme == "liquid_glass" else 0.30)))
            self.glass_clarity = int(state.get("glass_clarity", 45))
            self.glass_rim = int(state.get("glass_rim", 20))
            if hasattr(self, "slider_glass_clarity"):
                self.slider_glass_clarity.blockSignals(True); self.slider_glass_clarity.setValue(self.glass_clarity); self.slider_glass_clarity.blockSignals(False)
                self.slider_glass_rim.blockSignals(True); self.slider_glass_rim.setValue(self.glass_rim); self.slider_glass_rim.blockSignals(False)
            if hasattr(self, "slider_glow"):
                self.slider_glow.blockSignals(True)
                self.slider_glow.setValue(int(self.ambient_opacity * 100))
                self.slider_glow.blockSignals(False)
            if theme in ("classic", "light", "liquid_glass") and self.current_theme != theme:
                order = ["classic", "light", "liquid_glass"]
                self.current_theme = order[(order.index(theme) - 1) % 3]
                self.toggle_theme()
            panel_open = bool(state.get("dsp_panel_open", False))
            self.btn_toggle_dsp_panel.setChecked(panel_open)
            self.toggle_dsp_panel(panel_open)

            sofa = state.get("sofa_path", "")
            if sofa and os.path.exists(sofa) and sofa != self.engine.sofa_path:
                self.lbl_sofa_file.setText(os.path.basename(sofa))
                self.engine.load_sofa(sofa)

            saved_tracks = [t for t in state.get("tracks", []) if t.get("path")]
            if not saved_tracks:
                return

            saved_custom = [t for t in state.get("custom_tracks", []) if t.get("path")]
            group_mode = state.get("group_mode", 0)
            sort_dir = state.get("sort_dir", False)
            if hasattr(self, "combo_group") and isinstance(group_mode, int) and 0 <= group_mode < self.combo_group.count():
                self.combo_group.blockSignals(True)
                self.combo_group.setCurrentIndex(group_mode)
                self.combo_group.blockSignals(False)
            if hasattr(self, "btn_sort_dir"):
                self.btn_sort_dir.blockSignals(True)
                self.btn_sort_dir.setChecked(sort_dir)
                self.btn_sort_dir.setText("Z-A" if sort_dir else "A-Z")
                self.btn_sort_dir.setEnabled(group_mode != 0)
                self.btn_sort_dir.blockSignals(False)

            self.on_scan_finished(saved_tracks)
            if saved_custom:
                self.custom_tracks = saved_custom
                
            # Only repair a small batch on startup to keep the app fast
            needs_repair = [t for t in self.library_tracks if 'album' not in t]
            if needs_repair:
                self.repair_thread = MetadataRepairThread(needs_repair[:50])
                self.repair_thread.finished_repair.connect(self.on_repair_finished)
                self.repair_thread.start()
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
                    self._raw_cover_pixmap = pixmap.scaled(440, 440, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                    self._last_cover_render_key = None
                    self.update_cover_art_size()

                self._pending_load_path = track['path']
                self._pending_resume_sec = pos_sec
                self.lbl_time_current.setText(self.format_time(pos_sec))
        except Exception as e:
            print(f"Failed to load session: {e}")

    def _set_cover_badge(self, text, color_hex="#666666", font_px=28):
        from PySide6.QtGui import QPainter, QColor, QFont
        pm = QPixmap(440, 440)
        pm.fill(QColor("#111111"))
        painter = QPainter(pm)
        painter.setPen(QColor(color_hex))
        painter.setFont(QFont("Helvetica Neue", font_px, QFont.Bold))
        painter.drawText(pm.rect(), Qt.AlignCenter, text)
        painter.end()
        self._raw_cover_pixmap = pm
        self._last_cover_render_key = None
        self.update_cover_art_size()

    def minimumSizeHint(self):
        return QSize(1100, 820)

    def update_cover_art_size(self):
        h = self.height()
        panel_h = self.dsp_panel.sizeHint().height() if (hasattr(self, "dsp_panel") and self.dsp_panel.isVisible()) else 0
        free_for_cover = (self.height() - panel_h - 590) * 0.65
        size = max(110, min(230, int(free_for_cover)))
        stepped_size = size

        self.lbl_cover.setMinimumSize(110, 110)
        if self.lbl_cover.width() != stepped_size or self.lbl_cover.height() != stepped_size:
            self.lbl_cover.setFixedSize(stepped_size, stepped_size)

        raw = getattr(self, "_raw_cover_pixmap", None)
        render_key = (id(raw), stepped_size)
        if getattr(self, "_last_cover_render_key", None) != render_key:
            self._last_cover_render_key = render_key
            r, g, b = sample_ambient_rgb(raw)
            
            target_color = QColor(r, g, b)
            self.current_accent, self.current_accent_end = self._get_accent_color(r, g, b)
            
            # Get the current actual color (if animation is running, use its current value)
            if hasattr(self, 'ambient_anim') and self.ambient_anim.state() == QVariantAnimation.Running:
                current_color = self.ambient_anim.currentValue()
            else:
                current_color = self._current_ambient_color

            if target_color != self._current_ambient_color:
                self.ambient_anim.stop()
                self.ambient_anim.setStartValue(current_color)
                self.ambient_anim.setEndValue(target_color)
                self._current_ambient_color = target_color
                
                # Instantly apply QSS with the new accent color but keep the OLD background color
                # This makes the buttons change instantly, while the background smoothly fades.
                self.setStyleSheet(build_liquid_qss(
                    current_color.red(), current_color.green(), current_color.blue(),
                    theme=self.current_theme, ambient_opacity=self.ambient_opacity,
                    accent=self.current_accent, accent_end=self.current_accent_end,
                    glass_clarity=self.glass_clarity, glass_rim=self.glass_rim
                ))
                
                self.ambient_anim.start()
            else:
                self._on_ambient_color_changed(self._current_ambient_color)
                
            if raw is not None and not raw.isNull():
                self.lbl_cover.setPixmap(
                    raw.scaled(stepped_size, stepped_size, Qt.KeepAspectRatioByExpanding, Qt.FastTransformation if getattr(self, '_is_resizing', False) else Qt.SmoothTransformation)
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

    def changeEvent(self, event):
        if event.type() == QEvent.WindowStateChange:
            if hasattr(self, "lbl_cover"):
                self.lbl_cover.setMinimumSize(110, 110)
            if not self.isFullScreen():
                for ms in (50, 350, 750):
                    QTimer.singleShot(ms, lambda: (apply_seamless_titlebar(self), self.setGeometry(self.screen().availableGeometry()) if getattr(getattr(self, '_drag_filter', None), '_was_zoomed', False) else None))
        super().changeEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "lbl_cover"):
            self.update_cover_art_size()

    def closeEvent(self, event):
        QApplication.instance().removeEventFilter(self)
        self.save_session_state()
        self.engine.stop()
        
        # Safely wait for any background threads to finish before closing
        for thread in [self.loader_thread, self.scanner_thread, self.export_thread]:
            if thread is not None and thread.isRunning():
                thread.quit()
                thread.wait()
                
        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window._drag_filter = WindowDragFilter(window)
    app.installEventFilter(window._drag_filter)
    window.show()
    apply_seamless_titlebar(window)
    window.raise_()
    window.activateWindow()
    sys.exit(app.exec())
