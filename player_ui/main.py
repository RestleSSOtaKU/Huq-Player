import sys
import os
import threading
import subprocess
import json
import time
import random
import hashlib
import tempfile
from collections import OrderedDict
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
_mutagen_lock = threading.Lock()

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
        with _mutagen_lock:
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
    step = max(1, nperseg // 4)  # 75% overlap for higher temporal & spectral resolution
    win = np.hanning(nperseg)
    scale = 1.0 / (fs * np.sum(win * win))
    frames = np.lib.stride_tricks.sliding_window_view(x, nperseg)[::step]
    frames = (frames - np.mean(frames, axis=1, keepdims=True)) * win
    psd = np.mean(np.abs(np.fft.rfft(frames, axis=1)) ** 2, axis=0) * scale
    if len(psd) > 2:
        psd[1:-1] *= 2.0
    elif len(psd) == 2:
        psd *= 2.0
    return np.fft.rfftfreq(nperseg, 1.0 / fs), psd

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
                               QComboBox, QListWidget, QListWidgetItem, QSplitter, QLineEdit, QStyledItemDelegate, QStyle, QStyleOptionViewItem, QAbstractItemView, QToolTip, QScrollArea, QSizePolicy)
from PySide6.QtCore import Qt, QTimer, Signal, QObject, QThread, QSize, QRect, QEvent, QPointF, QPoint
from PySide6.QtGui import QPixmap, QIcon, QColor, QPainter, QPalette
from dsp_bridge import RustDspBridge
from liquid_glass import (make_glass_card, sample_ambient_rgb, sample_ambient_palette,
                          LavaLampOverlay, build_liquid_qss, apply_seamless_titlebar,
                          apply_windows_glass, configure_macos_glass,
                          clear_macos_glass_state,
                          schedule_macos_panel_glass_sync,
                          remove_macos_panel_glass,
                          windows_transparency_enabled, read_system_glass_preferences,
                          WindowDragFilter, GLASS_STAGES, GLASS_STAGE_LABELS,
                          GLASS_STAGE_MATERIAL, GLASS_STAGE_TINT_MAX, DEFAULT_GLASS_RIM,
                          canonical_glass_material,
                          glass_material_profile, normalize_glass_overrides)
from glass_lens import GlassLensOverlay

GLASS_THEMES = ("liquid_glass", "macos27_glass")
THEME_ORDER = ("classic", "light", "liquid_glass", "macos27_glass")
THEME_LABELS = {
    "classic": "Classic",
    "light": "Light",
    "liquid_glass": "Glass",
    "macos27_glass": "Glass 2.0",
}

_DEFAULT_WINDOW_HEIGHT = 880
_LIBRARY_BASE_HEIGHT = 760

# Panel Scrim starts at 50% (fully transparent) and ends at 100% (full scrim).
# The lower half of the old 0-255 slider is cut off so the whole track is useful.
_SCRIM_SLIDER_MIN = 50
_SCRIM_SLIDER_MAX = 100

# Live graph performance readout. The target comes from the Graph FPS selector;
# the achieved rate is measured from actual update_graph() callbacks, not from
# the timer interval, so dropped frames are visible.
GRAPH_FPS_TARGETS = (10, 30, 60, 120)
GRAPH_FPS_INTERVALS_MS = (100, 33, 16, 8)
# Full Quality ships at a sustainable 60 FPS; 120 stays an opt-in stress target.
GRAPH_FPS_DEFAULT_INDEX = 2
_GRAPH_PERF_WINDOW_SEC = 0.7
_GRAPH_PERF_SMOOTHING = 0.4


def _scrim_slider_to_alpha(value):
    value = max(_SCRIM_SLIDER_MIN, min(_SCRIM_SLIDER_MAX, int(value)))
    return int(round(255.0 * (value - _SCRIM_SLIDER_MIN) /
                     (_SCRIM_SLIDER_MAX - _SCRIM_SLIDER_MIN)))


def _alpha_to_scrim_slider(alpha):
    alpha = max(0, min(255, int(alpha)))
    return _SCRIM_SLIDER_MIN + int(round(
        50.0 * alpha / 255.0
    ))


def graph_fps_target(index):
    if 0 <= index < len(GRAPH_FPS_TARGETS):
        return GRAPH_FPS_TARGETS[index]
    return GRAPH_FPS_TARGETS[-1]


def graph_fps_interval(index):
    if 0 <= index < len(GRAPH_FPS_INTERVALS_MS):
        return GRAPH_FPS_INTERVALS_MS[index]
    return GRAPH_FPS_INTERVALS_MS[-1]


def is_light_surface(theme, appearance="dark"):
    """True for any bright surface, including the Glass 2.0 Light stage."""
    return theme == "light" or (theme == "macos27_glass" and appearance == "light")


def muted_text_color(theme, appearance="dark"):
    """Readable secondary text on light glass; old grey on dark themes."""
    return "#3c3c43" if is_light_surface(theme, appearance) else "#8e8e93"


def performance_advice(target, achieved, *, low_end_mode=False,
                       spectrum_fill=True, theme=""):
    """Return (status, tip, color) for an achieved/target graph FPS pair."""
    ratio = float(achieved) / float(max(1, target))
    if ratio >= 0.90:
        return ("Full speed", "Graph updates are keeping up with the selected target.",
                "#34c759")

    tips = []
    if target >= 120:
        tips.append("switch to the 60 FPS graph target for a steadier display")
    if spectrum_fill:
        tips.append("disable Spectrum Gradient Fill to cut full-screen paint cost")
    if not low_end_mode:
        tips.append("enable Low-End CPU Mode if glass still stutters (Rust DSP stays active)")
    if theme == "macos27_glass":
        tips.append("Glass 2.0 adds per-card native blur, so 120 FPS is best-effort")
    if not tips:
        tips.append("close unused dialogs and lower Ambient Glow")
    status = "Near target" if ratio >= 0.70 else "Below target"
    color = "#ff9500" if ratio >= 0.70 else "#ff3b30"
    return (status, "Try: " + "; ".join(tips[:4]) + ".", color)


def is_glass_theme(theme):
    return theme in GLASS_THEMES


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

class SystemSyncLabel(QLabel):
    systemSyncRequested = Signal()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.systemSyncRequested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

class SystemSyncSlider(ResetSlider):
    systemSyncRequested = Signal()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.systemSyncRequested.emit()
            event.accept()
            return
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

# Cover extraction is lazy. Stamp-based LRU caches make repeated rows instant,
# and a content-keyed thumbnail cache avoids decoding the same large cover art
# once per track in an album.
_COVER_CACHE_LOCK = threading.Lock()
_COVER_MISS = object()
_COVER_CACHE_LIMIT = 512
_COVER_PATH_CACHE = OrderedDict()    # audio path -> (size, mtime_ns, embedded bytes or None)
_COVER_IMAGE_CACHE = OrderedDict()   # image path -> (size, mtime_ns, bytes)
_COVER_FOLDER_CACHE = OrderedDict()  # folder -> (dir_mtime_ns, chosen image path or None)

_MINI_THUMB_SIZE = 88
_MINI_THUMB_CACHE_LIMIT = 768
_MINI_THUMB_CACHE = OrderedDict()    # cover bytes digest -> 88x88 QImage


def _thumb_cache_get(key):
    if not key:
        return None
    from PySide6.QtGui import QImage
    with _COVER_CACHE_LOCK:
        img = _MINI_THUMB_CACHE.get(key)
        if img is None:
            return None
        _MINI_THUMB_CACHE.move_to_end(key)
        return QImage(img)


def _thumb_cache_set(key, img):
    if not key or img is None or img.isNull():
        return
    from PySide6.QtGui import QImage
    with _COVER_CACHE_LOCK:
        if key in _MINI_THUMB_CACHE:
            _MINI_THUMB_CACHE.move_to_end(key)
        elif len(_MINI_THUMB_CACHE) >= _MINI_THUMB_CACHE_LIMIT:
            _MINI_THUMB_CACHE.popitem(last=False)
        _MINI_THUMB_CACHE[key] = QImage(img)


def _decode_mini_thumb(raw):
    """Decode cover bytes straight to an 88x88 thumbnail, cached by content."""
    if not raw:
        return None
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize
    from PySide6.QtGui import QImage, QImageReader

    key = hashlib.blake2b(raw, digest_size=16).digest()
    cached = _thumb_cache_get(key)
    if cached is not None and not cached.isNull():
        return cached

    data = QByteArray(raw)  # keep the buffer alive for the whole decode
    buf = QBuffer(data)
    buf.open(QIODevice.ReadOnly)
    reader = QImageReader(buf)
    original = reader.size()
    if original.isValid() and original.width() > 0 and original.height() > 0:
        if original.width() >= original.height():
            target = QSize(
                _MINI_THUMB_SIZE,
                max(1, int(round(_MINI_THUMB_SIZE * original.height() / original.width()))),
            )
        else:
            target = QSize(
                max(1, int(round(_MINI_THUMB_SIZE * original.width() / original.height()))),
                _MINI_THUMB_SIZE,
            )
    else:
        target = QSize(_MINI_THUMB_SIZE, _MINI_THUMB_SIZE)
    reader.setScaledSize(target)
    img = reader.read()
    if img.isNull():
        src = QImage()
        if src.loadFromData(raw) and not src.isNull():
            img = src.scaled(_MINI_THUMB_SIZE, _MINI_THUMB_SIZE,
                             Qt.KeepAspectRatioByExpanding, Qt.FastTransformation)
    elif img.size() != QSize(_MINI_THUMB_SIZE, _MINI_THUMB_SIZE):
        img = img.scaled(_MINI_THUMB_SIZE, _MINI_THUMB_SIZE,
                         Qt.KeepAspectRatioByExpanding, Qt.FastTransformation)
    if not img.isNull():
        _thumb_cache_set(key, img)
        return img
    return None


def _cover_stamp(path):
    try:
        st = os.stat(path)
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return None


def _cover_cache_get(cache, key, stamp):
    if stamp is None:
        return _COVER_MISS
    with _COVER_CACHE_LOCK:
        hit = cache.get(key, _COVER_MISS)
        if hit is _COVER_MISS or hit[0] != stamp:
            return _COVER_MISS
        cache.move_to_end(key)
        return hit[1]


def _cover_cache_set(cache, key, stamp, value):
    if stamp is None:
        return
    with _COVER_CACHE_LOCK:
        if key in cache:
            cache.move_to_end(key)
        elif len(cache) >= _COVER_CACHE_LIMIT:
            cache.popitem(last=False)
        cache[key] = (stamp, value)


def _cached_image_bytes(path):
    stamp = _cover_stamp(path)
    hit = _cover_cache_get(_COVER_IMAGE_CACHE, path, stamp)
    if hit is not _COVER_MISS:
        return hit
    with open(path, "rb") as f:
        data = f.read()
    _cover_cache_set(_COVER_IMAGE_CACHE, path, stamp, data)
    return data


def _folder_cover_image(folder, priority_names, img_exts):
    stamp = _cover_stamp(folder)
    hit = _cover_cache_get(_COVER_FOLDER_CACHE, folder, stamp)
    if hit is not _COVER_MISS:
        return hit
    chosen = None
    try:
        entries = [e for e in os.listdir(folder)
                   if e.lower().endswith(img_exts) and not e.startswith(".")]
        if entries:
            for p_name in priority_names:
                for e in entries:
                    stem = os.path.splitext(e)[0].lower()
                    if stem == p_name or stem.startswith(p_name):
                        chosen = os.path.join(folder, e)
                        break
                if chosen:
                    break
            if not chosen:
                entries.sort(key=lambda e: os.path.getsize(os.path.join(folder, e)),
                             reverse=True)
                chosen = os.path.join(folder, entries[0])
    except OSError:
        chosen = None
    _cover_cache_set(_COVER_FOLDER_CACHE, folder, stamp, chosen)
    return chosen


def _extract_cover_bytes(path):
    stamp = _cover_stamp(path)
    embedded = _cover_cache_get(_COVER_PATH_CACHE, path, stamp)
    if embedded is not _COVER_MISS and embedded:
        return embedded

    # Only the embedded-tag result is keyed by the audio file stamp. External
    # art is rechecked every time because adding/replacing cover.jpg does not
    # touch the track's mtime; folder/image caches keep that lookup cheap.
    # 1. Check embedded tags (FLAC, MP3/ID3, MP4/M4A, Ogg/Opus)
    if embedded is _COVER_MISS:
        embedded = None
        try:
            mutagen, FLAC, ID3, MP3, MP4 = _get_mutagen()
            audio = mutagen.File(path)
            if audio:
                if isinstance(audio, FLAC) and audio.pictures:
                    embedded = audio.pictures[0].data
                elif getattr(audio, "tags", None):
                    if isinstance(audio.tags, ID3) or hasattr(audio.tags, "getall"):
                        for tag in audio.tags.values():
                            if getattr(tag, "FrameID", "") in ("APIC", "PIC"):
                                embedded = tag.data
                                break
                    if not embedded and "covr" in audio.tags:
                        embedded = bytes(audio.tags["covr"][0])
                    if not embedded and "metadata_block_picture" in audio.tags:
                        import base64
                        from mutagen.flac import Picture
                        pic = Picture(base64.b64decode(audio.tags["metadata_block_picture"][0]))
                        embedded = pic.data
        except Exception:
            embedded = None
        _cover_cache_set(_COVER_PATH_CACHE, path, stamp, embedded)
    if embedded:
        return embedded

    # 2. Fallback to external cover art in the track's folder (and the parent
    #    folder for CD1/Disc1). Folder scans are cached by directory mtime.
    try:
        folder = os.path.dirname(os.path.abspath(path))
        search_dirs = [folder]
        parent = os.path.dirname(folder)
        if os.path.basename(folder).lower().startswith(("cd", "disc", "disk")) and parent != folder:
            search_dirs.append(parent)

        img_exts = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
        priority_names = ("cover", "folder", "front", "album", "albumart", "artwork", "scan")
        for d in search_dirs:
            image_path = _folder_cover_image(d, priority_names, img_exts)
            if image_path:
                return _cached_image_bytes(image_path)
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
        data = index.data(Qt.UserRole)
        if isinstance(data, dict) and data.get("_is_header"):
            return QSize(100, 56)
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
        data = index.data(Qt.UserRole)
        if isinstance(data, dict) and data.get("_is_header"):
            from PySide6.QtGui import QFont, QPainterPath, QPen
            painter.save()
            painter.setRenderHint(QPainter.Antialiasing, True)
            mw = self.parent().window() if self.parent() else None
            is_light = is_light_surface(
                getattr(mw, 'current_theme', 'classic'),
                getattr(mw, 'glass_appearance', 'dark'))
            r = option.rect
            painter.setPen(QPen(QColor(0, 0, 0, 28) if is_light else QColor(255, 255, 255, 28), 1))
            painter.drawLine(r.left() + 8, r.top() + 3, r.right() - 8, r.top() + 3)

            cov_rect = r.adjusted(8, 8, 0, -4)
            cov_rect.setWidth(44)
            cov_rect.setHeight(44)
            pm = mw._get_mini_cover_async(data.get("sample_path", "")) if (mw and hasattr(mw, "_get_mini_cover_async")) else None
            clip = QPainterPath()
            clip.addRoundedRect(cov_rect, 7, 7)
            painter.setClipPath(clip)
            if pm is not None and not pm.isNull():
                painter.drawPixmap(cov_rect, pm)
            else:
                painter.fillRect(cov_rect, QColor(0, 0, 0, 35) if is_light else QColor(255, 255, 255, 22))
            painter.setClipping(False)

            tx = cov_rect.right() + 10
            tw = max(40, r.right() - tx - 56)
            fg = QColor("#161618" if is_light else "#f2f4f8")
            sub = QColor("#636366" if is_light else "#9ca3af")
            painter.setPen(fg)
            painter.setFont(QFont(".AppleSystemUIFont", 12, QFont.Bold))
            el_art = painter.fontMetrics().elidedText(data.get("artist", "Unknown Artist"), Qt.ElideRight, tw)
            painter.drawText(tx, r.top() + 8, tw, 20, Qt.AlignVCenter | Qt.AlignLeft, el_art)

            painter.setPen(sub)
            painter.setFont(QFont(".AppleSystemUIFont", 11))
            el_alb = painter.fontMetrics().elidedText(data.get("album", "Unknown Album"), Qt.ElideRight, tw)
            painter.drawText(tx, r.top() + 28, tw, 18, Qt.AlignVCenter | Qt.AlignLeft, el_alb)

            cnt_txt = f"{data.get('count', 0)} trk"
            painter.drawText(r.right() - 52, r.top() + 8, 44, 20, Qt.AlignVCenter | Qt.AlignRight, cnt_txt)
            painter.restore()
            return

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

        mw = self.parent().window() if self.parent() else None
        group_idx = mw.combo_group.currentIndex() if (mw and hasattr(mw, "combo_group")) else 0
        if group_idx not in (3, 4) and isinstance(data, dict) and data.get("path") and hasattr(mw, "_get_mini_cover_async"):
            pm = mw._get_mini_cover_async(data["path"])
            if pm is not None and not pm.isNull():
                from PySide6.QtGui import QPainterPath, QLinearGradient
                painter.save()
                painter.setRenderHint(QPainter.Antialiasing, True)
                bg_r = option.rect.adjusted(2, 2, -2, -2)
                clip = QPainterPath()
                clip.addRoundedRect(bg_r, 8, 8)
                painter.setClipPath(clip)
                # Horizontal middle crop of cover stretched across the row at subtle opacity
                src_h = max(8, int(pm.height() * 0.34))
                src_y = (pm.height() - src_h) // 2
                painter.setOpacity(0.16 if not bool(option.state & QStyle.State_Selected) else 0.12)
                painter.drawPixmap(bg_r, pm, pm.rect().adjusted(0, src_y, 0, -(pm.height() - src_y - src_h)))
                is_light = is_light_surface(
                    getattr(mw, 'current_theme', 'classic'),
                    getattr(mw, 'glass_appearance', 'dark'))
                grad = QLinearGradient(bg_r.left(), 0, bg_r.right(), 0)
                base_c = QColor(255, 255, 255, 165) if is_light else QColor(18, 20, 26, 175)
                edge_c = QColor(255, 255, 255, 55) if is_light else QColor(18, 20, 26, 65)
                grad.setColorAt(0.0, base_c)
                grad.setColorAt(0.65, edge_c)
                grad.setColorAt(1.0, base_c)
                painter.setOpacity(1.0)
                painter.fillRect(bg_r, grad)
                painter.restore()
            
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
            is_light = is_light_surface(
                getattr(mw, 'current_theme', 'classic'),
                getattr(mw, 'glass_appearance', 'dark'))
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

def _read_audio_tags(path):
    file = os.path.basename(path)
    title, artist, album, track_no, disc_no = file, "Unknown Artist", "Unknown Album", 0, 1
    def _first(val, default):
        if isinstance(val, (list, tuple)):
            return val[0] if val else default
        return val if val else default
    def _parse_num(val, default=0):
        try:
            s = str(_first(val, default)).split("/")[0]
            digits = "".join(c for c in s if c.isdigit())
            return int(digits) if digits else default
        except Exception:
            return default
    try:
        mutagen, FLAC, ID3, MP3, MP4 = _get_mutagen()
        audio = mutagen.File(path)
        if audio:
            if isinstance(audio, FLAC):
                title = _first(audio.get('title'), file)
                artist = _first(audio.get('artist'), "Unknown Artist")
                album = _first(audio.get('album'), "Unknown Album")
                track_no = _parse_num(audio.get('tracknumber'), 0)
                disc_no = _parse_num(audio.get('discnumber'), 1)
            elif isinstance(audio, (MP3, MP4)) and audio.tags:
                if isinstance(audio.tags, ID3):
                    title = str(audio.tags.get('TIT2', file)) or file
                    artist = str(audio.tags.get('TPE1', "Unknown Artist")) or "Unknown Artist"
                    album = str(audio.tags.get('TALB', "Unknown Album")) or "Unknown Album"
                    track_no = _parse_num(audio.tags.get('TRCK', 0), 0)
                    disc_no = _parse_num(audio.tags.get('TPOS', 1), 1)
                else:
                    title = _first(audio.tags.get('©nam'), file)
                    artist = _first(audio.tags.get('©ART'), "Unknown Artist")
                    album = _first(audio.tags.get('©alb'), "Unknown Album")
                    trkn = _first(audio.tags.get('trkn'), (0, 0))
                    disk = _first(audio.tags.get('disk'), (1, 0))
                    track_no = int(trkn[0]) if isinstance(trkn, (list, tuple)) and trkn else 0
                    disc_no = int(disk[0]) if isinstance(disk, (list, tuple)) and disk else 1
    except Exception:
        pass
    return {'path': path, 'title': title, 'artist': artist, 'album': album, 'track_no': track_no, 'disc_no': disc_no}

class MetadataRepairThread(QThread):
    finished_repair = Signal()
    def __init__(self, tracks):
        super().__init__()
        self.tracks = tracks
    def run(self):
        for t in self.tracks:
            if os.path.exists(t.get('path', '')):
                info = _read_audio_tags(t['path'])
                t['album'] = info['album']
                t['track_no'] = info['track_no']
                t['disc_no'] = info['disc_no']
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
                    tracks.append(_read_audio_tags(os.path.join(root, file)))
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
        self.latest_stereo_out = None
        
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
        self.cb_stereo_buf = np.zeros((self.block_size, 2), dtype=np.float32)
        self.cb_ramp_buf = np.ones((self.block_size, 1), dtype=np.float32)
        self._unit_ramp = np.linspace(0.0, 1.0, self.block_size, dtype=np.float32)
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
            
        h_LL = np.asarray(self.raw_ipsi_L, dtype=np.float64)
        h_RL = np.asarray(self.raw_contra_R, dtype=np.float64)
        h_LR = np.asarray(self.raw_contra_L, dtype=np.float64)
        h_RR = np.asarray(self.raw_ipsi_R, dtype=np.float64)

        max_h_len = max(len(h_LL), len(h_RL), len(h_LR), len(h_RR))
        self.N = 2 ** int(np.ceil(np.log2(self.block_size + max_h_len - 1)))
        
        self.h_LL_fft_raw = np.fft.rfft(h_LL, n=self.N)
        self.h_RL_fft_raw = np.fft.rfft(h_RL, n=self.N)
        self.h_LR_fft_raw = np.fft.rfft(h_LR, n=self.N)
        self.h_RR_fft_raw = np.fft.rfft(h_RR, n=self.N)

        f = np.fft.rfftfreq(self.N, 1.0 / self.fs)

        # Zero-phase tonal magnitude equalization (preserves exact SOFA ITD & inter-ear phase)
        if self.tonal_match_enabled and self.correction_f is not None and self.correction_db is not None:
            corr_interp_db = np.interp(f, self.correction_f, self.correction_db)
            eq_mag = 10.0 ** (corr_interp_db / 20.0)
        else:
            eq_mag = 1.0

        self.h_LL_fft_norm = self.h_LL_fft_raw * eq_mag
        self.h_RL_fft_norm = self.h_RL_fft_raw * eq_mag
        self.h_LR_fft_norm = self.h_LR_fft_raw * eq_mag
        self.h_RR_fft_norm = self.h_RR_fft_raw * eq_mag

        mag_LL = np.abs(self.h_LL_fft_norm) + 1e-12
        mag_RL = np.abs(self.h_RL_fft_norm) + 1e-12
        mag_LR = np.abs(self.h_LR_fft_norm) + 1e-12
        mag_RR = np.abs(self.h_RR_fft_norm) + 1e-12

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

        # Phase-coherent HF extension locked to each HRIR's true onset delay (zero 14 kHz phase jump)
        def _true_delay_samples(ir):
            pk = float(np.max(np.abs(ir)))
            idx = np.where(np.abs(ir) >= 0.25 * pk)[0]
            return float(idx[0]) if len(idx) > 0 else float(np.argmax(np.abs(ir)))

        w = 2.0 * np.pi * f / float(self.fs)
        h_LL_ext = ipsi_ref_L * np.exp(-1j * w * _true_delay_samples(h_LL))
        h_RR_ext = ipsi_ref_R * np.exp(-1j * w * _true_delay_samples(h_RR))
        h_RL_ext = (ipsi_ref_L * xf_ratio_RL) * np.exp(-1j * w * _true_delay_samples(h_RL))
        h_LR_ext = (ipsi_ref_R * xf_ratio_LR) * np.exp(-1j * w * _true_delay_samples(h_LR))

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

        # Enforce linear overlap-add constraint (IR length <= N - block_size) so frequency-domain EQ/HF extension never wraps around
        max_ir = max(64, self.N - self.block_size)
        fade = np.linspace(1.0, 0.0, 64, dtype=np.float64)
        def _clamp_ir_fft(H_fft):
            ir = np.fft.irfft(H_fft, n=self.N)[:max_ir].copy()
            ir[-64:] *= fade
            return np.fft.rfft(ir, n=self.N)

        self.h_LL_fft = _clamp_ir_fft(self.h_LL_fft)
        self.h_RL_fft = _clamp_ir_fft(self.h_RL_fft)
        self.h_LR_fft = _clamp_ir_fft(self.h_LR_fft)
        self.h_RR_fft = _clamp_ir_fft(self.h_RR_fft)
        self.h_LL_fft_raw = _clamp_ir_fft(self.h_LL_fft_raw)
        self.h_RL_fft_raw = _clamp_ir_fft(self.h_RL_fft_raw)

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
            import gc; gc.disable()
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
        import gc; gc.enable(); gc.collect()

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

    def _audio_callback(self, outdata, frames, time_info, status):
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
         t0 = time.perf_counter()
         self.rust_dsp.process(in_block, out_slice, viz_slice)
         self.last_dsp_ms = (time.perf_counter() - t0) * 1000.0
         self.dsp_hist[self.dsp_hist_idx] = self.last_dsp_ms
         self.dsp_hist_idx = (self.dsp_hist_idx + 1) & 4095
         if self.dsp_hist_count < 4096:
             self.dsp_hist_count += 1

         if not self.float_mode and not self.bit_perfect:
             ceiling = 0.94406  # -0.5 dBFS headroom ceiling
             prev_trim = self.auto_trim_gain
             pk = float(np.max(np.abs(out_slice)))
             if pk * prev_trim > ceiling:
                 self.auto_trim_gain = ceiling / max(pk, 1e-9)
             elif prev_trim < 1.0:
                 self.auto_trim_gain = min(1.0, prev_trim * 1.015)
             if prev_trim < 0.9999 or self.auto_trim_gain < 0.9999:
                 if abs(self.auto_trim_gain - prev_trim) > 1e-5:
                     ramp = self.cb_ramp_buf[:frames, 0]
                     np.multiply(self._unit_ramp[:frames], float(self.auto_trim_gain - prev_trim), out=ramp)
                     np.add(ramp, float(prev_trim), out=ramp)
                     np.multiply(out_slice, self.cb_ramp_buf[:frames], out=out_slice)
                 else:
                     np.multiply(out_slice, self.auto_trim_gain, out=out_slice)
             np.clip(out_slice, -1.0, 1.0, out=out_slice)
         st_dst = self.cb_stereo_buf[:frames]
         np.copyto(st_dst, out_slice)
         self.latest_stereo_out = st_dst
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
        self.viz_state = (new_h, f, time.perf_counter())




def reset_macos_window_opaque(window):
    if sys.platform != "darwin":
        return
    try:
        # PyObjC returns real NSString class names. The previous ctypes version
        # read -className as char*, which reads raw NSString bytes and can
        # segfault after a few theme switches.
        from AppKit import NSColor
        from pyqt_liquidglass._bridge import (get_nswindow_from_widget,
                                              get_nsview_from_widget)

        ns_window = get_nswindow_from_widget(window)
        if ns_window is None:
            return
        ns_window.setOpaque_(True)
        ns_window.setBackgroundColor_(NSColor.windowBackgroundColor())

        content_view = ns_window.contentView()
        root_view = get_nsview_from_widget(window)
        managed = [v for v in (
            [getattr(window, "_glass_view", None)] +
            [rec.get("view") for rec in
             (getattr(window, "_panel_glass_views", []) or [])]
        ) if v is not None]

        def is_same(a, b):
            if a is b:
                return True
            try:
                return bool(a.isEqual_(b))
            except Exception:
                return False

        for view in list(content_view.subviews() or []):
            if root_view is not None and is_same(view, root_view):
                continue
            if any(is_same(view, other) for other in managed):
                continue
            try:
                name = str(view.className() or "")
            except Exception:
                continue
            if "VisualEffectView" in name or "GlassEffectView" in name:
                view.removeFromSuperview()
    except Exception as e:
        print(f"Failed to reset window opacity: {e}")

from PySide6.QtWidgets import QDialog, QGridLayout, QFrame, QProgressBar

APP_VERSION = "0.5.3"
try:
    from lfm_keys import LFM_API_KEY, LFM_API_SECRET
except ImportError:
    LFM_API_KEY = os.environ.get("LFM_API_KEY", "")
    LFM_API_SECRET = os.environ.get("LFM_API_SECRET", "")
GITHUB_RELEASES_API = "https://api.github.com/repos/RestleSSOtaKU/REXA-Player/releases/latest"

def _urlopen_resilient(req, timeout=8):
    import urllib.request, urllib.error
    ctx = _ssl_context()
    for use_proxy in (True, False):
        try:
            if use_proxy:
                return urllib.request.urlopen(req, timeout=timeout, context=ctx)
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(context=ctx)
            )
            return opener.open(req, timeout=timeout)
        except urllib.error.HTTPError:
            raise
        except Exception as err:
            last_err = err
    raise RuntimeError(f"{last_err} [{req.full_url}]")

def _ssl_context():
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()

def _parse_ver(tag):
    clean = str(tag or "").strip().lstrip("vV")
    parts = []
    for p in clean.split("."):
        digits = "".join(c for c in p if c.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])

class ScrobbleWorker(QThread):
    status_msg = Signal(str)

    def __init__(self, cfg, action, meta=None, parent=None):
        super().__init__(parent)
        self.cfg = dict(cfg or {})
        self.action = action
        self.meta = dict(meta or {})

    @staticmethod
    def _lfm_sig(params, secret):
        import hashlib
        raw = "".join(f"{k}{params[k]}" for k in sorted(params.keys())) + secret
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def _call_lfm(self, params, secret, post=True):
        import json, urllib.request, urllib.parse, urllib.error
        p = {k: str(v) for k, v in params.items() if v is not None}
        p["api_sig"] = self._lfm_sig(p, secret)
        p["format"] = "json"
        q = urllib.parse.urlencode(p)
        hdrs = {"User-Agent": f"REXA-Player/{APP_VERSION}", "Content-Type": "application/x-www-form-urlencoded"}
        api_base = "https://" + ".".join(("ws", "audioscrobbler", "com")) + "/2.0/"
        req = urllib.request.Request(api_base, data=q.encode("utf-8"), headers=hdrs) if post else urllib.request.Request(f"{api_base}?{q}", headers={"User-Agent": hdrs["User-Agent"]})
        try:
            with _urlopen_resilient(req, timeout=8) as resp:
                raw = resp.read().decode("utf-8", errors="replace").strip()
        except urllib.error.HTTPError as he:
            raw = he.read().decode("utf-8", errors="replace").strip()
        if not raw.startswith("{"):
            return {"error": -1, "message": f"HTTP non-JSON ({raw[:48]})"}
        return json.loads(raw)

    def run(self):
        import webbrowser, urllib.parse
        key = LFM_API_KEY.strip()
        sec = LFM_API_SECRET.strip()
        if self.action == "browser_auth":
            if not key or not sec:
                self.status_msg.emit("Enter Last.fm API Key & Secret first.")
                return
            try:
                tok_res = self._call_lfm({"method": "auth.getToken", "api_key": key}, sec, post=False)
                token = tok_res.get("token", "")
                if not token:
                    self.status_msg.emit(f"Last.fm error: {tok_res.get('message', 'No token')}")
                    return
                auth_url = urllib.parse.urlunparse(("https", "www.last.fm", "/api/auth/", "", f"api_key={key}&token={token}", ""))
                webbrowser.open(auth_url)
                self.status_msg.emit("Approve REXA in your browser...")
                for _ in range(48):
                    if self.isInterruptionRequested():
                        return
                    self.msleep(2500)
                    sess_res = self._call_lfm({"method": "auth.getSession", "api_key": key, "token": token}, sec, post=False)
                    sess = sess_res.get("session")
                    if sess and sess.get("key"):
                        self.status_msg.emit(f"LFM_OK:{sess.get('name', '')}:{sess['key']}")
                        return
                    if sess_res.get("error") not in (None, 14):
                        self.status_msg.emit(f"Last.fm error: {sess_res.get('message', 'Auth failed')}")
                        return
                self.status_msg.emit("Browser approval timed out — click Connect again.")
            except Exception as e:
                self.status_msg.emit(f"Last.fm error: {e}")
            return

        sk = (self.cfg.get("lfm_sk") or "").strip()
        artist = (self.meta.get("artist") or "").strip()
        title = (self.meta.get("title") or "").strip()
        if not (self.cfg.get("lfm_enabled") and key and sec and sk and artist and title) or artist == "Unknown Artist":
            return
        try:
            params = {"method": "track.updateNowPlaying" if self.action == "now_playing" else "track.scrobble", "api_key": key, "sk": sk, "artist": artist, "track": title}
            album = (self.meta.get("album") or "").strip()
            dur = int(self.meta.get("duration") or 0)
            if album: params["album"] = album
            if dur > 0: params["duration"] = str(dur)
            if self.action == "scrobble": params["timestamp"] = str(int(self.meta.get("timestamp") or time.time()))
            res = self._call_lfm(params, sec, post=True)
            if "error" in res:
                self.status_msg.emit(f"Last.fm error {res.get('error')}: {res.get('message', '')}")
            elif self.action == "scrobble":
                self.status_msg.emit(f"Scrobbled ✓ {artist} — {title}")
            elif self.action == "now_playing":
                self.status_msg.emit(f"Now Playing ♪ {artist} — {title}")
        except Exception as e:
            self.status_msg.emit(f"Scrobble error: {e}")

class UpdateCheckThread(QThread):
    result_ready = Signal(bool, str, str, str)  # has_update, latest_tag, asset_url, release_notes

    def run(self):
        import json, urllib.request
        try:
            req = urllib.request.Request(
                GITHUB_RELEASES_API,
                headers={"User-Agent": f"REXA-Player/{APP_VERSION}", "Accept": "application/vnd.github+json"}
            )
            with _urlopen_resilient(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            tag = data.get("tag_name", "")
            notes = (data.get("body") or "").strip()
            assets = data.get("assets", [])
            want_name = "REXA-macOS-AppleSilicon.dmg" if sys.platform == "darwin" else "REXA-Windows.zip"
            dl_url = ""
            for a in assets:
                if a.get("name") == want_name:
                    dl_url = a.get("browser_download_url", "")
                    break
            if not dl_url:
                dl_url = data.get("html_url", "https://github.com/RestleSSOtaKU/REXA-Player/releases/latest")
            has_upd = _parse_ver(tag) > _parse_ver(APP_VERSION)
            self.result_ready.emit(has_upd, tag or f"v{APP_VERSION}", dl_url, notes)
        except Exception as e:
            self.result_ready.emit(False, f"Error: {e}", "", "")

class UpdateInstallThread(QThread):
    progress = Signal(int)
    finished_install = Signal(bool, str)

    def __init__(self, url, parent=None):
        super().__init__(parent)
        self.url = url

    def run(self):
        import urllib.request, tempfile, zipfile, shutil
        try:
            dl_dir = os.path.join(tempfile.gettempdir(), "rexa_update")
            os.makedirs(dl_dir, exist_ok=True)
            fname = os.path.basename(self.url.split("?")[0]) or ("REXA.dmg" if sys.platform == "darwin" else "REXA-Windows.zip")
            dest = os.path.join(dl_dir, fname)

            req = urllib.request.Request(self.url, headers={"User-Agent": f"REXA-Player/{APP_VERSION}"})
            with _urlopen_resilient(req, timeout=30) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                read = 0
                with open(dest, "wb") as f:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        read += len(chunk)
                        if total > 0:
                            self.progress.emit(min(99, int(read * 100 / total)))

            self.progress.emit(100)
            if sys.platform == "darwin" and dest.endswith(".dmg"):
                mnt = os.path.join(dl_dir, "mnt")
                os.makedirs(mnt, exist_ok=True)
                subprocess.run(["hdiutil", "detach", mnt, "-quiet"], check=False)
                res = subprocess.run(["hdiutil", "attach", dest, "-nobrowse", "-quiet", "-mountpoint", mnt], capture_output=True)
                new_app = os.path.join(mnt, "REXA.app")
                target_app = ""
                if getattr(sys, "frozen", False) and ".app/Contents/" in sys.executable:
                    target_app = sys.executable.split(".app/Contents/")[0] + ".app"
                elif os.path.isdir("/Applications/REXA.app"):
                    target_app = "/Applications/REXA.app"

                if res.returncode == 0 and os.path.isdir(new_app) and target_app and os.access(os.path.dirname(target_app), os.W_OK):
                    sh_script = os.path.join(dl_dir, "apply_update.sh")
                    with open(sh_script, "w", encoding="utf-8") as sf:
                        sf.write(f'#!/bin/sh\nsleep 1\nrm -rf "{target_app}"\nditto "{new_app}" "{target_app}"\nxattr -cr "{target_app}" 2>/dev/null || true\nhdiutil detach "{mnt}" -quiet\nopen -n "{target_app}"\n')
                    os.chmod(sh_script, 0o755)
                    subprocess.Popen(["/bin/sh", sh_script], start_new_session=True)
                    self.finished_install.emit(True, "RESTART_NOW")
                else:
                    subprocess.run(["hdiutil", "detach", mnt, "-quiet"], check=False)
                    subprocess.Popen(["open", dest])
                    self.finished_install.emit(True, "Downloaded update image! Drag REXA.app to Applications to finish updating.")
            elif sys.platform == "win32" and dest.endswith(".zip") and getattr(sys, "frozen", False):
                ext_dir = os.path.join(dl_dir, "extracted")
                if os.path.exists(ext_dir):
                    shutil.rmtree(ext_dir, ignore_errors=True)
                with zipfile.ZipFile(dest, "r") as zf:
                    zf.extractall(ext_dir)
                src_dir = ext_dir
                sub = os.path.join(ext_dir, "REXA")
                if os.path.isdir(sub):
                    src_dir = sub
                app_dir = os.path.dirname(os.path.abspath(sys.executable))
                bat = os.path.join(dl_dir, "apply_update.bat")
                with open(bat, "w", encoding="utf-8") as bf:
                    bf.write(f'@echo off\ntimeout /t 2 /nobreak >nul\nxcopy /E /Y /I "{src_dir}\\*" "{app_dir}\\" >nul\nstart "" "{sys.executable}"\n')
                subprocess.Popen(["cmd", "/c", bat], creationflags=0x08000000)
                self.finished_install.emit(True, "RESTART_NOW")
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "explorer", dl_dir])
                self.finished_install.emit(True, f"Downloaded update to {dest}")
        except Exception as e:
            self.finished_install.emit(False, str(e))

class StereoAnalyzerWidget(QWidget):
    def __init__(self, mw, parent=None):
        super().__init__(parent)
        self.mw = mw
        self.setMinimumWidth(190)
        self.setMaximumWidth(230)
        self._corr = 1.0
        self._pts = None

    def update_from_stereo(self, stereo):
        if stereo is None or len(stereo) < 16 or stereo.shape[1] < 2:
            return
        L = stereo[:, 0]
        R = stereo[:, 1]
        denom = float(np.sqrt(np.sum(L * L) * np.sum(R * R)))
        if denom > 1e-9:
            c = float(np.sum(L * R) / denom)
            self._corr = 0.75 * self._corr + 0.25 * max(-1.0, min(1.0, c))
        step = max(1, len(L) // 256)
        ls, rs = L[::step], R[::step]
        mid = (ls + rs) * 0.7071
        side = (rs - ls) * 0.7071
        self._pts = (side, mid)
        self.update()

    def paintEvent(self, event):
        from PySide6.QtGui import QPen, QFont, QPolygonF
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = float(self.width()), float(self.height())
        is_light = is_light_surface(
            getattr(self.mw, "current_theme", "classic"),
            getattr(self.mw, "glass_appearance", "dark"))

        fg = QColor("#161618" if is_light else "#f2f4f8")
        sub = QColor(0, 0, 0, 45) if is_light else QColor(255, 255, 255, 42)
        accent = QColor(getattr(self.mw, "current_accent", "#0a84ff"))

        p.setPen(fg)
        p.setFont(QFont(".AppleSystemUIFont", 11, QFont.Bold))
        p.drawText(8, 16, "Stereo & Phase")

        bar_h = 26.0
        scope_top = 22.0
        scope_h = max(60.0, h - scope_top - bar_h - 8.0)
        cx = w * 0.5
        cy = scope_top + scope_h * 0.5
        rad = min(w * 0.42, scope_h * 0.46)

        p.setPen(QPen(sub, 1.0))
        p.drawEllipse(QPointF(cx, cy), rad, rad)
        p.drawEllipse(QPointF(cx, cy), rad * 0.5, rad * 0.5)
        d = rad * 0.7071
        p.drawLine(QPointF(cx - d, cy - d), QPointF(cx + d, cy + d))
        p.drawLine(QPointF(cx + d, cy - d), QPointF(cx - d, cy + d))

        p.setFont(QFont(".AppleSystemUIFont", 9, QFont.Bold))
        p.setPen(QColor("#52525b" if is_light else "#9ca3af"))
        p.drawText(int(cx - rad - 4), int(cy - rad + 8), "L")
        p.drawText(int(cx + rad - 4), int(cy - rad + 8), "R")
        p.drawText(int(cx - 4), int(cy - rad - 2), "+M")

        if self._pts is not None:
            side, mid = self._pts
            peak = max(0.05, float(np.max(np.abs(mid))), float(np.max(np.abs(side))))
            scale = (rad * 0.92) / peak
            poly = QPolygonF()
            for sx, my in zip(side, mid):
                px = max(cx - rad, min(cx + rad, cx + float(sx) * scale))
                py = max(cy - rad, min(cy + rad, cy - float(my) * scale))
                poly.append(QPointF(px, py))
            wave_c = QColor(accent)
            wave_c.setAlpha(210)
            p.setPen(QPen(wave_c, 1.5))
            p.drawPolyline(poly)

        by = h - bar_h
        bx, bw, bh = 10.0, w - 20.0, 7.0
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 26) if is_light else QColor(255, 255, 255, 28))
        p.drawRoundedRect(int(bx), int(by), int(bw), int(bh), 3.5, 3.5)

        mid_x = bx + bw * 0.5
        p.setPen(QPen(sub, 1.0))
        p.drawLine(int(mid_x), int(by - 2), int(mid_x), int(by + bh + 2))

        c_val = self._corr
        if c_val >= 0.25:
            bar_c = QColor("#30d158")
        elif c_val >= 0.0:
            bar_c = QColor("#ff9f0a")
        else:
            bar_c = QColor("#ff3b30")
        p.setPen(Qt.NoPen)
        p.setBrush(bar_c)
        fill_w = abs(c_val) * (bw * 0.5)
        fill_x = mid_x if c_val >= 0 else (mid_x - fill_w)
        p.drawRoundedRect(int(fill_x), int(by), max(3, int(fill_w)), int(bh), 3.0, 3.0)

        p.setPen(fg)
        p.setFont(QFont(".AppleSystemUIFont", 10, QFont.Bold))
        p.drawText(int(bx), int(by + bh + 12), "-1")
        p.drawText(int(mid_x - 4), int(by + bh + 12), "0")
        p.drawText(int(bx + bw - 14), int(by + bh + 12), "+1")
        p.setPen(bar_c)
        p.drawText(int(bx + bw - 48), int(by - 4), f"{c_val:+.2f}")
        p.end()

class PeakSchemeWidget(QWidget):
    def __init__(self, mw, parent=None):
        super().__init__(parent)
        self.mw = mw
        from PySide6.QtWidgets import QSizePolicy
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(96)

    def paintEvent(self, event):
        from PySide6.QtGui import QPen, QPainterPath, QFont
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = float(self.width()), float(self.height())
        is_light = is_light_surface(
            getattr(self.mw, "current_theme", "classic"),
            getattr(self.mw, "glass_appearance", "dark"))
        is_float = bool(self.mw.engine.float_mode)

        # Background box
        p.setPen(QPen(QColor(0, 0, 0, 25) if is_light else QColor(255, 255, 255, 32), 1))
        p.setBrush(QColor(255, 255, 255, 246) if is_light else QColor(20, 23, 31, 244))
        p.drawRoundedRect(1, 1, int(w - 2), int(h - 2), 10, 10)

        mid_y = h * 0.78
        ceil_y = h * 0.36  # 0.0 dBFS ceiling line

        # 0 dBFS Ceiling Line
        ceil_pen = QPen(QColor("#ff3b30"), 1.2, Qt.DashLine)
        p.setPen(ceil_pen)
        p.drawLine(int(12), int(ceil_y), int(w - 12), int(ceil_y))
        p.setFont(QFont(".AppleSystemUIFont", 10, QFont.Bold))
        p.drawText(int(w - 76), int(ceil_y - 4), "0 dBFS Limit")

        # Draw transient peak wave (left = Hard Clip, right = 32-bit Float)
        def draw_peak(cx, span_w, clamped, active, color_hex, title, sub):
            path = QPainterPath()
            steps = 60
            peak_h = (mid_y - ceil_y) * 1.48  # Overshoots 0 dBFS by ~3.4 dB
            for i in range(steps + 1):
                t = (i / steps) * 2.0 - 1.0
                x = cx + t * (span_w * 0.42)
                env = max(0.0, np.cos(t * np.pi * 0.5)) ** 1.6
                y = mid_y - env * peak_h
                if clamped and y < ceil_y + 2:
                    y = ceil_y + 2  # Soft/hard ceiling clamp at -0.5 dBFS
                if i == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)

            c = QColor(color_hex)
            c.setAlpha(255 if active else 95)
            p.setPen(QPen(c, 2.6 if active else 1.6))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)

            txt_c = QColor("#161618" if is_light else "#f2f4f8")
            txt_c.setAlpha(255 if active else 130)
            p.setPen(txt_c)
            p.setFont(QFont(".AppleSystemUIFont", 11, QFont.Bold if active else QFont.Normal))
            p.drawText(int(cx - span_w * 0.46), int(h - 16), title)
            p.setFont(QFont(".AppleSystemUIFont", 10))
            p.drawText(int(cx - span_w * 0.46), int(15), sub)

        draw_peak(w * 0.27, w * 0.46, True, not is_float, "#ff9500",
                  "Hard Clip: Tight & DAC-Safe", "Clamped at -0.5 dBFS")
        draw_peak(w * 0.74, w * 0.46, False, is_float, "#30d158",
                  "32-bit Float: Open Breathing", "Unclamped > 0 dBFS")
        p.end()

class FloatSchemeButton(RightClickPushButton):
    def __init__(self, text, mw, parent=None):
        super().__init__(text, parent)
        self.mw = mw
        self._popup = None
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(700)
        self._hover_timer.timeout.connect(self._show_popup)

    def _show_popup(self):
        if not self.underMouse():
            return
        if self._popup is None:
            self._popup = QWidget(None, Qt.ToolTip | Qt.FramelessWindowHint)
            self._popup.setAttribute(Qt.WA_TranslucentBackground, True)
            lay = QVBoxLayout(self._popup)
            lay.setContentsMargins(0, 0, 0, 0)
            self._scheme = PeakSchemeWidget(self.mw)
            self._scheme.setFixedSize(410, 96)
            lay.addWidget(self._scheme)
        self._scheme.update()
        pos = self.mapToGlobal(self.rect().topLeft())
        self._popup.move(max(10, pos.x() - 140), max(10, pos.y() - 104))
        self._popup.show()

    def enterEvent(self, event):
        self._hover_timer.start()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover_timer.stop()
        if self._popup is not None:
            self._popup.hide()
        super().leaveEvent(event)

class SettingsDialog(QDialog):
    def __init__(self, mw):
        super().__init__(mw)
        self.mw = mw
        muted = muted_text_color(getattr(mw, "current_theme", "classic"),
                                 getattr(mw, "glass_appearance", "dark"))
        self._glass_id = None
        self.setWindowTitle("Settings")
        self.setAttribute(Qt.WA_TranslucentBackground, windows_transparency_enabled())
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_ContentsMarginsRespectsSafeArea, False)
        self.setMinimumSize(540, 420)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        # Keep the dialog capped to the screen and scroll its contents instead
        # of clipping the lower cards on short displays.
        self.settings_scroll = QScrollArea(self)
        self.settings_scroll.setObjectName("settingsScroll")
        self.settings_scroll.setFrameShape(QScrollArea.NoFrame)
        self.settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.settings_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        translucent = windows_transparency_enabled()
        self.settings_scroll.setAttribute(Qt.WA_TranslucentBackground, translucent)
        self.settings_scroll.viewport().setAttribute(Qt.WA_TranslucentBackground, translucent)
        self.settings_scroll.viewport().setAttribute(Qt.WA_NoSystemBackground, translucent)
        outer.addWidget(self.settings_scroll)

        root = QFrame(self.settings_scroll)
        root.setObjectName("settingsRoot")
        self.settings_scroll.setWidget(root)
        self.settings_scroll.verticalScrollBar().valueChanged.connect(
            lambda *_: schedule_macos_panel_glass_sync(self)
        )

        lay = QVBoxLayout(root)
        lay.setContentsMargins(18, 40 if sys.platform == "darwin" else 18, 18, 18)
        lay.setSpacing(16)

        app_card = make_glass_card()
        g = QGridLayout(app_card)
        g.setContentsMargins(18, 16, 18, 16)
        g.setHorizontalSpacing(16)
        g.setVerticalSpacing(14)
        g.setColumnStretch(1, 1)

        hdr1 = QLabel("Appearance")
        hdr1.setStyleSheet("font-weight: bold; font-size: 14px;")
        g.addWidget(hdr1, 0, 0, 1, 2)

        g.addWidget(QLabel("Theme:"), 1, 0)
        self.combo_theme = QComboBox()
        self.combo_theme.addItems([THEME_LABELS[t] for t in THEME_ORDER])

        self.combo_theme.currentIndexChanged.connect(self._on_theme)
        g.addWidget(self.combo_theme, 1, 1)

        g.addWidget(QLabel("Background:"), 2, 0)
        self.combo_bg_mode = QComboBox()
        self.combo_bg_mode.addItems(["Dynamic (Lava Lamp)", "Static"])
        self.combo_bg_mode.setToolTip(
            "Lava drifts at 30 FPS idle and a lighter 15 FPS during playback so Classic/Glass keep full graph FPS; ambient colors stay live."
        )
        self.combo_bg_mode.currentIndexChanged.connect(lambda i: self.mw.toggle_dynamic_ambient(i == 0))
        g.addWidget(self.combo_bg_mode, 2, 1)

        g.addWidget(QLabel("Ambient Glow:"), 3, 0)
        self.s_glow = ResetSlider(30, Qt.Horizontal)
        self.s_glow.setRange(0, 100)
        self.s_glow.valueChanged.connect(mw.update_ambient_opacity)
        g.addWidget(self.s_glow, 3, 1)

        self.lbl_stage = SystemSyncLabel("Glass Stage:")
        self.lbl_stage.setCursor(Qt.PointingHandCursor)
        self._glass_sync_tip = "Double-click to sync Dark/Light from macOS/Windows system appearance."
        self.lbl_stage.setToolTip(self._glass_sync_tip)
        self.lbl_stage.systemSyncRequested.connect(self._sync_glass_from_system)
        g.addWidget(self.lbl_stage, 4, 0)
        self.lbl_trans = self.lbl_stage  # legacy name used by older smoke tests
        self.combo_glass_stage = QComboBox()
        self.combo_glass_stage.addItems([GLASS_STAGE_LABELS[s] for s in GLASS_STAGES])
        for i, stage in enumerate(GLASS_STAGES):
            self.combo_glass_stage.setItemData(i, stage)
        self.combo_glass_stage.setToolTip(
            "Dark = DarkAqua + dark tint/scrim; Light = Aqua + white tint/scrim. "
            "Double-click the label to sync from the system."
        )
        self.combo_glass_stage.currentIndexChanged.connect(self._on_glass_stage)
        g.addWidget(self.combo_glass_stage, 4, 1)

        self.lbl_rim = QLabel("Glass Border Rim:")
        g.addWidget(self.lbl_rim, 5, 0)
        self.s_rim = ResetSlider(DEFAULT_GLASS_RIM, Qt.Horizontal)
        self.s_rim.setRange(0, 100)
        self.s_rim.valueChanged.connect(self._on_glass)
        g.addWidget(self.s_rim, 5, 1)

        self.lbl_glass_tint = QLabel("Native Tint:")
        self.lbl_glass_tint.setToolTip("Glass 2.0 only.")
        g.addWidget(self.lbl_glass_tint, 6, 0)
        self.s_glass_tint = ResetSlider(34, Qt.Horizontal)
        self.s_glass_tint.setRange(0, 100)
        self.s_glass_tint.valueChanged.connect(self._on_glass_tuning)
        g.addWidget(self.s_glass_tint, 6, 1)

        self.lbl_glass_scrim = QLabel("Panel Scrim:")
        self.lbl_glass_scrim.setToolTip("Glass 2.0 only.")
        g.addWidget(self.lbl_glass_scrim, 7, 0)
        self.s_glass_scrim = ResetSlider(_alpha_to_scrim_slider(174), Qt.Horizontal)
        self.s_glass_scrim.setRange(_SCRIM_SLIDER_MIN, _SCRIM_SLIDER_MAX)
        self.s_glass_scrim.setToolTip(
            "50% leaves panels transparent; 100% is a full scrim over the native glass."
        )
        self.s_glass_scrim.valueChanged.connect(self._on_glass_tuning)
        g.addWidget(self.s_glass_scrim, 7, 1)

        self.lbl_glass_style = QLabel("Native Style:")
        self.lbl_glass_style.setToolTip("Glass 2.0 only.")
        g.addWidget(self.lbl_glass_style, 8, 0)
        self.combo_glass_style = QComboBox()
        self.combo_glass_style.addItems(["Clear", "Regular"])
        self.combo_glass_style.setItemData(0, "clear")
        self.combo_glass_style.setItemData(1, "regular")
        self.combo_glass_style.setToolTip(
            "Public NSGlassEffectView style. Regular is softer but can smear."
        )
        self.combo_glass_style.currentIndexChanged.connect(self._on_glass_tuning)
        g.addWidget(self.combo_glass_style, 8, 1)

        self.btn_reset_glass = QPushButton("Reset Stage Tuning")
        self.btn_reset_glass.clicked.connect(self._reset_glass_tuning)
        g.addWidget(self.btn_reset_glass, 9, 1)
        lay.addWidget(app_card)

        perf_card = make_glass_card()
        p_lay = QVBoxLayout(perf_card)
        p_lay.setContentsMargins(18, 16, 18, 16)
        p_lay.setSpacing(11)
        hdr2 = QLabel("Engine & Future Modules")
        hdr2.setStyleSheet("font-weight: bold; font-size: 14px;")
        p_lay.addWidget(hdr2)

        self.chk_low_cpu = QCheckBox("Low-End CPU Mode (Faster startup & 10 FPS graph)")
        self.chk_low_cpu.toggled.connect(mw.toggle_cpu_mode)
        p_lay.addWidget(self.chk_low_cpu)

        self.chk_spec_fill = QCheckBox("Spectrum Gradient Fill (Disable for max Full-Screen FPS)")
        self.chk_spec_fill.toggled.connect(mw.toggle_spectrum_fill)
        p_lay.addWidget(self.chk_spec_fill)

        self.lbl_perf_status = QLabel(getattr(mw, "_graph_perf_text", "Perf: idle"))
        self.lbl_perf_status.setStyleSheet(f"font-size: 12px; font-weight: bold; color: {muted};")
        self.lbl_perf_status.setWordWrap(True)
        self.lbl_perf_status.setToolTip(getattr(mw, "_graph_perf_tip", "Start playback to measure real graph FPS."))
        p_lay.addWidget(self.lbl_perf_status)

        self.lbl_perf_tips = QLabel(getattr(mw, "_graph_perf_tip", "Start playback to measure real graph FPS."))
        self.lbl_perf_tips.setStyleSheet(f"font-size: 11px; color: {muted};")
        self.lbl_perf_tips.setWordWrap(True)
        p_lay.addWidget(self.lbl_perf_tips)

        upd_row = QHBoxLayout()
        self.lbl_upd = QLabel(f"REXA v{APP_VERSION}")
        self.lbl_upd.setStyleSheet(f"font-size: 11px; color: {muted};")
        self.btn_check_upd = QPushButton("Check for Updates")
        self.btn_check_upd.clicked.connect(self._on_check_updates)
        upd_row.addWidget(self.lbl_upd, 1)
        upd_row.addWidget(self.btn_check_upd)
        p_lay.addLayout(upd_row)

        scrob_card = make_glass_card()
        s_lay = QVBoxLayout(scrob_card)
        s_lay.setContentsMargins(18, 16, 18, 16)
        s_lay.setSpacing(12)
        s_hdr = QLabel("Last.fm Scrobbling")
        s_hdr.setStyleSheet("font-weight: bold; font-size: 14px;")
        s_lay.addWidget(s_hdr)

        act_row = QHBoxLayout()
        self.chk_lfm = QCheckBox("Enable Scrobbling")
        self.btn_lfm_connect = QPushButton("Connect with Browser")
        self.btn_lfm_connect.clicked.connect(self._on_lfm_browser_connect)
        act_row.addWidget(self.chk_lfm)
        act_row.addStretch()
        act_row.addWidget(self.btn_lfm_connect)
        s_lay.addLayout(act_row)

        scrob_grid = QGridLayout()
        scrob_grid.setHorizontalSpacing(14)
        scrob_grid.setVerticalSpacing(11)
        scrob_grid.setColumnStretch(1, 1)

        scrob_grid.addWidget(QLabel("Scrobble tracks longer than:"), 0, 0)
        self.s_scrob_min = ResetSlider(6, Qt.Horizontal)  # 6 * 5 = 30s
        self.s_scrob_min.setRange(2, 12)  # 00:10..01:00 (5s step)
        self.lbl_scrob_min = QLabel("00:30")
        self.lbl_scrob_min.setFixedWidth(46)
        self.lbl_scrob_min.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        scrob_grid.addWidget(self.s_scrob_min, 0, 1)
        scrob_grid.addWidget(self.lbl_scrob_min, 0, 2)

        self.lbl_delay_hdr = QLabel("Scrobble delay (whichever comes first)")
        self.lbl_delay_hdr.setStyleSheet(f"font-size: 11px; font-weight: bold; color: {muted};")
        self.lbl_delay_hdr.setMinimumHeight(20)
        scrob_grid.addWidget(self.lbl_delay_hdr, 1, 0, 1, 3)

        scrob_grid.addWidget(QLabel("Percentage:"), 2, 0)
        self.s_scrob_pct = ResetSlider(50, Qt.Horizontal)
        self.s_scrob_pct.setRange(30, 90)  # 30%..90% (1% step)
        self.lbl_scrob_pct = QLabel("50%")
        self.lbl_scrob_pct.setFixedWidth(46)
        self.lbl_scrob_pct.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        scrob_grid.addWidget(self.s_scrob_pct, 2, 1)
        scrob_grid.addWidget(self.lbl_scrob_pct, 2, 2)

        scrob_grid.addWidget(QLabel("Minutes:"), 3, 0)
        self.s_scrob_max = ResetSlider(18, Qt.Horizontal)  # 18 * 10 = 180s (03:00)
        self.s_scrob_max.setRange(3, 36)  # 00:30..06:00 (10s step)
        self.lbl_scrob_max = QLabel("03:00")
        self.lbl_scrob_max.setFixedWidth(46)
        self.lbl_scrob_max.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        scrob_grid.addWidget(self.s_scrob_max, 3, 1)
        scrob_grid.addWidget(self.lbl_scrob_max, 3, 2)

        for sl in (self.s_scrob_min, self.s_scrob_pct, self.s_scrob_max):
            sl.valueChanged.connect(self._save_scrobble_ui)
        s_lay.addLayout(scrob_grid)

        self.scrob_progress = QProgressBar()
        self.scrob_progress.setRange(0, 100)
        self.scrob_progress.setValue(0)
        self.scrob_progress.setFixedHeight(6)
        self.scrob_progress.setTextVisible(False)
        s_lay.addWidget(self.scrob_progress)

        self.lbl_scrob_status = QLabel("Not connected")
        self.lbl_scrob_status.setStyleSheet(f"font-size: 11px; color: {muted};")
        s_lay.addWidget(self.lbl_scrob_status)

        self.chk_lfm.toggled.connect(self._save_scrobble_ui)
        lay.addWidget(scrob_card)

        self.lbl_note = QLabel("Layout presets coming soon.")
        self.lbl_note.setWordWrap(True)
        self.lbl_note.setStyleSheet(f"font-size: 12px; color: {muted};")
        p_lay.addWidget(self.lbl_note)
        lay.addWidget(perf_card)
        lay.addStretch()
        self.sync_from_main()

    def _clear_native_glass(self):
        if glass and getattr(self, "_glass_id", None) is not None:
            try:
                glass.remove_glass_effect(self._glass_id)
            except Exception:
                pass
            self._glass_id = None
        clear_macos_glass_state(self)
        remove_macos_panel_glass(self)

    def showEvent(self, event):
        super().showEvent(event)
        # AppKit drops the dialog's native glass views while it is hidden; the
        # cached effect id/container are stale on the next open. Rebuild both
        # layers before sync_from_main() reconfigures them.
        self._clear_native_glass()
        self.sync_from_main()
        self._fit_to_screen()

    def hideEvent(self, event):
        super().hideEvent(event)
        # Dialog-owned NSGlassEffectViews used to survive close(). The next
        # activation re-composited them over the main window, which is why the
        # first Settings close darkened the panels. Drop them on hide so every
        # open/close cycle starts and ends clean.
        self._clear_native_glass()

    def _fit_to_screen(self):
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        avail = screen.availableGeometry()
        max_w = max(self.minimumWidth(), min(700, avail.width() - 40))
        max_h = max(self.minimumHeight(), avail.height() - 80)
        self.setMaximumSize(max_w, max_h)
        if not getattr(self, "_initial_size_applied", False):
            hint = self.sizeHint()
            self.resize(max(self.minimumWidth(), min(hint.width(), max_w)),
                        max(self.minimumHeight(), min(hint.height(), max_h)))
            self._initial_size_applied = True
        else:
            self.resize(min(self.width(), max_w), min(self.height(), max_h))

    def _on_check_updates(self):
        self.btn_check_upd.setEnabled(False)
        self.lbl_upd.setText("Checking GitHub Releases...")
        self._chk_thr = UpdateCheckThread(self)
        self._chk_thr.result_ready.connect(self._on_update_checked)
        self._chk_thr.start()

    def _on_update_checked(self, has_upd, tag, dl_url, notes):
        self.btn_check_upd.setEnabled(True)
        if tag.startswith("Error:"):
            self.lbl_upd.setText(tag[:60])
            return
        if not has_upd:
            self.lbl_upd.setText(f"Up to date (v{APP_VERSION})")
            return
        self.lbl_upd.setText(f"Update available: {tag}!")
        reply = QMessageBox.question(
            self, "Update Available",
            f"A newer version of REXA ({tag}) is available (current: v{APP_VERSION}).\n\nDownload and install now?",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            self.btn_check_upd.setEnabled(False)
            self._dl_thr = UpdateInstallThread(dl_url, self)
            self._dl_thr.progress.connect(lambda p: self.lbl_upd.setText(f"Downloading {tag}... {p}%"))
            self._dl_thr.finished_install.connect(self._on_update_installed)
            self._dl_thr.start()

    def _on_update_installed(self, ok, msg):
        self.btn_check_upd.setEnabled(True)
        if not ok:
            self.lbl_upd.setText("Update failed")
            QMessageBox.warning(self, "Update Error", f"Failed to download update:\n{msg}")
            return
        if msg == "RESTART_NOW":
            QApplication.quit()
        else:
            self.lbl_upd.setText("Update ready!")
            QMessageBox.information(self, "Update Ready", msg)

    def _save_scrobble_ui(self, *_):
        cfg = getattr(self.mw, "scrobble_cfg", {})
        min_s = self.s_scrob_min.value() * 5
        pct_v = self.s_scrob_pct.value()
        max_s = self.s_scrob_max.value() * 10
        self.lbl_scrob_min.setText(f"{min_s // 60:02d}:{min_s % 60:02d}")
        self.lbl_scrob_pct.setText(f"{pct_v}%")
        self.lbl_scrob_max.setText(f"{max_s // 60:02d}:{max_s % 60:02d}")
        cfg["lfm_enabled"] = self.chk_lfm.isChecked()
        cfg["min_track_sec"] = min_s
        cfg["scrobble_pct"] = pct_v
        cfg["max_time_sec"] = max_s
        self.mw.scrobble_cfg = cfg
        self.mw.save_session_state()

    def _on_lfm_browser_connect(self):
        self._save_scrobble_ui()
        if getattr(self, "_auth_w", None) and self._auth_w.isRunning():
            self._auth_w.requestInterruption()
            self._auth_w.wait(500)
        self.btn_lfm_connect.setEnabled(False)
        self.lbl_scrob_status.setText("Requesting Last.fm token...")
        self._auth_w = ScrobbleWorker(self.mw.scrobble_cfg, "browser_auth", parent=self)
        self._auth_w.status_msg.connect(self._on_lfm_auth_msg)
        self._auth_w.finished.connect(lambda: self.btn_lfm_connect.setEnabled(True))
        self._auth_w.start()

    def _on_lfm_auth_msg(self, msg):
        if msg.startswith("LFM_OK:"):
            _, uname, sk = msg.split(":", 2)
            self.mw.scrobble_cfg.update({"lfm_user": uname, "lfm_sk": sk, "lfm_enabled": True})
            self.chk_lfm.setChecked(True)
            self.lbl_scrob_status.setText(f"Connected as {uname or 'Last.fm user'}!")
            self.mw.save_session_state()
        else:
            self.lbl_scrob_status.setText(msg[:72])

    def _refresh_muted_styles(self):
        muted = muted_text_color(getattr(self.mw, "current_theme", "classic"),
                                 getattr(self.mw, "glass_appearance", "dark"))
        styles = (
            (self.lbl_perf_tips, "font-size: 11px;"),
            (self.lbl_upd, "font-size: 11px;"),
            (self.lbl_delay_hdr, "font-size: 11px; font-weight: bold;"),
            (self.lbl_scrob_status, "font-size: 11px;"),
            (self.lbl_note, "font-size: 12px;"),
        )
        for label, base in styles:
            label.setStyleSheet(f"{base} color: {muted};")

    def update_graph_perf(self, text, tip, color):
        if hasattr(self, "lbl_perf_status"):
            self.lbl_perf_status.setText(text)
            self.lbl_perf_status.setStyleSheet(
                f"font-size: 12px; font-weight: bold; color: {color};"
            )
            self.lbl_perf_status.setToolTip(tip)
        if hasattr(self, "lbl_perf_tips"):
            self.lbl_perf_tips.setText(tip)

    def sync_from_main(self):
        idx_map = {t: i for i, t in enumerate(THEME_ORDER)}
        self.combo_theme.blockSignals(True)
        self.combo_theme.setCurrentIndex(idx_map.get(self.mw.current_theme, 0))
        self.combo_bg_mode.blockSignals(True)
        self.combo_bg_mode.setCurrentIndex(0 if getattr(self.mw, 'dynamic_ambient', True) else 1)
        self.combo_bg_mode.blockSignals(False)
        self.combo_theme.blockSignals(False)
        for s, val in ((self.s_glow, int(self.mw.ambient_opacity * 100)),
                       (self.s_rim, self.mw.glass_rim)):
            s.blockSignals(True); s.setValue(val); s.blockSignals(False)
        # Glass Stage is the only appearance control; tuning is per stage.
        self.glass_appearance = getattr(self.mw, "glass_appearance", "dark")
        if self.glass_appearance not in GLASS_STAGES:
            self.glass_appearance = "dark"
        self.mw.glass_appearance = self.glass_appearance
        self.mw.glass_material = GLASS_STAGE_MATERIAL[self.glass_appearance]
        self.glass_overrides = getattr(self.mw, "glass_overrides", {})
        stage = self.glass_appearance
        self.combo_glass_stage.blockSignals(True)
        self.combo_glass_stage.setCurrentIndex(GLASS_STAGES.index(stage))
        self.combo_glass_stage.blockSignals(False)
        material_key = GLASS_STAGE_MATERIAL[stage]
        per = normalize_glass_overrides((self.glass_overrides or {}).get(stage), stage)
        profile = glass_material_profile(
            material_key, getattr(self.mw, "glass_clarity", 45) / 100.0, per, stage
        )
        defaults = glass_material_profile(
            material_key, getattr(self.mw, "glass_clarity", 45) / 100.0, stage=stage
        )
        tint_max = int(round(GLASS_STAGE_TINT_MAX.get(stage, 1.0) * 100))
        self.s_glass_tint.blockSignals(True)
        self.s_glass_tint.setRange(0, tint_max)
        self.s_glass_tint.blockSignals(False)
        tuning = {
            self.s_glass_tint: int(round(profile["tint_alpha"] * 100)),
            self.s_glass_scrim: _alpha_to_scrim_slider(profile["card_alpha"]),
        }
        default_tuning = {
            self.s_glass_tint: int(round(defaults["tint_alpha"] * 100)),
            self.s_glass_scrim: _alpha_to_scrim_slider(defaults["card_alpha"]),
        }
        for slider, val in tuning.items():
            slider.default_value = default_tuning[slider]
            slider.blockSignals(True); slider.setValue(val); slider.blockSignals(False)
        style_idx = 1 if profile.get("style") == "regular" else 0
        self.combo_glass_style.blockSignals(True)
        self.combo_glass_style.setCurrentIndex(style_idx)
        self.combo_glass_style.blockSignals(False)
        tuning_enabled = self.mw.current_theme == "macos27_glass"
        for widget in (self.lbl_stage, self.combo_glass_stage,
                       self.lbl_glass_tint, self.s_glass_tint,
                       self.lbl_glass_scrim, self.s_glass_scrim,
                       self.lbl_glass_style, self.combo_glass_style,
                       self.btn_reset_glass):
            widget.setEnabled(tuning_enabled)

        self.chk_low_cpu.blockSignals(True)
        self.chk_low_cpu.setChecked(self.mw.engine.low_end_mode)
        self.chk_low_cpu.blockSignals(False)
        self.chk_spec_fill.blockSignals(True)
        self.chk_spec_fill.setChecked(getattr(self.mw, 'spectrum_fill_enabled', True))
        self.chk_spec_fill.blockSignals(False)
        self.update_graph_perf(
            getattr(self.mw, "_graph_perf_text", "Perf: idle"),
            getattr(self.mw, "_graph_perf_tip", "Start playback to measure real graph FPS."),
            getattr(self.mw, "_graph_perf_color", "#8e8e93"),
        )
        self._refresh_muted_styles()
        cfg = getattr(self.mw, "scrobble_cfg", {})
        self.chk_lfm.blockSignals(True); self.chk_lfm.setChecked(bool(cfg.get("lfm_enabled", False))); self.chk_lfm.blockSignals(False)
        min_s = int(cfg.get("min_track_sec", 30))
        pct_v = int(cfg.get("scrobble_pct", 50))
        max_s = int(cfg.get("max_time_sec", 180))
        for sl, val in ((self.s_scrob_min, max(2, min(12, min_s // 5))), (self.s_scrob_pct, max(30, min(90, pct_v))), (self.s_scrob_max, max(3, min(36, max_s // 10)))):
            sl.blockSignals(True); sl.setValue(val); sl.blockSignals(False)
        m_s = self.s_scrob_min.value() * 5; self.lbl_scrob_min.setText(f"{m_s // 60:02d}:{m_s % 60:02d}")
        self.lbl_scrob_pct.setText(f"{self.s_scrob_pct.value()}%")
        ms = self.s_scrob_max.value() * 10
        self.lbl_scrob_max.setText(f"{ms // 60:02d}:{ms % 60:02d}")
        if cfg.get("lfm_sk"):
            self.lbl_scrob_status.setText(f"Connected as {cfg.get('lfm_user') or 'Last.fm user'}")
        is_g = is_glass_theme(self.mw.current_theme)
        self.s_glow.default_value = 30 if is_g else (20 if self.mw.current_theme == "classic" else 30)
        self.lbl_rim.setEnabled(is_g)
        self.s_rim.setEnabled(is_g)
        if self.isVisible():
            if is_g:
                if glass and self._glass_id is None:
                    self._glass_id = glass.apply_glass_to_window(self)
                if glass:
                    configure_macos_glass(self, self.mw.current_theme, True,
                                          getattr(self.mw, "glass_clarity", 45) / 100.0,
                                          material=getattr(self.mw, "glass_material", None))
                apply_windows_glass(self, True, False)
                apply_seamless_titlebar(self)
            else:
                if glass and self._glass_id is not None:
                    try:
                        glass.remove_glass_effect(self._glass_id)
                    except Exception:
                        pass
                    self._glass_id = None
                clear_macos_glass_state(self)
                if glass:
                    configure_macos_glass(self, self.mw.current_theme, False)
                reset_macos_window_opaque(self)
                apply_windows_glass(self, False, self.mw.current_theme == 'light')
                apply_seamless_titlebar(self)
        self.setStyleSheet(self.mw.styleSheet())

    def _on_theme(self, idx):
        targets = list(THEME_ORDER)
        target = targets[idx]
        if self.mw.current_theme != target:
            self.mw.current_theme = targets[(idx - 1) % len(targets)]
            self.mw.toggle_theme()
        self.sync_from_main()

    def _on_glass(self, *_):
        """Apply the current Dark/Light stage (also used by the rim slider)."""
        stage = getattr(self.mw, "glass_appearance", "dark")
        if stage not in GLASS_STAGES:
            stage = "dark"
        self.mw.glass_appearance = stage
        self.mw.glass_material = GLASS_STAGE_MATERIAL[stage]
        self.mw.glass_rim = self.s_rim.value()
        # configure_macos_glass() reads these from the widget it is given.
        self.glass_appearance = stage
        self.glass_overrides = getattr(self.mw, "glass_overrides", {})
        if glass and is_glass_theme(self.mw.current_theme):
            configure_macos_glass(self.mw, self.mw.current_theme, True,
                                  getattr(self.mw, "glass_clarity", 45) / 100.0,
                                  material=self.mw.glass_material)
            configure_macos_glass(self, self.mw.current_theme, True,
                                  getattr(self.mw, "glass_clarity", 45) / 100.0,
                                  material=self.mw.glass_material)
        self.mw._on_ambient_color_changed(self.mw._current_ambient_color)
        graph_theme = getattr(self.mw, "_update_graph_theme", None)
        if graph_theme:
            graph_theme()
        self.mw.save_session_state()
        self.setStyleSheet(self.mw.styleSheet())

    def _on_glass_stage(self, *_):
        self.mw.glass_appearance = self.combo_glass_stage.currentData() or "dark"
        self._on_glass()
        self.sync_from_main()

    def _on_glass_tuning(self, *_):
        stage = getattr(self.mw, "glass_appearance", "dark")
        if stage not in GLASS_STAGES:
            stage = "dark"
        overrides = dict(getattr(self.mw, "glass_overrides", {}) or {})
        per = dict(overrides.get(stage, {}) or {})
        per["tint_alpha"] = self.s_glass_tint.value() / 100.0
        card_alpha = _scrim_slider_to_alpha(self.s_glass_scrim.value())
        per["card_alpha"] = card_alpha
        per["list_alpha"] = max(0, card_alpha - 8)
        per["style"] = self.combo_glass_style.currentData() or "clear"
        overrides[stage] = normalize_glass_overrides(per, stage)
        self.mw.glass_overrides = overrides
        self._on_glass()

    def _reset_glass_tuning(self):
        stage = getattr(self.mw, "glass_appearance", "dark")
        if stage not in GLASS_STAGES:
            stage = "dark"
        overrides = dict(getattr(self.mw, "glass_overrides", {}) or {})
        overrides.pop(stage, None)
        self.mw.glass_overrides = overrides
        self._on_glass()
        self.sync_from_main()

    def _sync_glass_from_system(self):
        info = read_system_glass_preferences()
        appearance = info.get("appearance")
        if appearance in GLASS_STAGES and self.mw.current_theme == "macos27_glass":
            self.mw.glass_appearance = appearance
            self.combo_glass_stage.blockSignals(True)
            self.combo_glass_stage.setCurrentIndex(GLASS_STAGES.index(appearance))
            self.combo_glass_stage.blockSignals(False)
            self._on_glass()
            self.sync_from_main()
        summary = info.get("summary", "System sync unavailable.")
        self.lbl_stage.setToolTip(f"{self._glass_sync_tip}\nLast sync: {summary}")
        self.combo_glass_stage.setToolTip(
            f"Dark/Light stage only. {summary}"
        )
        QToolTip.showText(self.lbl_stage.mapToGlobal(QPoint(0, self.lbl_stage.height())), summary, self.lbl_stage)

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
        # A little extra default height goes to the spectrum graph only;
        # left library and top controls keep their previous proportions.
        avail = QApplication.primaryScreen().availableGeometry() if QApplication.primaryScreen() else None
        default_h = min(880, max(820, avail.height() - 20)) if avail else 880
        self.resize(1100, max(820, default_h))
        
        pg.setConfigOptions(antialias=False, useOpenGL=False, enableExperimental=False)
        
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

        # Ambient colors follow the selected cover immediately. A separate
        # fade used to leave the old hue behind while the accent/lava switched.
        self._current_ambient_color = QColor(26, 36, 58)

        self.engine = AudioEngine()
        self.engine.playback_stopped.connect(self.on_playback_stopped)
        self.engine.track_finished.connect(self.play_next_track)
        
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.update_ui_seekbar)

        self.viz_timer = QTimer(self)
        self.viz_timer.setTimerType(Qt.PreciseTimer)
        self.viz_timer.setInterval(graph_fps_interval(GRAPH_FPS_DEFAULT_INDEX))
        self.viz_timer.timeout.connect(self.update_graph)

        self._graph_perf_start = 0.0
        self._graph_perf_frames = 0
        self._graph_fps_ema = None
        self._graph_perf_text = "Perf: idle"
        self._graph_perf_tip = "Start playback to measure real graph FPS."
        self._graph_perf_color = "#8e8e93"

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
        
        self.current_theme = "liquid_glass"
        self.glass_effect_id = None
        self.current_accent, self.current_accent_end = "#0a84ff", "#64d2ff"
        self.theme_glow = {"classic": 0.20, "light": 0.30, "liquid_glass": 1.00, "macos27_glass": 1.00}
        self.ambient_opacity = self.theme_glow["liquid_glass"]
        self.glass_material = "automatic"
        self.glass_appearance = "dark"
        self.glass_overrides = {}
        self.glass_clarity = 45
        self.glass_rim = DEFAULT_GLASS_RIM
        self.spectrum_fill_enabled = True
        self.dynamic_ambient = True
        self.scrobble_cfg = {"lfm_enabled": False, "lfm_api_key": "", "lfm_api_secret": "", "lfm_user": "", "lfm_sk": ""}
        self._current_meta = {}
        self._scrobble_sent = False
        self._scrobble_started_ts = 0
        self._scrobble_elapsed_sec = 0.0
        self._scrobble_last_tick = None
        self._scrobble_workers = []
        pg.setConfigOption("foreground", "#eef2f8")
        self.init_ui()
        self._sync_main_content_height()
        self._reset_graph_perf("Perf: idle")

        self.sys_device_timer = QTimer(self)
        self.sys_device_timer.setInterval(1000)
        self.sys_device_timer.timeout.connect(self.check_sys_default)
        QTimer.singleShot(120, self._deferred_startup)

    def _on_glass_tuned(self, _=None):
        # Legacy hook from the old compact slider row; the discrete material
        # selector lives in SettingsDialog and updates these values directly.
        if hasattr(self, "slider_glass_rim"):
            self.glass_rim = self.slider_glass_rim.value()
        if glass and is_glass_theme(self.current_theme):
            configure_macos_glass(self, self.current_theme, True,
                                  self.glass_clarity / 100.0,
                                  material=getattr(self, "glass_material", None))
        if hasattr(self, 'lava_overlay'): self.lava_overlay.sync_active()
        self._on_ambient_color_changed(self._current_ambient_color)
        self.save_session_state()

    def open_settings(self):
        if not hasattr(self, "_settings_dlg") or self._settings_dlg is None:
            self._settings_dlg = SettingsDialog(self)
        self._settings_dlg.sync_from_main()
        self._settings_dlg.show()
        self._settings_dlg.raise_()
        self._settings_dlg.activateWindow()

    def toggle_dynamic_ambient(self, enabled):
        self.dynamic_ambient = bool(enabled)
        if hasattr(self, "lava_overlay"):
            self.lava_overlay.sync_active()
        self.save_session_state()

    def update_ambient_opacity(self, val):
        self.ambient_opacity = val / 100.0
        self.theme_glow[self.current_theme] = self.ambient_opacity
        self._on_ambient_color_changed(self._current_ambient_color)
        self.save_session_state()

    def _update_graph_theme(self):
        theme = getattr(self, "current_theme", "classic")
        appearance = getattr(self, "glass_appearance", "dark")
        is_light = is_light_surface(theme, appearance)
        is_glass = is_glass_theme(theme)
        axis_color = '#1c1c1e' if is_light else ('#ffffff' if is_glass else '#eef2f8')
        
        pg.setConfigOption('foreground', axis_color)

        # Update axes and grid
        for axis_name in ['left', 'bottom']:
            axis = self.plot_widget.getAxis(axis_name)
            axis.setPen(pg.mkPen(axis_color))
            axis.setTextPen(pg.mkPen(axis_color))
            axis.setTickPen(pg.mkPen(axis_color))
            if axis.label is not None:
                axis.label.setDefaultTextColor(pg.mkColor(axis_color))
        
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
        theme = getattr(self, "current_theme", "classic")
        is_m27 = theme == "macos27_glass"
        is_light = theme == "light"
        # With Ambient Glow at minimum, Glass 2.0 should stay neutral instead
        # of amplifying the sampled album-art hue.
        if s < 0.04 or (is_m27 and getattr(self, "ambient_opacity", 0.3) <= 0.02):
            return ("#8e8e93", "#c7c7cc")

        # Greens/yellows (h ~ 0.14..0.46) have high perceptual brightness, so tame saturation & luminance
        is_bright_hue = 0.13 <= h <= 0.46
        if is_m27:
            s = min(0.58 if is_bright_hue else 0.68, max(0.28, s * 0.80))
            l = min(0.44 if is_bright_hue else 0.50, max(0.32, l))
        elif is_light:
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
        try:
            idx = THEME_ORDER.index(self.current_theme)
        except ValueError:
            idx = -1
        self.current_theme = THEME_ORDER[(idx + 1) % len(THEME_ORDER)]

        self.ambient_opacity = self.theme_glow.get(self.current_theme, 1.0 if is_glass_theme(self.current_theme) else (0.20 if self.current_theme == "classic" else 0.30))
        if hasattr(self, "btn_theme"): self.btn_theme.setText(f"Theme: {THEME_LABELS[self.current_theme]}")
        if hasattr(self, "glass_tune_row"):
            self.glass_tune_row.setVisible(is_glass_theme(self.current_theme))

        if is_glass_theme(self.current_theme):
            if glass and getattr(self, "glass_effect_id", None) is None:
                self.glass_effect_id = glass.apply_glass_to_window(self)
            if glass:
                configure_macos_glass(self, self.current_theme, True,
                                      getattr(self, "glass_clarity", 45) / 100.0,
                                      material=getattr(self, "glass_material", None))
            apply_windows_glass(self, True, False)
            apply_seamless_titlebar(self)
        else:
            if glass and getattr(self, "glass_effect_id", None) is not None:
                try:
                    glass.remove_glass_effect(self.glass_effect_id)
                except Exception:
                    pass
                self.glass_effect_id = None
            clear_macos_glass_state(self)
            if glass:
                configure_macos_glass(self, self.current_theme, False)
            reset_macos_window_opaque(self)
            apply_windows_glass(self, False, self.current_theme == 'light')
            apply_seamless_titlebar(self)

        self._update_graph_theme()
        self._refresh_muted_text()
        self._apply_theme_style()
        
        # Force layout recalculation and repaint
        if hasattr(self, 'lava_overlay'): self.lava_overlay.sync_active()
        if hasattr(self, 'glass_lens'): self.glass_lens.sync_active()
        self.centralWidget().layout().activate()
        self.update()
        self.repaint()
        if getattr(self, "_settings_dlg", None) and self._settings_dlg.isVisible():
            self._settings_dlg.sync_from_main()
        self.save_session_state()

    def _apply_theme_style(self, color=None):
        """Rebuild QSS from the current theme/overrides.

        Theme changes, session restore, and ambient-color changes all go through
        here so a saved Panel Scrim is never skipped when the saved theme already
        matches the in-memory default.
        """
        if color is None:
            color = self._current_ambient_color
        # Re-derive the accent from the current Ambient Glow value. Otherwise
        # lowering the slider leaves the previously amplified color in QSS.
        self.current_accent, self.current_accent_end = self._get_accent_color(
            color.red(), color.green(), color.blue()
        )
        self.setStyleSheet(build_liquid_qss(
            color.red(), color.green(), color.blue(),
            theme=self.current_theme,
            ambient_opacity=self.ambient_opacity,
            accent=self.current_accent,
            accent_end=self.current_accent_end,
            glass_clarity=self.glass_clarity,
            glass_rim=self.glass_rim,
            glass_material=getattr(self, "glass_material", None),
            glass_overrides=getattr(self, "glass_overrides", None),
            glass_appearance=getattr(self, "glass_appearance", "dark"),
        ))
        self._refresh_muted_text()
        if getattr(self, "_settings_dlg", None) and self._settings_dlg.isVisible():
            self._settings_dlg.setStyleSheet(self.styleSheet())

    def _on_ambient_color_changed(self, color):
        self._apply_theme_style(color)

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
        # AppKit can defer the native material render until after the first
        # activation; without this second push the panels read too transparent
        # until the user opens and closes Settings.
        QTimer.singleShot(200, self._reapply_native_glass)
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

    def _restore_dev_status(self):
        if self.engine.is_playing:
            self._set_dev_status(self.engine.stream_mode_status, self.engine.stream_is_exclusive)
        else:
            self._set_dev_status("Tip: Select your DAC directly instead of System Default for native sample-rate switching.", None)

    def _on_scrobble_status(self, msg):
        if getattr(self, "_settings_dlg", None):
            self._settings_dlg.lbl_scrob_status.setText(msg)
        if msg.startswith("Scrobbled") or msg.startswith("Last.fm error"):
            self._set_dev_status(f"Last.fm • {msg}", True if msg.startswith("Scrobbled") else False)
            if not hasattr(self, "_scrob_banner_timer"):
                self._scrob_banner_timer = QTimer(self)
                self._scrob_banner_timer.setSingleShot(True)
                self._scrob_banner_timer.timeout.connect(self._restore_dev_status)
            self._scrob_banner_timer.start(5000)

    def _trigger_scrobble(self, action):
        cfg = getattr(self, "scrobble_cfg", {})
        if not (cfg.get("lfm_enabled") and cfg.get("lfm_sk")):
            return
        meta = dict(getattr(self, "_current_meta", {}) or {})
        if self.engine.audio_data is not None and self.engine.fs:
            meta["duration"] = int(len(self.engine.audio_data) / float(self.engine.fs))
        meta["timestamp"] = getattr(self, "_scrobble_started_ts", int(time.time()))
        w = ScrobbleWorker(cfg, action, meta, self)
        w.status_msg.connect(self._on_scrobble_status)
        w.finished.connect(lambda: self._scrobble_workers.remove(w) if w in self._scrobble_workers else None)
        self._scrobble_workers.append(w)
        w.start()

    def toggle_spectrum_fill(self, checked):
        self.spectrum_fill_enabled = bool(checked)
        lvl = -120 if self.spectrum_fill_enabled else None
        if hasattr(self, "curve_in"):
            self.curve_in.setFillLevel(lvl)
            self.curve_out.setFillLevel(lvl)
        if hasattr(self, "save_session_state"):
            self.save_session_state()

    def _reapply_native_glass(self):
        """Re-push native glass after a mode/CPU change (backdrop scale too)."""
        if not glass or not is_glass_theme(self.current_theme):
            return
        configure_macos_glass(self, self.current_theme, True,
                              getattr(self, "glass_clarity", 45) / 100.0,
                              material=getattr(self, "glass_material", None))
        dlg = getattr(self, "_settings_dlg", None)
        if dlg is not None and dlg.isVisible():
            dlg.sync_from_main()
        props = getattr(self, "_props_dlg", None)
        if props is not None and props.isVisible() and hasattr(props, "mw"):
            configure_macos_glass(props, self.current_theme, True,
                                  getattr(self, "glass_clarity", 45) / 100.0,
                                  material=self.glass_material)

    def toggle_cpu_mode(self, checked):
        low = bool(checked)
        self.engine.low_end_mode = low
        if hasattr(self, "btn_cpu_mode"): self.btn_cpu_mode.setText("CPU: Low-End" if low else "CPU: Full Quality")
        self.combo_fps.setCurrentIndex(0 if low else GRAPH_FPS_DEFAULT_INDEX)
        if hasattr(self, 'lava_overlay'): self.lava_overlay.sync_active()
        self._reapply_native_glass()
        if self.engine.sofa_data is not None:
            self.engine._update_hrirs()
        self.save_session_state()

    def load_default_sofa(self):
        default_path = get_resource_path("default.sofa")
        prebaked_npz = get_resource_path("default_sofa.npz")
        if os.path.exists(prebaked_npz):
            npz = np.load(prebaked_npz)
            self.engine.sofa_data = _FastSofaData(npz["time"], int(npz["fs"]))
            self.engine.sofa_coords = _FastSofaCoords(npz["cart"])
            self.engine.sofa_path = default_path if os.path.exists(default_path) else prebaked_npz
            self.engine._update_hrirs()
            self.lbl_sofa_file.setText("default.sofa (Built-in)")
        elif os.path.exists(default_path):
            is_cached = os.path.exists(self.engine._sofa_cache_path(default_path))
            if not is_cached:
                self.lbl_sofa_file.setText("Caching HRTF (first launch)...")
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
        self.lava_overlay = LavaLampOverlay(self, central)
        self.lava_overlay.lower()

        # Keep the whole player scrollable so opening DSP controls never
        # squishes the cover / transport / spectrum. Transparent viewport so
        # the native glass material and lava wash stay visible behind it.
        outer_layout = QVBoxLayout(central)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)
        self.main_scroll = QScrollArea(central)
        self.main_scroll.setObjectName("mainScroll")
        self.main_scroll.setFrameShape(QScrollArea.NoFrame)
        self.main_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.main_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.main_scroll.setWidgetResizable(True)
        self.main_scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        translucent = windows_transparency_enabled()
        self.main_scroll.setAttribute(Qt.WA_TranslucentBackground, translucent)
        self.main_scroll.setAutoFillBackground(False)
        viewport = self.main_scroll.viewport()
        viewport.setAttribute(Qt.WA_TranslucentBackground, translucent)
        viewport.setAttribute(Qt.WA_NoSystemBackground, translucent)
        viewport.setAutoFillBackground(False)
        outer_layout.addWidget(self.main_scroll)
        content = QWidget(self.main_scroll)
        content.setObjectName("mainContent")
        # A plain QWidget paints the palette window color by default; that
        # opaque fill hid the native NSGlassEffectView behind the scroll area.
        content.setAttribute(Qt.WA_TranslucentBackground, translucent)
        content.setAttribute(Qt.WA_NoSystemBackground, translucent)
        content.setAutoFillBackground(False)
        self.main_content = content
        self.main_scroll.setWidget(content)
        self.main_scroll.verticalScrollBar().valueChanged.connect(
            lambda *_: schedule_macos_panel_glass_sync(self)
        )
        self.lava_overlay.lower()

        main_layout = QHBoxLayout(content)
        main_layout.setSpacing(10)
        main_layout.setContentsMargins(20, 40 if sys.platform == 'darwin' else 20, 20, 20)

        # --- LEFT SIDE: LIBRARY ---
        left_card = make_glass_card()
        self.left_card = left_card
        # Default window is slightly taller for the graph; keep the library
        # card at its original 820px-window proportion.
        left_card.setFixedHeight(_LIBRARY_BASE_HEIGHT)
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
            "Album",
            "Artist • Album"
        ])
        self.combo_group.setCurrentIndex(4)
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
        self.list_widget.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self._show_playlist_context_menu)
        self.list_widget.setDragDropMode(QListWidget.NoDragDrop if (hasattr(self, 'combo_group') and self.combo_group.currentIndex() in (3, 4)) else QListWidget.InternalMove)
        self.list_widget.setDefaultDropAction(Qt.MoveAction)
        self.list_widget.setSelectionMode(QListWidget.ExtendedSelection)
        self.list_widget.model().rowsMoved.connect(self.on_playlist_reordered)
        left_layout.addWidget(self.list_widget, 1)
        
        main_layout.addWidget(left_card, stretch=1, alignment=Qt.AlignTop)

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
        self.btn_settings.setFixedSize(42, 34)
        self.btn_settings.setStyleSheet("font-size: 19px; padding: 0px 0px 1px 0px;")
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

        _is_first = (not os.path.exists(get_resource_path("default_sofa.npz"))) and (not os.path.exists(self.engine._sofa_cache_path(get_resource_path("default.sofa"))) if os.path.exists(get_resource_path("default.sofa")) else False)
        self.lbl_dev_status = QLabel("First launch: caching HRTF acoustic profile — subsequent startups will be instant." if _is_first else "Tip: Select your DAC directly instead of System Default for native sample-rate switching.")
        self._dev_status_exclusive = "info" if _is_first else None
        if _is_first:
            self.lbl_dev_status.setStyleSheet("color: #64d2ff; font-size: 12px; font-weight: bold; margin-bottom: 4px;")
        self.lbl_dev_status.setWordWrap(True)
        if not _is_first:
            self.lbl_dev_status.setStyleSheet(self._dev_status_style())
        top_card_layout.addWidget(self.lbl_dev_status)

        # Album Art & Track Info
        info_layout = QHBoxLayout()
        self.lbl_cover = QLabel()
        self.lbl_cover.setFixedSize(120, 120)
        self.lbl_cover.setStyleSheet("background-color: rgba(0,0,0,90); border: 1px solid rgba(255,255,255,45); border-radius: 12px;")
        self.glass_lens = GlassLensOverlay(self, self.lbl_cover)
        self.glass_lens.sync_active()
        
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

        self.btn_float_mode = FloatSchemeButton("Hard Clip (Exact)", self)
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
        self.combo_fps.setCurrentIndex(GRAPH_FPS_DEFAULT_INDEX)
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

        self.btn_stereo_scope = QPushButton("Stereo: Off")
        self.btn_stereo_scope.setCheckable(True)
        self.btn_stereo_scope.setChecked(False)
        self.btn_stereo_scope.clicked.connect(self.toggle_stereo_scope)
        peak_layout.addWidget(self.btn_stereo_scope)
        
        peak_layout.addStretch()
        self.lbl_peak = QLabel("Peak: -inf dB")
        self.lbl_peak.setStyleSheet("color: #34c759; font-weight: bold; font-family: 'Helvetica Neue';")
        peak_layout.addWidget(self.lbl_peak)
        
        spec_layout.addLayout(peak_layout)

        perf_row = QHBoxLayout()
        perf_row.setContentsMargins(0, 0, 0, 0)
        perf_row.setSpacing(6)
        self.lbl_perf = QLabel("Perf: idle")
        self.lbl_perf.setMaximumHeight(16)
        self.lbl_perf.setStyleSheet(f"color: {self._muted_text_color()}; font-size: 11px; font-weight: bold;")
        self.lbl_perf.setToolTip("Start playback to measure real graph FPS.")
        perf_row.addWidget(self.lbl_perf)
        perf_row.addStretch()
        spec_layout.addLayout(perf_row)

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
        
        spec_row = QHBoxLayout()
        spec_row.setContentsMargins(0, 0, 0, 0)
        spec_row.setSpacing(10)
        spec_row.addWidget(self.plot_widget, 1)
        self.stereo_analyzer = StereoAnalyzerWidget(self)
        self.stereo_analyzer.setVisible(False)
        spec_row.addWidget(self.stereo_analyzer)
        spec_layout.addLayout(spec_row, stretch=1)
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

        if hasattr(self, "combo_group") and self.combo_group.currentIndex() != 0:
            self.apply_sort()
            self.lbl_lib_info.setText(f"{len(self.library_tracks)} tracks in playlist")
            self.btn_scan.setEnabled(True)
            if auto_play_if_empty and was_empty and self.library_tracks:
                target_t = self.library_tracks[0]
                item = next((self.list_widget.item(r) for r in range(self.list_widget.count()) if (self.list_widget.item(r).data(Qt.UserRole) or {}).get('path') == target_t['path']), None)
                if item:
                    self.list_widget.setCurrentItem(item)
                    self.play_selected_item(item)
            return

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
        if not isinstance(track, dict) or track.get("_is_header"):
            return
        self.current_track_index = next((i for i, t in enumerate(self.library_tracks) if t['path'] == track['path']), -1)
        
        self.lbl_track_title.setText(track['title'])
        self.lbl_track_artist.setText(track['artist'])
        self._current_meta = dict(track)
        self._scrobble_sent = False
        self._scrobble_elapsed_sec = 0.0
        self._scrobble_last_tick = time.perf_counter()
        self._scrobble_started_ts = int(time.time())
        self._trigger_scrobble('now_playing')
        
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
        target_t = self.library_tracks[self.current_track_index]
        item = next((self.list_widget.item(r) for r in range(self.list_widget.count()) if (self.list_widget.item(r).data(Qt.UserRole) or {}).get('path') == target_t['path']), None)
        if item is None:
            return
        self.list_widget.setCurrentItem(item)
        self.play_selected_item(item)

    def play_prev_track(self):
        if not self.library_tracks:
            return
        self.current_track_index = (self.current_track_index - 1) % len(self.library_tracks)
        target_t = self.library_tracks[self.current_track_index]
        item = next((self.list_widget.item(r) for r in range(self.list_widget.count()) if (self.list_widget.item(r).data(Qt.UserRole) or {}).get('path') == target_t['path']), None)
        if item is None:
            return
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

    def _muted_text_color(self):
        return muted_text_color(getattr(self, "current_theme", "classic"),
                                getattr(self, "glass_appearance", "dark"))

    def _dev_status_style(self, *, bold=False):
        weight = " font-weight: bold;" if bold else ""
        return (f"color: {self._muted_text_color()}; font-size: 11px;"
                f"{weight} margin-bottom: 4px;")

    def _refresh_muted_text(self):
        muted = self._muted_text_color()
        if (hasattr(self, "lbl_dev_status")
                and getattr(self, "_dev_status_exclusive", None) is None):
            self.lbl_dev_status.setStyleSheet(self._dev_status_style())
        # Keep the idle perf readout in step with the surface; live green/orange
        # results keep their own status color.
        if getattr(self, "_graph_perf_color", None) in ("#8e8e93", "#3c3c43"):
            self._graph_perf_color = muted
            if hasattr(self, "lbl_perf"):
                self.lbl_perf.setStyleSheet(
                    f"color: {muted}; font-size: 11px; font-weight: bold;")

    def _set_dev_status(self, text, exclusive):
        self.lbl_dev_status.setText(text)
        self._dev_status_exclusive = exclusive
        if exclusive is None:
            self.lbl_dev_status.setStyleSheet(self._dev_status_style())
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
        self.viz_timer.setInterval(graph_fps_interval(index))
        self._reset_graph_perf()

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
        if getattr(self.btn_float_mode, "_popup", None) and self.btn_float_mode._popup.isVisible():
            self.btn_float_mode._scheme.update()

    def toggle_bypass(self, state):
        if not state and self.engine.rust_dsp is not None:
            self.engine.rust_dsp.reset()
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

    def _reset_graph_perf(self, text="Perf: measuring…"):
        self._graph_perf_start = 0.0
        self._graph_perf_frames = 0
        self._graph_fps_ema = None
        self._graph_perf_text = text
        self._graph_perf_tip = "Start playback to measure real graph FPS."
        self._graph_perf_color = self._muted_text_color()
        if hasattr(self, "lbl_perf"):
            self.lbl_perf.setText(text)
            self.lbl_perf.setStyleSheet(
                f"color: {self._graph_perf_color}; font-size: 11px; font-weight: bold;"
            )
            self.lbl_perf.setToolTip(self._graph_perf_tip)
        dlg = getattr(self, "_settings_dlg", None)
        if dlg is not None:
            dlg.update_graph_perf(self._graph_perf_text, self._graph_perf_tip,
                                  self._graph_perf_color)

    def _set_graph_perf(self, achieved):
        target = graph_fps_target(self.combo_fps.currentIndex())
        status, tip, color = performance_advice(
            target, achieved,
            low_end_mode=bool(self.engine.low_end_mode),
            spectrum_fill=bool(getattr(self, "spectrum_fill_enabled", True)),
            theme=getattr(self, "current_theme", ""),
        )
        text = f"Perf: {achieved:.0f}/{target} FPS · {status}"
        full_tip = f"Measured from real graph callbacks (target {target} FPS). {tip}"
        self._graph_perf_text = text
        self._graph_perf_tip = full_tip
        self._graph_perf_color = color
        if hasattr(self, "lbl_perf"):
            self.lbl_perf.setText(text)
            self.lbl_perf.setStyleSheet(
                f"color: {color}; font-size: 11px; font-weight: bold;"
            )
            self.lbl_perf.setToolTip(full_tip)
        dlg = getattr(self, "_settings_dlg", None)
        if dlg is not None:
            dlg.update_graph_perf(text, full_tip, color)

    def _track_graph_fps(self, now=None):
        if not getattr(self.engine, "is_playing", False):
            return
        if now is None:
            now = time.perf_counter()
        if not getattr(self, "_graph_perf_start", 0.0):
            self._graph_perf_start = now
            self._graph_perf_frames = 0
            return
        self._graph_perf_frames += 1
        elapsed = now - self._graph_perf_start
        if elapsed < _GRAPH_PERF_WINDOW_SEC:
            return
        achieved = self._graph_perf_frames / elapsed
        ema = getattr(self, "_graph_fps_ema", None)
        self._graph_fps_ema = (achieved if ema is None else
                               (1.0 - _GRAPH_PERF_SMOOTHING) * ema +
                               _GRAPH_PERF_SMOOTHING * achieved)
        self._set_graph_perf(self._graph_fps_ema)
        self._graph_perf_start = now
        self._graph_perf_frames = 0

    def update_graph(self):
        try:
            self._track_graph_fps()
            if not self.engine.is_playing or (self.engine.audio_data is None and self.engine.input_device is None and not self.engine.use_mac_tap):
                return
            if getattr(self.engine, 'latest_stereo_out', None) is not None and hasattr(self, 'stereo_analyzer') and self.stereo_analyzer.isVisible():
                st_buf = self.engine.latest_stereo_out
                _, lf_st, wt_st = self.engine.viz_state
                fr_st = min(1.0, max(0.0, (time.perf_counter() - wt_st) / max(1e-4, lf_st / float(self.engine.fs))))
                end_st = max(512, min(len(st_buf), int(512 + fr_st * (len(st_buf) - 512))))
                self.stereo_analyzer.update_from_stereo(st_buf[max(0, end_st - 1024):end_st])
                
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
                self.chunk_in_buf = np.zeros(n_fft, dtype=np.float32)
                self.chunk_out_buf = np.zeros(n_fft, dtype=np.float32)

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
            self._scrobble_sent = False
            self._scrobble_elapsed_sec = 0.0
            self._scrobble_last_tick = time.perf_counter()
            self._scrobble_started_ts = int(time.time())
            self._trigger_scrobble("now_playing")
            self.load_audio_file(pending_path)
            return
        if self.engine.is_playing:
            self._scrobble_last_tick = None
            self.engine.stop()
            self.btn_play.setText("▶ Play")
            self.timer.stop()
            self.viz_timer.stop()
            self._reset_graph_perf("Perf: idle")
            self.smoothed_in = None
            self.lbl_dev_status.setText("Paused — Select your DAC directly instead of System Default for native sample-rate switching.")
            self.lbl_dev_status.setStyleSheet(self._dev_status_style())
        else:
            self._scrobble_last_tick = time.perf_counter()
            self.engine.start()
            self.btn_play.setText("⏸ Pause")
            self.timer.start()
            self.viz_timer.start()
            self._reset_graph_perf()
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
                now_t = time.perf_counter()
                last_t = getattr(self, "_scrobble_last_tick", None)
                if last_t is not None and not getattr(self, "is_seeking", False):
                    self._scrobble_elapsed_sec = getattr(self, "_scrobble_elapsed_sec", 0.0) + min(0.5, max(0.0, now_t - last_t))
                self._scrobble_last_tick = now_t
                listened_sec = getattr(self, "_scrobble_elapsed_sec", 0.0)

                dur_sec = len(self.engine.audio_data) / float(self.engine.fs)
                scfg = getattr(self, "scrobble_cfg", {})
                min_track_s = float(scfg.get("min_track_sec", 30))
                pct_thresh = float(scfg.get("scrobble_pct", 50)) / 100.0
                max_time_s = float(scfg.get("max_time_sec", 180))
                target_sec = min(max_time_s, dur_sec * pct_thresh)
                eligible = dur_sec >= min_track_s and target_sec > 0.0
                if getattr(self, "_settings_dlg", None) and self._settings_dlg.isVisible():
                    if getattr(self, "_scrobble_sent", False):
                        self._settings_dlg.scrob_progress.setValue(100)
                    elif eligible:
                        pct = min(99, max(0, int(round((listened_sec / target_sec) * 100.0))))
                        self._settings_dlg.scrob_progress.setValue(pct)
                        rem = max(0, int(round(target_sec - listened_sec)))
                        if scfg.get("lfm_enabled") and scfg.get("lfm_sk"):
                            self._settings_dlg.lbl_scrob_status.setText(f"Scrobbling in {rem}s ({pct}%)...")
                    else:
                        self._settings_dlg.scrob_progress.setValue(0)
                        if scfg.get("lfm_enabled") and scfg.get("lfm_sk"):
                            self._settings_dlg.lbl_scrob_status.setText(f"Track shorter than {int(min_track_s)}s — skipped")
                if not getattr(self, '_scrobble_sent', False) and eligible and listened_sec >= target_sec:
                    self._scrobble_sent = True
                    if getattr(self, "_settings_dlg", None):
                        self._settings_dlg.scrob_progress.setValue(100)
                    self._trigger_scrobble('scrobble')
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
        self._reset_graph_perf("Perf: idle")
        self.smoothed_in = None
        self._set_dev_status("Stopped — Select your DAC directly instead of System Default for native sample-rate switching.", None)

    def _sync_main_content_height(self):
        """Keep stock proportions; only add scrollable height for DSP panel."""
        scroll = getattr(self, "main_scroll", None)
        content = getattr(self, "main_content", None)
        if scroll is None or content is None:
            return
        vh = scroll.viewport().height()
        if vh < 200:  # not laid out yet; retry after the first show/resize
            QTimer.singleShot(0, self._sync_main_content_height)
            return
        # At the stock size the library keeps its original 760px proportion.
        # When the window is maximized or fullscreen, grow it by the same extra
        # height so it does not stay short while the right column fills the
        # screen.
        left = getattr(self, "left_card", None)
        if left is not None:
            # During a zoom/fullscreen transition keep the library at its
            # stock height. Growing it mid-animation raises the effective
            # minimum size and Qt/AppKit clamps the shrink part way.
            if getattr(self, "_is_resizing", False):
                target_h = _LIBRARY_BASE_HEIGHT
            else:
                target_h = _LIBRARY_BASE_HEIGHT + max(0, vh - _DEFAULT_WINDOW_HEIGHT)
            if left.height() != target_h:
                left.setFixedHeight(target_h)
        extra = 0
        panel = getattr(self, "dsp_panel", None)
        if panel is not None and panel.isVisible():
            extra = panel.sizeHint().height() + 8
        content.setMaximumHeight(vh + extra)

    def _reset_library_height_for_shrink(self):
        """Drop the grown library before a window shrink.

        ``_sync_main_content_height()`` grows the library with the viewport.
        Keeping that larger fixed height while the window shrinks raises the
        effective minimum size, so Qt/AppKit can only shrink part way and the
        spectrum card stays too tall. Reset to the stock height first; the
        following resizeEvent() grows it back to the correct size.
        """
        left = getattr(self, "left_card", None)
        if left is not None and left.height() > _LIBRARY_BASE_HEIGHT:
            left.setFixedHeight(_LIBRARY_BASE_HEIGHT)

    def _scroll_main_to_bottom(self):
        """Show the newly opened bottom controls without shrinking the UI."""
        scroll = getattr(self, "main_scroll", None)
        if scroll is None:
            return
        bar = scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def toggle_stereo_scope(self, checked):
        visible = bool(checked)
        self.stereo_analyzer.setVisible(visible)
        self.btn_stereo_scope.setText("Stereo: On" if visible else "Stereo: Off")
        self._sync_main_content_height()
        if visible:
            QTimer.singleShot(0, self._scroll_main_to_bottom)
        if hasattr(self, "save_session_state"):
            self.save_session_state()

    def toggle_dsp_panel(self, checked):
        visible = bool(checked)
        self.dsp_panel.setVisible(visible)
        self.btn_toggle_dsp_panel.setText("DSP Controls ▲" if visible else "DSP Controls ▼")
        if hasattr(self, "lava_overlay") and self.centralWidget():
            self.lava_overlay.setGeometry(self.centralWidget().rect())
            self.lava_overlay.lower()
        self._sync_main_content_height()
        if hasattr(self, "lbl_cover"):
            self.update_cover_art_size()
        if visible:
            # Let the layout settle once, then re-pin to the bottom after the
            # cover repaint in case the content height changed again.
            QTimer.singleShot(0, self._scroll_main_to_bottom)
            QTimer.singleShot(80, self._scroll_main_to_bottom)
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
            if track.get("_is_header"):
                item.setHidden(bool(query))
                continue
            haystack = f"{track.get('title', '')} {track.get('artist', '')} {track.get('album', '')}".lower()
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
            self.library_tracks.sort(key=lambda t: (t.get('album', '').lower(), int(t.get('disc_no', 1)), int(t.get('track_no', 0)), t.get('title', '').lower()), reverse=reverse)
        elif group_idx == 4:
            self.library_tracks.sort(key=lambda t: (t.get('artist', '').lower(), t.get('album', '').lower(), int(t.get('disc_no', 1)), int(t.get('track_no', 0)), t.get('title', '').lower()), reverse=reverse)

        self._refresh_playlist_labels()
        self._rebuild_playlist_widget(current_path)
        self.save_session_state()

    def _on_mini_cover_ready(self, path, qimg):
        mem = getattr(self, "_visible_covers", None)
        if mem is None:
            mem = {}
            self._visible_covers = mem
        pm = QPixmap.fromImage(qimg) if (qimg is not None and not qimg.isNull()) else None
        while len(mem) > 160:
            mem.pop(next(iter(mem)))
        mem[path] = pm
        pending = getattr(self, "_mini_cover_pending", None)
        if pending is not None:
            pending.discard(path)
        futures = getattr(self, "_mini_cover_futures", None)
        if futures is not None:
            futures.pop(path, None)
        if hasattr(self, "list_widget") and not getattr(self, "_cover_repaint_queued", False):
            self._cover_repaint_queued = True
            QTimer.singleShot(12, self._flush_cover_repaint)

    def _flush_cover_repaint(self):
        self._cover_repaint_queued = False
        if hasattr(self, "list_widget"):
            self.list_widget.viewport().update()

    def _flush_queued_covers(self):
        q = getattr(self, "_mini_cover_queue", None)
        if not q:
            return
        pending = getattr(self, "_mini_cover_pending", None)
        if pending is None:
            pending = set()
            self._mini_cover_pending = pending

        # Prioritize strictly what is currently visible in the viewport right now
        visible_paths = []
        if hasattr(self, "list_widget"):
            vp = self.list_widget.viewport().rect()
            top_idx = self.list_widget.indexAt(vp.topLeft())
            bot_idx = self.list_widget.indexAt(vp.bottomLeft())
            r0 = max(0, top_idx.row() if top_idx.isValid() else 0)
            r1 = min(self.list_widget.count() - 1, (bot_idx.row() if bot_idx.isValid() else r0 + 25) + 2)
            for r in range(r0, r1 + 1):
                it = self.list_widget.item(r)
                if it is None or it.isHidden():
                    continue
                d = it.data(Qt.UserRole)
                if isinstance(d, dict):
                    p = d.get("sample_path") if d.get("_is_header") else d.get("path")
                    if p and p not in visible_paths:
                        visible_paths.append(p)

        batch = visible_paths if visible_paths else list(q)[-14:]
        q.clear()

        # Rows that scrolled out of view no longer need their work. Cancel the
        # queued ones immediately so visible rows are not stuck behind them.
        futures = getattr(self, "_mini_cover_futures", None)
        if futures is None:
            futures = {}
            self._mini_cover_futures = futures
        desired = set(batch)
        for p, future in list(futures.items()):
            if p not in desired and not future.done() and future.cancel():
                futures.pop(p, None)
                pending.discard(p)

        def _decode_job(p, sig):
            img = None
            try:
                raw = _extract_cover_bytes(p)
                if raw:
                    img = _decode_mini_thumb(raw)
            except Exception:
                img = None
            sig.loaded.emit(p, img)

        for p in batch:
            if p not in pending and p not in getattr(self, "_visible_covers", {}):
                pending.add(p)
                future = self._mini_cover_pool.submit(_decode_job, p, self._mini_cover_sig)
                futures[p] = future

    def _get_mini_cover_async(self, path):
        if not path:
            return None
        mem = getattr(self, "_visible_covers", None)
        if mem is None:
            mem = {}
            self._visible_covers = mem
        if path in mem:
            return mem[path]
        pending = getattr(self, "_mini_cover_pending", None)
        if pending is not None and path in pending:
            return None
        # Mutagen is imported inside the worker on first decode; doing it here
        # would block the UI thread for the first scroll.
        if not hasattr(self, "_mini_cover_sig"):
            class _CovBridge(QObject):
                loaded = Signal(str, object)
            self._mini_cover_sig = _CovBridge(self)
            self._mini_cover_sig.loaded.connect(self._on_mini_cover_ready)
            from concurrent.futures import ThreadPoolExecutor
            # Four small decoders keep visible rows filling at an even pace;
            # tag/folder reads are cached and cancellation drops stale rows.
            self._mini_cover_pool = ThreadPoolExecutor(max_workers=4)
            self._mini_cover_futures = {}
            self._mini_cover_queue = []
            self._mini_cover_debounce = QTimer(self)
            self._mini_cover_debounce.setSingleShot(True)
            self._mini_cover_debounce.setInterval(20)
            self._mini_cover_debounce.timeout.connect(self._flush_queued_covers)
        q = self._mini_cover_queue
        if path not in q:
            q.append(path)
            if len(q) > 36:
                del q[:len(q) - 36]
        self._mini_cover_debounce.start()
        return None

    def _get_mini_cover(self, path):
        if not path:
            return None
        cache = getattr(self, "_mini_cover_cache", None)
        if cache is None:
            cache = {}
            self._mini_cover_cache = cache
        folder_key = os.path.dirname(os.path.abspath(path))
        if folder_key in cache:
            return cache[folder_key]
        raw = _extract_cover_bytes(path)
        pm = None
        if raw:
            img = _decode_mini_thumb(raw)
            if img is not None and not img.isNull():
                pm = QPixmap.fromImage(img)
        if len(cache) > 250:
            cache.clear()
        cache[folder_key] = pm
        return pm

    def _format_track_label(self, track):
        group_idx = self.combo_group.currentIndex() if hasattr(self, "combo_group") else 0
        t_no = int(track.get('track_no', 0) or 0)
        if group_idx in (3, 4):
            num_col = f"{t_no:02d}   " if t_no > 0 else "—    "
            return f"{num_col}{track['title']} - {track['artist']}"
        elif group_idx == 2:
            return f"{track['artist']} - {track['title']}"
        return f"{track['title']} - {track['artist']}"

    def _refresh_playlist_labels(self):
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            track = item.data(Qt.UserRole)
            if isinstance(track, dict) and not track.get("_is_header"):
                item.setText(self._format_track_label(track))

    def _rebuild_playlist_widget(self, current_path=None):
        self.list_widget.setUpdatesEnabled(False)
        self.list_widget.blockSignals(True)
        if hasattr(self, '_visible_covers'): self._visible_covers.clear()
        self.list_widget.clear()
        self.current_track_index = -1
        group_idx = self.combo_group.currentIndex() if hasattr(self, "combo_group") else 0
        use_headers = group_idx in (3, 4)

        album_counts = {}
        if use_headers:
            for t in self.library_tracks:
                k = (t.get('artist', 'Unknown Artist') if group_idx == 4 else "", t.get('album', 'Unknown Album'))
                album_counts[k] = album_counts.get(k, 0) + 1

        last_key = None
        for i, track in enumerate(self.library_tracks):
            if use_headers:
                art_k = track.get('artist', 'Unknown Artist') if group_idx == 4 else ""
                alb_k = track.get('album', 'Unknown Album')
                cur_key = (art_k, alb_k)
                if cur_key != last_key:
                    last_key = cur_key
                    hdr = QListWidgetItem("")
                    hdr.setFlags(Qt.NoItemFlags)
                    hdr.setData(Qt.UserRole, {
                        "_is_header": True,
                        "artist": track.get('artist', 'Unknown Artist'),
                        "album": alb_k,
                        "count": album_counts.get(cur_key, 1),
                        "sample_path": track.get('path', ''),
                    })
                    self.list_widget.addItem(hdr)
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

    def _show_track_properties(self, track):
        path = track.get("path", "")
        dlg = QDialog(self)
        dlg.setWindowTitle("Track Properties")
        dlg.setAttribute(Qt.WA_TranslucentBackground, windows_transparency_enabled())
        dlg.setAttribute(Qt.WA_NoSystemBackground, True)
        dlg.setAttribute(Qt.WA_ContentsMarginsRespectsSafeArea, False)
        dlg.setMinimumSize(500, 580)

        outer = QVBoxLayout(dlg)
        outer.setContentsMargins(0, 0, 0, 0)
        root = QFrame(dlg)
        outer.addWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(18, 40 if sys.platform == "darwin" else 18, 18, 18)
        lay.setSpacing(12)

        card = make_glass_card()
        card_v = QVBoxLayout(card)
        card_v.setContentsMargins(16, 14, 16, 14)
        card_v.setSpacing(12)

        raw_cov = _extract_cover_bytes(path)
        full_pm = QPixmap()
        has_cov = bool(raw_cov and full_pm.loadFromData(raw_cov) and not full_pm.isNull())
        if has_cov:
            from PySide6.QtGui import QPainterPath
            cov_row = QHBoxLayout()
            cov_btn = QPushButton()
            cov_btn.setCursor(Qt.PointingHandCursor)
            cov_btn.setFixedSize(136, 136)
            cov_btn.setToolTip(f"Click to view full resolution ({full_pm.width()} × {full_pm.height()} px)")
            cov_btn.setStyleSheet("padding: 0px; border: 1px solid rgba(255,255,255,45); border-radius: 12px; background: transparent;")
            thumb = QPixmap(136, 136)
            thumb.fill(Qt.transparent)
            pt = QPainter(thumb)
            pt.setRenderHint(QPainter.Antialiasing, True)
            cp = QPainterPath()
            cp.addRoundedRect(0, 0, 136, 136, 11, 11)
            pt.setClipPath(cp)
            pt.drawPixmap(0, 0, full_pm.scaled(136, 136, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))
            pt.end()
            cov_btn.setIcon(QIcon(thumb))
            cov_btn.setIconSize(QSize(136, 136))

            def _open_full_cover(_=False, pm=full_pm):
                class _CoverInspectDialog(QDialog):
                    def __init__(self, pix, parent=None):
                        super().__init__(parent)
                        self._pix = pix
                        self._inspect = False
                        self._held_fs = False
                        self._norm_geom = None
                        self._cursor_norm = QPointF(0.5, 0.5)
                        self.setWindowTitle(f"Cover Art — {pix.width()} × {pix.height()} px (Click: Full Window • Hold Left-Click: Loupe)")
                        self.setMouseTracking(True)
                        scr = (parent.window().screen() or QApplication.primaryScreen()).availableGeometry()
                        max_w, max_h = int(scr.width() * 0.78), int(scr.height() * 0.78)
                        base = pix if (pix.width() <= max_w and pix.height() <= max_h) else pix.scaled(max_w, max_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                        self.resize(base.width() + 24, base.height() + 24)
                        self.setCursor(Qt.CrossCursor)

                    def _target_rect(self):
                        r = self.rect().adjusted(0, 0, 0, 0) if self._held_fs else self.rect().adjusted(12, 12, -12, -12)
                        pw, ph = max(1, self._pix.width()), max(1, self._pix.height())
                        scale = min(r.width() / pw, r.height() / ph)
                        if not self._held_fs:
                            scale = min(1.0, scale)
                        dw, dh = int(round(pw * scale)), int(round(ph * scale))
                        dx = r.left() + (r.width() - dw) // 2
                        dy = r.top() + (r.height() - dh) // 2
                        from PySide6.QtCore import QRect
                        return QRect(dx, dy, dw, dh)

                    def _toggle_fs(self):
                        if not self._held_fs:
                            self._norm_geom = self.geometry()
                            self._norm_flags = self.windowFlags()
                            self._held_fs = True
                            scr = (self.screen() or QApplication.primaryScreen()).availableGeometry()
                            self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
                            self.setGeometry(scr)
                        else:
                            self._held_fs = False
                            self.setWindowFlags(getattr(self, "_norm_flags", Qt.Dialog))
                            if self._norm_geom is not None:
                                self.setGeometry(self._norm_geom)
                        self.show()
                        self.raise_()
                        self.activateWindow()
                        self.update()

                    def _enable_loupe(self):
                        if getattr(self, "_btn_down", False):
                            self._did_hold = True
                            self._inspect = True
                            self.setCursor(Qt.BlankCursor)
                            self.update()

                    def keyPressEvent(self, ev):
                        if ev.key() == Qt.Key_Escape:
                            if self._held_fs:
                                self._toggle_fs()
                            else:
                                self.close()
                            ev.accept()
                            return
                        super().keyPressEvent(ev)

                    def mousePressEvent(self, ev):
                        if ev.button() == Qt.LeftButton:
                            self._btn_down = True
                            self._did_hold = False
                            if not hasattr(self, "_loupe_hold_timer"):
                                self._loupe_hold_timer = QTimer(self)
                                self._loupe_hold_timer.setSingleShot(True)
                                self._loupe_hold_timer.setInterval(180)
                                self._loupe_hold_timer.timeout.connect(self._enable_loupe)
                            self._loupe_hold_timer.start()
                            ev.accept()

                    def mouseDoubleClickEvent(self, ev):
                        self.mousePressEvent(ev)

                    def mouseReleaseEvent(self, ev):
                        if ev.button() == Qt.LeftButton:
                            self._btn_down = False
                            if hasattr(self, "_loupe_hold_timer"):
                                self._loupe_hold_timer.stop()
                            if self._inspect or getattr(self, "_did_hold", False):
                                self._inspect = False
                                self.setCursor(Qt.CrossCursor)
                                self.update()
                            else:
                                self._toggle_fs()
                            ev.accept()

                    def mouseMoveEvent(self, ev):
                        tr = self._target_rect()
                        if tr.width() > 0 and tr.height() > 0:
                            nx = max(0.0, min(1.0, (ev.position().x() - tr.left()) / float(tr.width())))
                            ny = max(0.0, min(1.0, (ev.position().y() - tr.top()) / float(tr.height())))
                            self._cursor_norm = QPointF(nx, ny)
                            if self._inspect:
                                self.update()

                    def paintEvent(self, ev):
                        from PySide6.QtGui import QPen
                        p = QPainter(self)
                        p.setRenderHint(QPainter.Antialiasing, True)
                        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
                        p.fillRect(self.rect(), QColor(8, 10, 14, 250))
                        tr = self._target_rect()
                        p.drawPixmap(tr, self._pix)
                        if self._inspect and tr.width() > 20:
                            from PySide6.QtCore import QRect
                            from PySide6.QtGui import QPainterPath
                            zoom = 2.5
                            lw = max(160, int(self.width() * 0.72))
                            lh = max(160, int(self.height() * 0.72))
                            cx = tr.left() + self._cursor_norm.x() * tr.width()
                            cy = tr.top() + self._cursor_norm.y() * tr.height()
                            dst_x = max(0, min(self.width() - lw, int(round(cx - lw * 0.5))))
                            dst_y = max(0, min(self.height() - lh, int(round(cy - lh * 0.5))))
                            dst_rect = QRect(dst_x, dst_y, lw, lh)
                            src_w = (lw / zoom) * (self._pix.width() / float(tr.width()))
                            src_h = (lh / zoom) * (self._pix.height() / float(tr.height()))
                            sx = max(0.0, min(self._pix.width() - src_w, self._cursor_norm.x() * self._pix.width() - src_w * 0.5))
                            sy = max(0.0, min(self._pix.height() - src_h, self._cursor_norm.y() * self._pix.height() - src_h * 0.5))
                            clip = QPainterPath()
                            clip.addRoundedRect(dst_rect, 12, 12)
                            p.setClipPath(clip)
                            p.drawPixmap(dst_rect, self._pix, QRect(int(sx), int(sy), max(1, int(src_w)), max(1, int(src_h))))
                            p.setClipping(False)
                            p.setPen(QPen(QColor(255, 255, 255, 215), 2.0))
                            p.drawRoundedRect(dst_rect, 12, 12)
                        p.end()

                viewer = _CoverInspectDialog(pm, dlg)
                if getattr(self, "_cover_viewer", None) is not None:
                    try:
                        self._cover_viewer.close()
                    except Exception:
                        pass
                self._cover_viewer = viewer
                viewer.setAttribute(Qt.WA_DeleteOnClose, True)
                viewer.destroyed.connect(lambda: setattr(self, "_cover_viewer", None))
                viewer.show()
                viewer.raise_()
                viewer.activateWindow()

            cov_btn.clicked.connect(_open_full_cover)
            cov_info = QVBoxLayout()
            lbl_dim = QLabel(f"Cover Art: {full_pm.width()} × {full_pm.height()} px")
            lbl_dim.setStyleSheet("font-weight: bold; font-size: 12px;")
            lbl_hint = QLabel("Click cover to inspect full resolution")
            lbl_hint.setStyleSheet(f"font-size: 11px; color: {self._muted_text_color()};")
            cov_info.addStretch()
            cov_info.addWidget(lbl_dim)
            cov_info.addWidget(lbl_hint)
            cov_info.addStretch()
            cov_row.addWidget(cov_btn)
            cov_row.addSpacing(12)
            cov_row.addLayout(cov_info, 1)
            card_v.addLayout(cov_row)

        g = QGridLayout()
        card_v.addLayout(g)
        g.setHorizontalSpacing(14)
        g.setVerticalSpacing(8)
        g.setColumnStretch(1, 1)

        codec_str, sr_str, bits_str, br_str, dur_str, size_str = "-", "-", "-", "-", "-", "-"
        if os.path.exists(path):
            sz = os.path.getsize(path)
            size_str = f"{sz / (1024 * 1024):.2f} MB ({sz:,} bytes)"
            try:
                cmd = [get_binary_path("ffprobe"), "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path]
                res = subprocess.run(cmd, capture_output=True, text=True, **SUBPROCESS_FLAGS)
                info = json.loads(res.stdout)
                st = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), {})
                fmt = info.get("format", {})
                codec_str = f"{st.get('codec_long_name') or st.get('codec_name', '-')} ({st.get('channels', 2)} ch)"
                sr_val = int(st.get("sample_rate") or 0)
                if sr_val:
                    sr_str = f"{sr_val / 1000.0:g} kHz ({sr_val} Hz)"
                bps = int(st.get("bits_per_raw_sample") or st.get("bits_per_sample") or 0)
                bits_str = f"{bps}-bit" if bps > 0 else "32-bit float / lossy"
                br = int(st.get("bit_rate") or fmt.get("bit_rate") or 0)
                if br > 0:
                    br_str = f"{br // 1000} kbps"
                dur = float(fmt.get("duration") or st.get("duration") or 0.0)
                if dur > 0:
                    dur_str = self.format_time(dur)
            except Exception:
                pass

        t_no = int(track.get("track_no", 0) or 0)
        d_no = int(track.get("disc_no", 1) or 1)
        rows = [
            ("Title:", track.get("title", "-")),
            ("Artist:", track.get("artist", "-")),
            ("Album:", track.get("album", "-")),
            ("Track / Disc:", f"#{t_no:02d} (Disc {d_no})" if t_no > 0 else f"Disc {d_no}"),
            ("Duration:", dur_str),
            ("Codec:", codec_str),
            ("Sample Rate:", sr_str),
            ("Bit Depth:", bits_str),
            ("Bitrate:", br_str),
            ("File Size:", size_str),
            ("Location:", path),
        ]
        for r_idx, (k, v) in enumerate(rows):
            kl = QLabel(k)
            kl.setStyleSheet(f"font-weight: bold; color: {self._muted_text_color()};")
            vl = QLabel(str(v))
            vl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            vl.setWordWrap(True)
            g.addWidget(kl, r_idx, 0, Qt.AlignTop)
            g.addWidget(vl, r_idx, 1)

        lay.addWidget(card)
        btn_row = QHBoxLayout()
        btn_rev = QPushButton("Show in Finder" if sys.platform == "darwin" else "Show in Folder")
        btn_rev.clicked.connect(lambda: self._reveal_in_folder(path))
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(dlg.accept)
        btn_row.addWidget(btn_rev)
        btn_row.addStretch()
        btn_row.addWidget(btn_close)
        lay.addLayout(btn_row)

        dlg.setStyleSheet(self.styleSheet())
        dlg.mw = self  # let configure_macos_glass see Low-End CPU mode
        if is_glass_theme(self.current_theme) and glass:
            glass.apply_glass_to_window(dlg)
            configure_macos_glass(dlg, self.current_theme, True,
                                  getattr(self, "glass_clarity", 45) / 100.0,
                                  material=getattr(self, "glass_material", None))
        apply_windows_glass(dlg, is_glass_theme(self.current_theme), self.current_theme == "light")
        apply_seamless_titlebar(dlg)
        if getattr(self, "_props_dlg", None) is not None:
            try:
                self._props_dlg.close()
            except Exception:
                pass
        self._props_dlg = dlg
        dlg.setAttribute(Qt.WA_DeleteOnClose, True)
        dlg.destroyed.connect(lambda: setattr(self, "_props_dlg", None))
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _reveal_in_folder(self, path):
        if not path or not os.path.exists(path):
            QMessageBox.warning(self, "File Not Found", f"Audio file no longer exists on disk:\n{path}")
            return
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        elif sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(os.path.abspath(path))])

    def _show_playlist_context_menu(self, pos):
        item = self.list_widget.itemAt(pos)
        if item is None:
            return
        track = item.data(Qt.UserRole)
        if not isinstance(track, dict) or track.get("_is_header"):
            return
        if not item.isSelected():
            self.list_widget.setCurrentItem(item)

        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)
        menu.setAttribute(Qt.WA_TranslucentBackground, True)
        theme = getattr(self, "current_theme", "classic")
        is_light = is_light_surface(theme, getattr(self, "glass_appearance", "dark"))
        is_glass = is_glass_theme(theme)
        accent = getattr(self, "current_accent", "#0a84ff")
        if is_light:
            bg, fg, brd = "rgba(255, 255, 255, 238)", "#1c1c1e", "rgba(0, 0, 0, 28)"
            sep = "rgba(0, 0, 0, 22)"
        elif is_glass:
            bg, fg, brd = "rgba(24, 28, 38, 225)", "#f5f7fa", "rgba(255, 255, 255, 42)"
            sep = "rgba(255, 255, 255, 28)"
        else:
            bg, fg, brd = "rgba(28, 30, 36, 245)", "#eef2f8", "rgba(255, 255, 255, 24)"
            sep = "rgba(255, 255, 255, 20)"
        menu.setStyleSheet(f"""
            QMenu {{
                background-color: {bg};
                color: {fg};
                border: 1px solid {brd};
                border-radius: 12px;
                padding: 6px;
                font-size: 13px;
            }}
            QMenu::item {{
                padding: 7px 18px 7px 14px;
                border-radius: 8px;
                margin: 1px 0px;
            }}
            QMenu::item:selected {{
                background-color: {accent};
                color: #ffffff;
            }}
            QMenu::separator {{
                height: 1px;
                background: {sep};
                margin: 5px 10px;
            }}
        """)
        act_play = menu.addAction("Play Track")
        reveal_lbl = "Show in Finder" if sys.platform == "darwin" else ("Show in Explorer" if sys.platform == "win32" else "Show in Folder")
        act_reveal = menu.addAction(reveal_lbl)
        act_props = menu.addAction("Properties...")
        menu.addSeparator()
        act_remove = menu.addAction("Remove from Playlist")

        chosen = menu.exec(self.list_widget.viewport().mapToGlobal(pos))
        if chosen == act_play:
            self.play_selected_item(item)
        elif chosen == act_reveal:
            self._reveal_in_folder(track.get("path", ""))
        elif chosen == act_props:
            self._show_track_properties(track)
        elif chosen == act_remove:
            self.remove_selected_tracks()

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
            if hasattr(self, "glass_lens"):
                self.glass_lens.set_source(None)
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
                "current_path": self.library_tracks[self.current_track_index]["path"] if 0 <= self.current_track_index < len(self.library_tracks) else "",
                "position_sec": pos_sec,
                "volume": getattr(self, "_pre_bp_vol", getattr(self, "_pre_live_vol", int(round(self.engine.volume * 100)))) if (self.engine.bit_perfect or self._bp_ramp_timer.isActive() or self.engine.use_mac_tap) else int(round(self.engine.volume * 100)),
                "shuffle": self.shuffle_enabled,
                "stereo_scope_open": self.btn_stereo_scope.isChecked() if hasattr(self, "btn_stereo_scope") else False,
                "theme": getattr(self, "current_theme", "liquid_glass"),
                "ambient_opacity": getattr(self, "ambient_opacity", 0.30),
                "theme_glow": getattr(self, "theme_glow", {"classic": 0.20, "light": 0.30, "liquid_glass": 1.00, "macos27_glass": 1.00}),
                "glass_material": getattr(self, "glass_material", "automatic"),
                "glass_appearance": getattr(self, "glass_appearance", "dark"),
                "glass_overrides": getattr(self, "glass_overrides", {}),
                "glass_clarity": getattr(self, "glass_clarity", 45),
                "glass_rim": getattr(self, "glass_rim", DEFAULT_GLASS_RIM),
                "low_end_mode": self.engine.low_end_mode,
                "spectrum_fill": getattr(self, "spectrum_fill_enabled", True),
                "dynamic_ambient": getattr(self, "dynamic_ambient", True),
                "scrobble_cfg": getattr(self, "scrobble_cfg", {}),
                "sofa_path": self.engine.sofa_path if (self.engine.sofa_path and os.path.exists(self.engine.sofa_path) and os.path.abspath(self.engine.sofa_path) != os.path.abspath(get_resource_path("default.sofa"))) else "",
            }
            state_path = self._state_file_path()
            # Atomic replace prevents a startup read (or another REXA process)
            # from seeing a half-written JSON file and losing saved tuning.
            tmp = tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=os.path.dirname(state_path),
                prefix=".session-", suffix=".tmp", delete=False
            )
            try:
                with tmp:
                    json.dump(state, tmp, indent=2)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                os.replace(tmp.name, state_path)
            except Exception:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
                raise
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

            if isinstance(state.get("scrobble_cfg"), dict):
                self.scrobble_cfg.update(state["scrobble_cfg"])
            self.toggle_spectrum_fill(bool(state.get("spectrum_fill", True)))
            self.toggle_dynamic_ambient(bool(state.get("dynamic_ambient", True)))
            low = bool(state.get("low_end_mode", False))
            self.engine.low_end_mode = low
            if hasattr(self, "btn_cpu_mode"):
                self.btn_cpu_mode.blockSignals(True)
                self.btn_cpu_mode.setChecked(low)
                if hasattr(self, "btn_cpu_mode"): self.btn_cpu_mode.setText("CPU: Low-End" if low else "CPU: Full Quality")
                self.btn_cpu_mode.blockSignals(False)
                if low:
                    self.combo_fps.setCurrentIndex(0)
            theme = state.get("theme", "liquid_glass")
            saved_glow = state.get("theme_glow")
            if isinstance(saved_glow, dict):
                self.theme_glow.update({k: float(v) for k, v in saved_glow.items() if k in self.theme_glow})
            self.ambient_opacity = self.theme_glow.get(theme, float(state.get("ambient_opacity", 1.0 if is_glass_theme(theme) else 0.30)))
            legacy_clarity = int(state.get("glass_clarity", 45))
            self.glass_clarity = legacy_clarity
            # Legacy sessions had a material modifier; fold it into the two
            # stages so the UI only ever exposes Dark and Light.
            appearance = state.get("glass_appearance")
            if appearance not in GLASS_STAGES:
                legacy_material = canonical_glass_material(state.get("glass_material", "automatic"))
                if legacy_material == "clear":
                    appearance = "light"
                elif legacy_material == "dark":
                    appearance = "dark"
                else:
                    appearance = "dark" if legacy_clarity >= 70 else "light"
            self.glass_appearance = appearance
            self.glass_material = GLASS_STAGE_MATERIAL[appearance]
            self.glass_overrides = {}
            saved_overrides = state.get("glass_overrides")
            if isinstance(saved_overrides, dict):
                for key, value in saved_overrides.items():
                    k = str(key).strip().lower()
                    if k in GLASS_STAGES:
                        stage = k
                    else:
                        legacy_key = canonical_glass_material(k)
                        stage = ("dark" if legacy_key == "dark"
                                 else "light" if legacy_key == "clear"
                                 else "dark" if legacy_clarity >= 70 else "light")
                    if not stage:
                        continue
                    per = normalize_glass_overrides(value, stage)
                    if per:
                        self.glass_overrides[stage] = per
            self.glass_rim = int(state.get("glass_rim", DEFAULT_GLASS_RIM))
            if hasattr(self, "slider_glass_clarity"):
                self.slider_glass_clarity.blockSignals(True); self.slider_glass_clarity.setValue(self.glass_clarity); self.slider_glass_clarity.blockSignals(False)
                self.slider_glass_rim.blockSignals(True); self.slider_glass_rim.setValue(self.glass_rim); self.slider_glass_rim.blockSignals(False)
            if hasattr(self, "slider_glow"):
                self.slider_glow.blockSignals(True)
                self.slider_glow.setValue(int(self.ambient_opacity * 100))
                self.slider_glow.blockSignals(False)
            if theme in THEME_ORDER and self.current_theme != theme:
                self.current_theme = THEME_ORDER[(THEME_ORDER.index(theme) - 1) % len(THEME_ORDER)]
                self.toggle_theme()
            self._reapply_native_glass()
            # Native glass is one half of the panel; QSS scrim is the other.
            # Rebuild it here too, because toggle_theme() only runs when the
            # saved theme differs from the startup default.
            self._apply_theme_style()
            # DSP controls always start closed so the default proportions and
            # hidden scrollbar are predictable, regardless of older sessions.
            self.btn_toggle_dsp_panel.setChecked(False)
            self.toggle_dsp_panel(False)
            stereo_open = bool(state.get("stereo_scope_open", False))
            if hasattr(self, "btn_stereo_scope"):
                self.btn_stereo_scope.setChecked(stereo_open)
                self.toggle_stereo_scope(stereo_open)

            sofa = state.get("sofa_path", "")
            if sofa and os.path.exists(sofa) and sofa != self.engine.sofa_path:
                self.lbl_sofa_file.setText(os.path.basename(sofa))
                self.engine.load_sofa(sofa)

            saved_tracks = [t for t in state.get("tracks", []) if t.get("path")]
            if not saved_tracks:
                return

            saved_custom = [t for t in state.get("custom_tracks", []) if t.get("path")]
            group_mode = state.get("group_mode", 4)
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
            needs_repair = [t for t in self.library_tracks if 'album' not in t or 'track_no' not in t]
            if needs_repair:
                self.repair_thread = MetadataRepairThread(needs_repair)
                self.repair_thread.finished_repair.connect(self.on_repair_finished)
                self.repair_thread.start()
            saved_cur_path = state.get("current_path", "")
            idx = state.get("current_index", -1)
            if saved_cur_path:
                found_idx = next((i for i, t in enumerate(self.library_tracks) if t.get("path") == saved_cur_path), -1)
                if found_idx != -1:
                    idx = found_idx
            pos_sec = float(state.get("position_sec", 0.0))

            if 0 <= idx < len(self.library_tracks):
                track = self.library_tracks[idx]
                item = next((self.list_widget.item(r) for r in range(self.list_widget.count()) if (self.list_widget.item(r).data(Qt.UserRole) or {}).get("path") == track["path"]), None)
                if item is not None:
                    self.list_widget.setCurrentItem(item)
                    self.list_widget.scrollToItem(item, QAbstractItemView.PositionAtCenter)
                self.current_track_index = idx
                self.lbl_track_title.setText(track['title'])
                self.lbl_track_artist.setText(track['artist'])

                cover_bytes = _extract_cover_bytes(track['path'])
                if cover_bytes:
                    pixmap = QPixmap()
                    pixmap.loadFromData(cover_bytes)
                    self._raw_cover_pixmap = pixmap.scaled(440, 440, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                    self._last_cover_render_key = None
                    self.update_cover_art_size()

                self._current_meta = dict(track)
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
        # Cover and top controls keep the stock 820px-window proportion; the
        # taller default window adds its extra height only to the spectrum.
        h = min(self.height(), 820)
        w = self.width()
        # DSP controls live in the scroll area now; opening them must not
        # shrink the cover, so size only from the viewport height.
        free_for_cover = (h - 555) * 0.72
        size = max(108, min(280, int(free_for_cover)))
        stepped_size = size

        if hasattr(self, "stereo_analyzer"):
            sa_w = max(205, min(295, int(w * 0.185)))
            self.stereo_analyzer.setFixedWidth(sa_w)

        self.lbl_cover.setMinimumSize(100, 100)
        self.lbl_cover.setFixedSize(stepped_size, stepped_size)

        raw = getattr(self, "_raw_cover_pixmap", None)
        render_key = (id(raw), stepped_size)
        if getattr(self, "_last_cover_render_key", None) != render_key:
            self._last_cover_render_key = render_key
            r, g, b = sample_ambient_rgb(raw)
            if hasattr(self, "lava_overlay"):
                self.lava_overlay.set_palette(sample_ambient_palette(raw))
            
            # Background, accent, and lava all switch to the new cover together.
            # The old fade left the previous hue visible while the other colors
            # had already changed, which read as a wrong-track background.
            target_color = QColor(r, g, b)
            self.current_accent, self.current_accent_end = self._get_accent_color(r, g, b)
            self._current_ambient_color = target_color
            self._apply_theme_style(target_color)
                
            if raw is not None and not raw.isNull():
                self.lbl_cover.setPixmap(
                    raw.scaled(stepped_size, stepped_size, Qt.KeepAspectRatioByExpanding, Qt.FastTransformation if getattr(self, '_is_resizing', False) else Qt.SmoothTransformation)
                )

        if hasattr(self, "glass_lens"):
            self.glass_lens.sync_geometry()
            self.glass_lens.set_source(self.lbl_cover.pixmap())
            self.glass_lens.sync_active()

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

    def event(self, event):
        # AppKit can defer/rerender NSGlassEffectView material on show or
        # activation changes. Without a fresh push the panels read too
        # transparent until the user opens and closes Settings.
        if event.type() in (QEvent.Show, QEvent.WindowActivate):
            if not getattr(self, "_native_glass_refresh_pending", False):
                self._native_glass_refresh_pending = True
                QTimer.singleShot(0, self._flush_native_glass_refresh)
        return super().event(event)

    def _flush_native_glass_refresh(self):
        self._native_glass_refresh_pending = False
        self._reapply_native_glass()

    def showFullScreen(self):
        self._pre_fullscreen_geometry = QRect(self.geometry())
        super().showFullScreen()

    def showNormal(self):
        was_fullscreen = self.isFullScreen()
        saved = getattr(self, "_pre_fullscreen_geometry", None)
        self._reset_library_height_for_shrink()
        # Exiting fullscreen goes through the native window path, not
        # WindowDragFilter._animate_to(), so own the same resize guard here.
        self._is_resizing = True
        def _finish():
            self._is_resizing = False
            self._sync_main_content_height()
            self.update_cover_art_size()
        QTimer.singleShot(420, _finish)
        super().showNormal()
        if was_fullscreen and saved is not None:
            # Settle in this event turn so the fullscreen-sized spectrum is
            # never painted after the window returns to normal.
            for _ in range(8):
                self.setGeometry(saved)
                QApplication.processEvents()
                if self.geometry().size() == saved.size():
                    break
            self._pre_fullscreen_geometry = None

    def changeEvent(self, event):
        if event.type() == QEvent.WindowStateChange:
            if hasattr(self, "lbl_cover"):
                self.lbl_cover.setMinimumSize(100, 100)
            if not self.isFullScreen():
                for ms in (50, 350, 750):
                    QTimer.singleShot(ms, lambda: (apply_seamless_titlebar(self), self.setGeometry(self.screen().availableGeometry()) if getattr(getattr(self, '_drag_filter', None), '_was_zoomed', False) else None))
        super().changeEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_main_content_height()
        QTimer.singleShot(0, self._sync_main_content_height)
        if hasattr(self, "lava_overlay") and self.centralWidget():
            self.lava_overlay.setGeometry(self.centralWidget().rect())
            self.lava_overlay.lower()
        if hasattr(self, "lbl_cover"):
            self.update_cover_art_size()


    def closeEvent(self, event):
        for attr in ("_cover_viewer", "_props_dlg", "_settings_dlg"):
            d = getattr(self, attr, None)
            if d is not None:
                try:
                    d.close()
                except Exception:
                    pass
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
    if is_glass_theme(window.current_theme):
        if glass and getattr(window, "glass_effect_id", None) is None:
            window.glass_effect_id = glass.apply_glass_to_window(window)
        if glass:
            configure_macos_glass(window, window.current_theme, True,
                                  getattr(window, "glass_clarity", 45) / 100.0,
                                  material=getattr(window, "glass_material", None))
        apply_windows_glass(window, True, False)
        c = window._current_ambient_color
        window.setStyleSheet(build_liquid_qss(
            c.red(), c.green(), c.blue(),
            theme=window.current_theme,
            ambient_opacity=window.ambient_opacity,
            accent=window.current_accent,
            accent_end=window.current_accent_end,
            glass_clarity=window.glass_clarity,
            glass_rim=window.glass_rim,
            glass_material=getattr(window, "glass_material", None),
            glass_overrides=getattr(window, "glass_overrides", None),
            glass_appearance=getattr(window, "glass_appearance", "dark")
        ))
    apply_seamless_titlebar(window)
    window.raise_()
    window.activateWindow()
    sys.exit(app.exec())
