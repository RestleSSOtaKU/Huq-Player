import os, sys, time, ctypes
from PySide6.QtWidgets import (QFrame, QAbstractButton, QAbstractSlider, QComboBox,
                               QLineEdit, QAbstractSpinBox, QAbstractItemView, QScrollBar)
from PySide6.QtCore import QObject, QEvent, Qt, QTimer, QRect
from PySide6.QtGui import QImage

def make_glass_card(parent=None, radius=16):
    card = QFrame(parent)
    card.setObjectName("glassCard")
    return card

def windows_transparency_enabled():
    if sys.platform != "win32":
        return True
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            val, _ = winreg.QueryValueEx(k, "EnableTransparency")
            return bool(val)
    except Exception:
        return False


def _sample_wallpaper_luminance():
    """Return main wallpaper luminance in 0..1, or None if unavailable."""
    if sys.platform != "darwin":
        return None
    try:
        from AppKit import NSWorkspace, NSScreen

        ws = NSWorkspace.sharedWorkspace()
        screen = NSScreen.mainScreen()
        url = ws.desktopImageURLForScreen_(screen) if screen else None
        path = url.path() if url else None
        if not path or not os.path.exists(path):
            return None

        img = QImage(path)
        if img.isNull():
            return None
        img = img.scaled(24, 24).convertToFormat(QImage.Format_RGB32)
        total = 0.0
        for y in range(24):
            for x in range(24):
                c = img.pixelColor(x, y)
                total += 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
        return max(0.0, min(1.0, total / (24 * 24 * 255.0)))
    except Exception:
        return None


def read_system_glass_preferences():
    """Best-effort mapping from OS appearance settings to Glass Opacity.

    macOS does not expose the exact Default/Dark/Clear/Tinted choice publicly,
    so we combine the public signals: effective appearance, Reduce
    Transparency, and wallpaper luminance. Windows uses registry values.
    """
    info = {
        "available": False,
        "platform": sys.platform,
        "appearance": "unknown",
        "reduce_transparency": None,
        "wallpaper_luminance": None,
        "suggested_opacity": None,
        "summary": "System glass sync is available on macOS and Windows only.",
    }

    if sys.platform == "darwin":
        try:
            from AppKit import NSWorkspace, NSUserDefaults, NSApp

            ws = NSWorkspace.sharedWorkspace()
            reduce = bool(ws.accessibilityDisplayShouldReduceTransparency())
            defaults = NSUserDefaults.standardUserDefaults()
            style = str(defaults.stringForKey_("AppleInterfaceStyle") or "")
            auto = bool(defaults.boolForKey_("AppleInterfaceStyleSwitchesAutomatically"))
            dark = style.lower() == "dark"
            try:
                if NSApp is not None and hasattr(NSApp, "effectiveAppearance"):
                    name = str(NSApp.effectiveAppearance().name())
                    if "Dark" in name:
                        dark = True
                    elif "Aqua" in name or "Light" in name:
                        dark = False
            except Exception:
                pass

            luma = _sample_wallpaper_luminance()
            if reduce:
                opacity = 100
            else:
                base = 50 if dark else 38
                spread = 28 if dark else 32
                opacity = int(round(base + spread * (0.5 if luma is None else luma)))
                opacity = max(12, min(94, opacity))

            appearance_label = ("Dark" if dark else "Light") + (" (Auto)" if auto else "")
            parts = [
                f"macOS {appearance_label}",
                f"Reduce Transparency {'On' if reduce else 'Off'}",
            ]
            if luma is not None:
                parts.append(f"wallpaper {int(luma * 100)}%")
            parts.append(f"→ Glass Opacity {opacity}%")
            info.update({
                "available": True,
                "appearance": "dark" if dark else "light",
                "reduce_transparency": reduce,
                "wallpaper_luminance": luma,
                "suggested_opacity": opacity,
                "summary": " · ".join(parts),
            })
        except Exception:
            info["summary"] = "Could not read macOS appearance; kept current Glass Opacity."
    elif sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
                try:
                    val, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
                    dark = int(val) == 0
                except OSError:
                    dark = False
                try:
                    val, _ = winreg.QueryValueEx(k, "EnableTransparency")
                    transparent = bool(int(val))
                except OSError:
                    transparent = False

            opacity = 100 if not transparent else (68 if dark else 48)
            parts = [
                f"Windows {'Dark' if dark else 'Light'}",
                f"Transparency {'On' if transparent else 'Off'}",
                f"→ Glass Opacity {opacity}%",
            ]
            info.update({
                "available": True,
                "appearance": "dark" if dark else "light",
                "reduce_transparency": not transparent,
                "suggested_opacity": opacity,
                "summary": " · ".join(parts),
            })
        except Exception:
            info["summary"] = "Could not read Windows theme; kept current Glass Opacity."

    return info

def apply_windows_glass(window, enable=True, light_titlebar=False):
    if sys.platform != "win32":
        return
    try:
        hwnd = int(window.winId())
        dwm = ctypes.windll.dwmapi
        user32 = ctypes.windll.user32

        # Sync native Windows titlebar dark/light appearance
        dark = ctypes.c_int(0 if light_titlebar else 1)
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark))

        class MARGINS(ctypes.Structure):
            _fields_ = [("cxLeftWidth", ctypes.c_int), ("cxRightWidth", ctypes.c_int),
                        ("cyTopHeight", ctypes.c_int), ("cyBottomHeight", ctypes.c_int)]

        margins = MARGINS(-1, -1, -1, -1) if enable else MARGINS(0, 0, 0, 0)
        dwm.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins))

        # Tier 1: Windows 11 22H2+ DWM Acrylic backdrop (3 = Acrylic, 1 = None)
        backdrop = ctypes.c_int(3 if enable else 1)
        hr = dwm.DwmSetWindowAttribute(hwnd, 38, ctypes.byref(backdrop), ctypes.sizeof(backdrop))

        # Tier 2: Windows 10 / pre-22H2 SetWindowCompositionAttribute fallback
        if hr != 0:
            class ACCENT_POLICY(ctypes.Structure):
                _fields_ = [("AccentState", ctypes.c_uint), ("AccentFlags", ctypes.c_uint),
                            ("GradientColor", ctypes.c_uint), ("AnimationId", ctypes.c_uint)]
            class WINDOWCOMPOSITIONATTRIBDATA(ctypes.Structure):
                _fields_ = [("Attribute", ctypes.c_int), ("Data", ctypes.POINTER(ACCENT_POLICY)),
                            ("SizeOfData", ctypes.c_size_t)]
            accent = ACCENT_POLICY(4 if enable else 0, 2, 0x22101218 if enable else 0, 0)
            data = WINDOWCOMPOSITIONATTRIBDATA(19, ctypes.pointer(accent), ctypes.sizeof(accent))
            user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(data))
    except Exception:
        pass

def configure_macos_glass(window, theme="macos27_glass", enable=True, opacity=None):
    """Match the native glass material to the selected theme and opacity.

    ``macos27_glass`` uses the native Clear material with dark vibrancy: that
    is the gentle blur seen in macOS Settings/Control Center. ``opacity`` only
    drives the QSS veil; the native view stays at full alpha so AppKit keeps
    rendering the material.
    """
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSAppearance, NSAppearanceNameVibrantDark
        try:
            from AppKit import NSGlassEffectViewStyleClear, NSGlassEffectViewStyleRegular
        except ImportError:
            # Older AppKit metadata may not expose the constants.
            NSGlassEffectViewStyleClear, NSGlassEffectViewStyleRegular = 1, 0
        from pyqt_liquidglass._bridge import get_nswindow_from_widget

        ns_window = get_nswindow_from_widget(window)
        if ns_window is not None:
            if not enable:
                ns_window.setAppearance_(None)
            else:
                # Both glass themes are dark native material; the references
                # are dark-mode macOS panels over a picture background.
                ns_window.setAppearance_(NSAppearance.appearanceNamed_(NSAppearanceNameVibrantDark))

        glass_view = getattr(window, "_glass_view", None)
        if glass_view is not None and hasattr(glass_view, "setStyle_"):
            # Regular is the dense/mushy material. macOS Settings panels use
            # Clear; keep Original Glass on Regular so its UI stays readable.
            glass_view.setStyle_(
                NSGlassEffectViewStyleClear if (enable and theme == "macos27_glass")
                else NSGlassEffectViewStyleRegular
            )
        if glass_view is not None and hasattr(glass_view, "setCornerRadius_"):
            # Match the rounded window corners so the native material does not
            # leave a square edge / cut-off line under the QSS panels.
            glass_view.setCornerRadius_(14.0 if enable else 0.0)
        if glass_view is not None and hasattr(glass_view, "setNeedsDisplay_"):
            glass_view.setNeedsDisplay_(True)
        # Do NOT lower NSGlassEffectView.alphaValue: AppKit then stops showing
        # the material and the window looks flat. Keep the native view at full
        # strength; the Glass Opacity slider varies the QSS veil instead.
        if glass_view is not None and hasattr(glass_view, "setAlphaValue_"):
            glass_view.setAlphaValue_(1.0)
    except Exception:
        pass


def apply_seamless_titlebar(window):
    if sys.platform != "darwin":
        return
    try:
        objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        def msg(obj, sel, res=ctypes.c_void_p, types=(), *args):
            fn = ctypes.cast(objc.objc_msgSend, ctypes.CFUNCTYPE(res, ctypes.c_void_p, ctypes.c_void_p, *types))
            return fn(obj, objc.sel_registerName(sel.encode()), *args)
        ns_win = msg(ctypes.c_void_p(int(window.winId())), "window")
        if ns_win:
            msg(ns_win, "setTitlebarAppearsTransparent:", None, (ctypes.c_bool,), True)
            msg(ns_win, "setTitleVisibility:", None, (ctypes.c_long,), 1)
            mask = msg(ns_win, "styleMask", ctypes.c_ulong)
            if not (mask & (1 << 15)):
                class NSRect(ctypes.Structure):
                    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double), ("w", ctypes.c_double), ("h", ctypes.c_double)]
                get_f = ctypes.cast(objc.objc_msgSend, ctypes.CFUNCTYPE(NSRect, ctypes.c_void_p, ctypes.c_void_p))
                old_f = get_f(ns_win, objc.sel_registerName(b"frame"))
                msg(ns_win, "setStyleMask:", None, (ctypes.c_ulong,), mask | (1 << 15))
                set_f = ctypes.cast(objc.objc_msgSend, ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, NSRect, ctypes.c_bool))
                set_f(ns_win, objc.sel_registerName(b"setFrame:display:"), old_f, True)
    except Exception:
        pass

def sample_ambient_rgb(pixmap):
    if pixmap is None or pixmap.isNull():
        return (26, 36, 58)
    
    img = pixmap.toImage().scaled(32, 32).convertToFormat(QImage.Format_RGB32)
    
    from collections import defaultdict
    color_buckets = defaultdict(list)
    
    for y in range(32):
        for x in range(32):
            c = img.pixelColor(x, y)
            r, g, b = c.red(), c.green(), c.blue()
            
            # Use proper luminance to avoid skipping bright reds or yellows
            lum = 0.299 * r + 0.587 * g + 0.114 * b
            if lum < 20 or lum > 235:
                continue
                
            mn = min(r, g, b)
            mx = max(r, g, b)
            s = (mx - mn) / mx if mx > 0 else 0
            
            if s > 0.05:
                key = (r // 16, g // 16, b // 16)
                color_buckets[key].append((r, g, b, s))
                
    if not color_buckets:
        total_r = total_g = total_b = count = 0
        for y in range(32):
            for x in range(32):
                c = img.pixelColor(x, y)
                r, g, b = c.red(), c.green(), c.blue()
                lum = 0.299 * r + 0.587 * g + 0.114 * b
                if 20 < lum < 235:
                    total_r += r; total_g += g; total_b += b
                    count += 1
        if count > 0:
            return (total_r // count, total_g // count, total_b // count)
        return (26, 36, 58)
        
    best_score = -1
    best_rgb = (26, 36, 58)
    
    for bucket in color_buckets.values():
        count = len(bucket)
        avg_r = sum(p[0] for p in bucket) / count
        avg_g = sum(p[1] for p in bucket) / count
        avg_b = sum(p[2] for p in bucket) / count
        avg_s = sum(p[3] for p in bucket) / count
        
        score = count * (avg_s ** 2.5)
        
        if score > best_score:
            best_score = score
            best_rgb = (int(avg_r), int(avg_g), int(avg_b), avg_s)
            
    return (best_rgb[0], best_rgb[1], best_rgb[2])

def build_liquid_qss(r=26, g=36, b=58, glass_pct=50, theme="classic", ambient_opacity=0.3, accent="#0a84ff", accent_end="#64d2ff", glass_clarity=45, glass_rim=20):
    def hex_to_rgb(h):
        h = h.lstrip('#')
        return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
    try:
        ar, ag, ab = hex_to_rgb(accent)
        brightness = (ar * 299 + ag * 587 + ab * 114) / 1000
        btn_text = "#000000" if brightness > 160 else "#ffffff"
    except:
        btn_text = "#ffffff"
    
    if theme in ("liquid_glass", "macos27_glass"):
        is_m27 = theme == "macos27_glass"
        c_f = max(0.0, min(1.0, glass_clarity / 100.0))
        r_f = max(0.0, min(1.0, glass_rim / 100.0))

        # The native NSGlassEffectView / DWM acrylic is the actual glass.
        # QSS only adds a thin veil and rim so the backdrop stays visible
        # instead of being buried under an opaque dark gradient.
        if is_m27:
            # Dark native Clear material with a real clear -> frosted range.
            # Glass Opacity (c_f) drives only the QSS veil; the native view
            # stays at full alpha so AppKit keeps rendering the material.
            text_color = "#f5f5f7"
            secondary_text = "rgba(235, 235, 240, 190)"
            # Keep the veil thin: the native Clear material supplies the blur.
            card_a = int(12 + 220 * c_f)
            rim_a = int(10 + 46 * r_f)
            btn_a = int(18 + 170 * r_f)
            card_bg = f"rgba(26, 26, 28, {card_a})"
            card_hi = f"rgba(255, 255, 255, {rim_a})"
            card_lo = card_hi
            list_bg = f"rgba(20, 20, 22, {int(10 + 205 * c_f)})"
            input_bg = f"rgba(255, 255, 255, {int(18 + 160 * r_f)})"
            btn_bg = f"rgba(255, 255, 255, {btn_a})"
            slider_bg = "rgba(255, 255, 255, 46)"
            slider_handle = "#ffffff"
            tint_a = int(8 * ambient_opacity)
            base_a = 0
            mid_rgb, low_rgb = "22, 22, 24", "16, 16, 18"
            set_mid, set_low = "24, 24, 26", "18, 18, 20"
        else:
            # Restore the original readable dark Glass material.
            text_color = "#ffffff"
            secondary_text = "rgba(255, 255, 255, 190)"
            card_a = int(28 + 110 * c_f)
            rim_a = int(24 + 76 * r_f)
            btn_a = int(16 + 24 * r_f)
            card_bg = f"rgba(24, 26, 34, {card_a})"
            card_hi = f"rgba(255, 255, 255, {rim_a})"
            card_lo = card_hi
            list_bg = f"rgba(14, 16, 22, {int(18 + 90 * c_f)})"
            input_bg = f"rgba(255, 255, 255, {int(14 + 20 * r_f)})"
            btn_bg = f"rgba(255, 255, 255, {btn_a})"
            slider_bg = "rgba(255, 255, 255, 50)"
            slider_handle = "#ffffff"
            tint_a = int(8 + 75 * ambient_opacity)
            base_a = int(20 + 165 * c_f)
            mid_rgb, low_rgb = "14, 16, 24", "8, 10, 16"
            set_mid, set_low = "16, 18, 26", "10, 12, 18"

        if is_m27:
            # Do not paint a square gradient layer over the rounded native
            # glass view; that square edge is what caused the cut-off corner.
            bg_gradient = "transparent"
            settings_bg = "transparent"
        else:
            bg_gradient = f"qradialgradient(cx:0.28, cy:0.18, radius:1.4, stop:0 rgba({r},{g},{b},{tint_a}), stop:0.55 rgba({mid_rgb},{base_a}), stop:1 rgba({low_rgb},{base_a}))"
            settings_bg = f"qlineargradient(x1:1, y1:0.9, x2:0, y2:0, stop:0 rgba({r},{g},{b},{int(tint_a*0.85)}), stop:0.45 rgba({set_mid},{base_a}), stop:1 rgba({set_low},{base_a}))"
    
    elif theme == "light":
        import colorsys
        lh, _, ls = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
        pr, pg_, pb = colorsys.hls_to_rgb(lh, 0.72, min(0.75, max(0.35, ls * 0.95)))
        sr, sg, sb = int(pr * 255), int(pg_ * 255), int(pb * 255)
        base_r, base_g, base_b = 238, 241, 246
        text_color = "#161618"
        secondary_text = "#52525b"
        card_bg = "rgba(255, 255, 255, 200)"
        card_hi = "rgba(0, 0, 0, 22)"
        card_lo = "rgba(0, 0, 0, 22)"
        list_bg = "rgba(245, 247, 251, 200)"
        input_bg = "rgba(255, 255, 255, 242)"
        btn_bg = "rgba(255, 255, 255, 248)"
        slider_bg = "rgba(0, 0, 0, 30)"
        slider_handle = accent
        cr = int(base_r + (sr - base_r) * ambient_opacity)
        cg = int(base_g + (sg - base_g) * ambient_opacity)
        cb = int(base_b + (sb - base_b) * ambient_opacity)
        bg_gradient = f"qradialgradient(cx:0.28, cy:0.18, radius:1.25, stop:0 rgba({cr},{cg},{cb},255), stop:0.65 rgba(234,237,243,255), stop:1 rgba(242,244,248,255))"
        settings_bg = f"qlineargradient(x1:1, y1:0.9, x2:0, y2:0, stop:0 rgba({cr},{cg},{cb},255), stop:0.45 rgba(242,244,248,255), stop:1 rgba(235,238,244,255))"

    else:  # classic dark
        base_r, base_g, base_b = 14, 17, 24
        text_color = "#f2f4f8"
        secondary_text = "#9ca3af"
        card_bg = "rgba(255, 255, 255, 16)"
        card_hi = "rgba(255, 255, 255, 28)"
        card_lo = "rgba(255, 255, 255, 28)"
        list_bg = "rgba(0, 0, 0, 55)"
        input_bg = "rgba(255, 255, 255, 20)"
        btn_bg = "rgba(255, 255, 255, 22)"
        slider_bg = "rgba(255, 255, 255, 34)"
        slider_handle = "#ffffff"
        cr = int(base_r + (r - base_r) * ambient_opacity)
        cg = int(base_g + (g - base_g) * ambient_opacity)
        cb = int(base_b + (b - base_b) * ambient_opacity)
        bg_gradient = f"qradialgradient(cx:0.28, cy:0.18, radius:1.25, stop:0 rgba({cr},{cg},{cb},255), stop:0.65 rgba(13,16,23,255), stop:1 rgba(8,10,15,255))"
        settings_bg = f"qlineargradient(x1:1, y1:0.9, x2:0, y2:0, stop:0 rgba({cr},{cg},{cb},255), stop:0.42 rgba(17,20,28,255), stop:1 rgba(11,13,19,255))"

    return f"""
    QMainWindow, QWidget#centralWidget {{ 
        background: {bg_gradient}; 
    }}
    QDialog, QFrame#settingsRoot {{ 
        background: {settings_bg}; 
    }}
    QFrame#glassCard {{ background: {card_bg}; border: 1px solid {card_hi}; border-radius: 16px; }}
    QLabel, QCheckBox {{ color: {text_color}; font-size: 13px; font-family: ".AppleSystemUIFont", "Helvetica Neue", sans-serif; background: transparent; }}
    QLabel#trackTitle {{ color: {text_color}; font-size: 18px; font-weight: bold; }}
    QLabel#trackArtist {{ color: {secondary_text}; font-size: 14px; }}
    QListWidget {{ background: {list_bg}; color: {text_color}; border: 1px solid {card_lo}; border-radius: 12px; padding: 4px; outline: none; }}
    QListWidget::item {{ padding: 8px; border-radius: 8px; }}
    QListWidget::item:selected {{ background-color: {accent}; color: {btn_text}; }}
    QLineEdit, QComboBox, QSpinBox {{ background-color: {input_bg}; color: {text_color}; border: 1px solid {card_lo}; border-radius: 9px; padding: 5px 10px; }}
    QComboBox {{ combobox-popup: 0; }}
    QComboBox QAbstractItemView {{
        background-color: {'#ffffff' if theme=='light' else ('rgba(26, 26, 28, 236)' if theme=='macos27_glass' else ('rgba(22, 25, 34, 236)' if theme=='liquid_glass' else '#1d212b'))}; color: {text_color}; border: 1px solid {card_hi}; border-radius: 10px; padding: 4px; outline: none;
    }}
    QComboBox QAbstractItemView::item {{ padding: 3px 8px; border-radius: 5px; margin: 1px 2px; min-height: 14px; font-size: 12px; }}
    QComboBox QAbstractItemView::item:selected {{ background-color: {accent}; color: {btn_text}; }}
    QComboBox::drop-down {{ border: none; width: 20px; }}
    QPushButton {{ background: {btn_bg}; color: {text_color}; border: 1px solid {card_hi}; border-radius: 10px; padding: 8px 14px; font-weight: 500; }}
    QPushButton:hover {{ background-color: {'rgba(235,239,246,255)' if theme=='light' else 'rgba(255,255,255,38)'}; }}
    QPushButton:focus {{ border: 1px solid {accent}; }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border: 1px solid {accent}; }}
    QPushButton:checked {{ background-color: {accent}; border-top: 1px solid rgba(255,255,255,100); color: {btn_text}; }}
    QPushButton:disabled {{ background-color: rgba(0,0,0,10); color: #999; border: none; }}
    QPushButton#playBtn {{ font-size: 16px; padding: 10px 20px; border-radius: 12px; }}
    QSlider::groove:horizontal {{ height: 5px; background: {slider_bg}; border-radius: 2px; }}
    QSlider::sub-page:horizontal {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {accent}, stop:1 {accent_end}); border-radius: 2px; }}
    QSlider::handle:horizontal {{ background: {slider_handle}; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; }}
    QSlider::sub-page:horizontal:disabled {{ background: {slider_bg}; }}
    QSlider::handle:horizontal:disabled {{ background: rgba(150, 150, 155, 130); }}
    QLabel:disabled {{ color: rgba(142, 142, 147, 140); }}
    """

class WindowDragFilter(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.win = window
        self._press_pos = None
        self._click_count = 0
        self._last_click_t = 0.0
        self._last_click_pos = None
        self._normal_geom = None
        self._zoom_timer = QTimer(self)
        self._zoom_timer.setSingleShot(True)
        self._zoom_timer.setInterval(240)
        self._zoom_timer.timeout.connect(self._toggle_zoom)

    def _is_interactive(self, widget):
        w = widget
        while w is not None and w is not self.win:
            if isinstance(w, (QAbstractButton, QAbstractSlider, QComboBox, QLineEdit,
                              QAbstractSpinBox, QAbstractItemView, QScrollBar)):
                return True
            if w.objectName() == "PlotWidget" or w.__class__.__name__ in ("PlotWidget", "GraphicsView"):
                return True
            w = w.parentWidget()
        return False

    def _animate_to(self, target_rect, shrinking=False):
        self.win._is_resizing = True
        def _finish():
            self.win._is_resizing = False
            self.win._last_cover_render_key = None
            self.win.update_cover_art_size()
        QTimer.singleShot(420, _finish)
        if sys.platform == "darwin":
            try:
                objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
                objc.sel_registerName.restype = ctypes.c_void_p
                objc.sel_registerName.argtypes = [ctypes.c_char_p]
                class NSRect(ctypes.Structure):
                    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double), ("w", ctypes.c_double), ("h", ctypes.c_double)]
                get_w = ctypes.cast(objc.objc_msgSend, ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p))
                ns_win = get_w(ctypes.c_void_p(int(self.win.winId())), objc.sel_registerName(b"window"))
                scr_h = self.win.screen().geometry().height()
                rect = NSRect(float(target_rect.x()), float(scr_h - (target_rect.y() + target_rect.height())), float(target_rect.width()), float(target_rect.height()))
                set_f = ctypes.cast(objc.objc_msgSend, ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, NSRect, ctypes.c_bool, ctypes.c_bool))
                set_f(ns_win, objc.sel_registerName(b"setFrame:display:animate:"), rect, True, True)
                return
            except Exception:
                pass
        self.win.setGeometry(target_rect)

    def _toggle_zoom(self):
        self._click_count = 0
        if self.win.isFullScreen():
            self.win.showNormal()
            return
        avail = self.win.screen().availableGeometry()
        default_small = QRect(avail.x() + (avail.width() - 1100) // 2, avail.y() + (avail.height() - 820) // 2, 1100, 820)
        fg = self.win.frameGeometry()
        if fg.width() >= avail.width() - 25 and fg.height() >= avail.height() - 45:
            target = self._normal_geom or default_small
            self._normal_geom = None
            self._was_zoomed = False
            self._animate_to(target, shrinking=True)
        else:
            if fg.width() < avail.width() - 100:
                self._normal_geom = QRect(fg)
            self._was_zoomed = True
            self._animate_to(avail, shrinking=False)

    def eventFilter(self, obj, event):
        if obj is not self.win:
            return False
        t = event.type()
        if t in (QEvent.MouseButtonPress, QEvent.MouseButtonDblClick) and event.button() == Qt.LeftButton:
            gpos = event.globalPosition().toPoint()
            if self._is_interactive(self.win.childAt(self.win.mapFromGlobal(gpos))):
                self._press_pos = None; self._click_count = 0; self._zoom_timer.stop()
                return False
            now = time.monotonic()
            if self._last_click_pos and (gpos - self._last_click_pos).manhattanLength() < 8 and (now - self._last_click_t) < 0.45:
                self._click_count += 1
            else:
                self._click_count = 1
            self._last_click_t = now; self._last_click_pos = gpos
            if self._click_count == 1:
                self._press_pos = gpos
            elif self._click_count == 2:
                self._press_pos = None; self._zoom_timer.start()
                return True
            elif self._click_count >= 3:
                self._zoom_timer.stop(); self._press_pos = None; self._click_count = 0
                self.win.showNormal() if self.win.isFullScreen() else self.win.showFullScreen()
                return True
        elif t == QEvent.MouseMove and self._press_pos is not None:
            if (event.globalPosition().toPoint() - self._press_pos).manhattanLength() >= 4:
                self._zoom_timer.stop(); self._press_pos = None; self._click_count = 0
                wh = self.win.windowHandle()
                if wh is not None:
                    wh.startSystemMove()
                    return True
        elif t == QEvent.MouseButtonRelease:
            self._press_pos = None
        return False

def sample_ambient_palette(pixmap):
    r, g, b = sample_ambient_rgb(pixmap)
    import colorsys
    h, l, s = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
    s = max(0.38, min(0.85, s * 1.25))
    l = max(0.32, min(0.58, l))
    c1 = tuple(int(round(v * 255)) for v in colorsys.hls_to_rgb(h, l, s))
    c2 = tuple(int(round(v * 255)) for v in colorsys.hls_to_rgb((h + 0.08) % 1.0, min(0.65, l + 0.07), s))
    c3 = tuple(int(round(v * 255)) for v in colorsys.hls_to_rgb((h - 0.09) % 1.0, max(0.28, l - 0.05), min(0.9, s * 1.1)))
    return [c1, c2, c3]


from PySide6.QtWidgets import QWidget
from PySide6.QtCore import QTimer

class LavaLampOverlay(QWidget):
    def __init__(self, mw, parent=None):
        super().__init__(parent)
        self.mw = mw
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self._phase = 0.0
        self._palette = [(26, 86, 180), (96, 52, 175), (18, 128, 155)]
        self._timer = QTimer(self)
        self._timer.setInterval(50)  # Smooth 20 FPS slow drift
        self._timer.timeout.connect(self._tick)

    def set_palette(self, colors):
        if colors and len(colors) >= 3:
            self._palette = list(colors[:3])
            self.update()

    def sync_active(self):
        theme = getattr(self.mw, "current_theme", "classic")
        glow = float(getattr(self.mw, "ambient_opacity", 0.3))
        dyn = bool(getattr(self.mw, "dynamic_ambient", True))
        low_cpu = bool(getattr(getattr(self.mw, "engine", None), "low_end_mode", False))
        show = dyn and theme in ("classic", "light", "liquid_glass", "macos27_glass") and glow > 0.01
        # macOS 27 should feel like a native static material, not a lava lamp.
        active = show and not low_cpu and theme != "macos27_glass"
        self.setVisible(show)
        if active and not self._timer.isActive():
            self._timer.start()
        elif not active and self._timer.isActive():
            self._timer.stop()
        self.update()

    def _tick(self):
        if not self.isVisible() or self.window().isMinimized():
            return
        self._phase += 0.012
        self.update()

    def paintEvent(self, event):
        from PySide6.QtGui import QPainter, QImage, QPixmap
        from PySide6.QtCore import Qt
        import math, numpy as np
        theme = getattr(self.mw, "current_theme", "classic")
        if theme not in ("classic", "light", "liquid_glass", "macos27_glass"):
            return
        glow = max(0.0, min(1.0, float(getattr(self.mw, "ambient_opacity", 0.3))))
        if glow <= 0.01 or self.width() < 16 or self.height() < 16:
            return

        gw, gh = 320, 200
        if not hasattr(self, "_grid_xy"):
            xs = np.linspace(0.0, 1.0, gw, dtype=np.float32)[None, :]
            ys = np.linspace(0.0, 1.0, gh, dtype=np.float32)[:, None]
            # Large 512x512 non-repeating 1-LSB micro-grain tile (zero wallpaper pattern)
            rng = np.random.default_rng(1337)
            tile_arr = np.zeros((512, 512, 4), dtype=np.uint8)
            n = rng.integers(-1, 2, size=(512, 512), dtype=np.int16)
            tile_arr[:, :, :3] = np.where(n[:, :, None] > 0, 2, 0)
            tile_arr[:, :, 3] = np.where(n != 0, 2, 0)
            t_img = QImage(tile_arr.data, 512, 512, 512 * 4, QImage.Format_RGBA8888_Premultiplied).copy()
            self._grid_xy = (xs, ys, QPixmap.fromImage(t_img))
        xs, ys, dither_tile = self._grid_xy

        is_light = theme == "light"
        peak_a = (0.015 if theme == "macos27_glass" else (0.12 if theme == "liquid_glass" else (0.36 if is_light else 0.44))) * glow
        t = self._phase
        p0, p1, p2 = self._palette[0], self._palette[1], self._palette[2]
        orbs = (
            (0.28 + 0.22 * math.sin(t * 0.7), 0.30 + 0.20 * math.cos(t * 0.5), 0.24, p0),
            (0.72 + 0.20 * math.cos(t * 0.6), 0.65 + 0.22 * math.sin(t * 0.8), 0.28, p1),
            (0.50 + 0.25 * math.sin(t * 0.4 + 1.4), 0.48 + 0.24 * math.cos(t * 0.65 + 0.8), 0.22, p2),
        )

        # True premultiplied alpha blending so overlapping orbs never create moire/floral shapes
        acc_rgb = np.zeros((gh, gw, 3), dtype=np.float32)
        rem_a = np.ones((gh, gw), dtype=np.float32)
        for ox, oy, inv_var, (cr, cg, cb) in orbs:
            dx = xs - np.float32(ox)
            dy = (ys - np.float32(oy)) * np.float32(0.75)
            w_orb = np.exp(-(dx * dx + dy * dy) / np.float32(inv_var)) * np.float32(peak_a)
            contrib = w_orb * rem_a
            acc_rgb[:, :, 0] += contrib * np.float32(cr)
            acc_rgb[:, :, 1] += contrib * np.float32(cg)
            acc_rgb[:, :, 2] += contrib * np.float32(cb)
            rem_a *= (np.float32(1.0) - w_orb)

        total_a = np.clip(np.float32(1.0) - rem_a, 0.0, 1.0)
        rgba = np.empty((gh, gw, 4), dtype=np.uint8)
        rgba[:, :, :3] = np.clip(acc_rgb, 0.0, 255.0).astype(np.uint8)
        rgba[:, :, 3] = np.clip(total_a * np.float32(255.0), 0.0, 255.0).astype(np.uint8)

        img = QImage(rgba.data, gw, gh, gw * 4, QImage.Format_RGBA8888_Premultiplied)
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.drawImage(self.rect(), img)
        # Native macOS 27 glass is clean; micro-grain belongs to the other
        # themes where it breaks up large gradients without muddying blur.
        if theme != "macos27_glass":
            p.drawTiledPixmap(self.rect(), dither_tile)
        p.end()