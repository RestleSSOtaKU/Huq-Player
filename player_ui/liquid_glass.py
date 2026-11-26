import os, sys, time, ctypes
from PySide6.QtWidgets import (QApplication, QFrame, QAbstractButton, QAbstractSlider,
                               QComboBox, QLineEdit, QAbstractSpinBox,
                               QAbstractItemView, QScrollBar)
from PySide6.QtCore import QObject, QEvent, Qt, QTimer, QRect, QPoint
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


def glass_opacity_state(opacity):
    """Split legacy 0..1 clarity into a bucket for fallback/auto sync.

    ``macos27_glass`` no longer maps this to Regular glass: every mode uses
    the public Clear style and differs by tint/scrim density. This helper
    remains for old numeric sessions and Automatic system sync.
    """
    o = max(0.0, min(1.0, float(opacity if opacity is not None else 0.0)))
    if o < 0.5:
        return "clear", o / 0.5
    return "frosted", (o - 0.5) / 0.5


GLASS_MATERIALS = ("automatic", "default", "dark", "clear", "tinted")
GLASS_MATERIAL_LABELS = {
    "automatic": "Automatic",
    "default": "Default",
    "dark": "Dark",
    "clear": "Clear",
    "tinted": "Tinted",
}

# Two quick stages map to the existing Dark/Clear presets. The appearance
# field is a native AppKit appearance, not an exact macOS material-mode read.
GLASS_STAGES = ("dark", "light")
GLASS_STAGE_LABELS = {"dark": "Dark", "light": "Light"}
GLASS_STAGE_MATERIAL = {"dark": "dark", "light": "clear"}

# Dark's native tint is intentionally capped at its stock level; brighter
# tints washed the panel out over picture backgrounds. Light keeps full range.
GLASS_STAGE_TINT_MAX = {"dark": 0.52, "light": 1.0}

# Stock Glass Border Rim: shared by QSS generation, the Settings reset slider,
# and fresh/missing session state.
DEFAULT_GLASS_RIM = 10

# Backdrop downsample is not user-tunable: keep the sharpest/full-scale value
# in normal mode and ease to ~1/3 only for Low-End CPU mode.
GLASS_BACKDROP_SCALE_FULL = 0.50
GLASS_BACKDROP_SCALE_LOW_END = 0.33

# Per-card native glass. The QSS scrim stays fairly light so this material is
# actually visible; the window-level material remains behind it.
GLASS_PANEL_TINT_ALPHA = {"dark": 0.16, "light": 0.06}
_PANEL_GLASS_WINDOW_BELOW = -1
_PANEL_GLASS_RESIZE_MASK = 2 | 16  # NSViewWidthSizable | NSViewHeightSizable

_GLASS_OVERRIDE_RANGES = {
    "tint_alpha": (0.0, 1.0),
    "card_alpha": (0, 255),
    "list_alpha": (0, 255),
}


def normalize_glass_overrides(value, stage=None):
    """Clamp user tuning values to safe ranges; ignore unknown keys.

    ``stage`` also enforces the per-stage Native Tint ceiling.
    """
    out = {}
    if not isinstance(value, dict):
        return out
    for key, (lo, hi) in _GLASS_OVERRIDE_RANGES.items():
        if key not in value:
            continue
        try:
            number = float(value[key])
        except (TypeError, ValueError):
            continue
        number = max(lo, min(hi, number))
        if key == "tint_alpha" and stage in GLASS_STAGE_TINT_MAX:
            number = min(number, GLASS_STAGE_TINT_MAX[stage])
        out[key] = int(round(number)) if key in ("card_alpha", "list_alpha") else number
    style = str(value.get("style", "")).strip().lower()
    if style in ("clear", "regular"):
        out["style"] = style
    return out


# Native references are dark panels over a picture background. AppKit exposes
# only Clear/Regular styles, and Regular reads as a smeared blur here, so macOS
# 27 presets default to Clear and vary native tint + QSS scrim instead.
_GLASS_MATERIAL_PROFILES = {
    "automatic": {"opacity": 0.58, "style": "clear", "tint_alpha": 0.32,
                  "tint_rgb": (0.05, 0.055, 0.065), "card_alpha": 174,
                  "list_alpha": 166, "backdrop_scale": 0.345},
    "default": {"opacity": 0.62, "style": "clear", "tint_alpha": 0.34,
                "tint_rgb": (0.05, 0.055, 0.065), "card_alpha": 174,
                "list_alpha": 166, "backdrop_scale": 0.35},
    # Dark stage panels lean deliberately dark: the native Clear material
    # still supplies the blur, while a denser tint + QSS scrim gives text a
    # steadier background over bright wallpapers. This is not a blur-radius
    # control; it is a best-effort visual preset.
    "dark": {"opacity": 0.82, "style": "clear", "tint_alpha": 0.52,
             "tint_rgb": (0.028, 0.031, 0.038), "card_alpha": 176,
             "list_alpha": 168, "backdrop_scale": 0.33},
    "clear": {"opacity": 0.48, "style": "clear", "tint_alpha": 0.27,
              "tint_rgb": (0.05, 0.055, 0.065), "card_alpha": 162,
              "list_alpha": 154, "backdrop_scale": 0.36},
    "tinted": {"opacity": 0.58, "style": "clear", "tint_alpha": 0.36,
               "tint_rgb": (0.07, 0.065, 0.09), "card_alpha": 184,
               "list_alpha": 176, "backdrop_scale": 0.34},
}


def canonical_glass_material(material):
    key = str(material or "").strip().lower()
    return key if key in GLASS_MATERIALS else "automatic"


def glass_material_profile(material, opacity=None, overrides=None, stage=None):
    """Return the native-glass preset for a discrete material mode.

    Every Glass 2.0 mode uses the public Clear style by default; Regular is an
    advanced opt-in override. ``automatic`` follows the last system-synced
    clarity. User overrides are clamped experimental values, not a public
    numeric blur control. ``stage`` enforces the Dark/Light Native Tint cap.
    """
    key = canonical_glass_material(material)
    if key == "automatic":
        o = max(0.0, min(1.0, float(opacity if opacity is not None else 0.58)))
        profile = {
            "material": key, "opacity": o, "style": "clear",
            "tint_alpha": 0.24 + 0.26 * o,
            "tint_rgb": (0.05, 0.055, 0.065),
            "card_alpha": int(round(140 + 50 * o)),
            "list_alpha": int(round(132 + 50 * o)),
            "backdrop_scale": 0.36 - 0.03 * o,
        }
    else:
        profile = dict(_GLASS_MATERIAL_PROFILES[key])
        profile["material"] = key
    profile.update(normalize_glass_overrides(overrides, stage))
    return profile


def glass_material_clarity(material, fallback=45):
    """Return the persisted numeric clarity for a discrete mode."""
    key = canonical_glass_material(material)
    if key == "automatic":
        try:
            return int(max(0, min(100, int(fallback))))
        except (TypeError, ValueError):
            return 45
    return int(round(_GLASS_MATERIAL_PROFILES[key]["opacity"] * 100))


def material_from_opacity(opacity):
    """Infer a discrete mode from a legacy/system numeric opacity."""
    o = max(0.0, min(1.0, float(opacity if opacity is not None else 0.45)))
    state, _ = glass_opacity_state(o)
    if state == "clear":
        return "clear"
    return "dark" if o >= 0.8 else "default"


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
    """Best-effort mapping from OS appearance settings to a Glass Stage.

    macOS does not expose exact native material modes publicly, so we combine
    public signals: effective appearance, Reduce Transparency, and wallpaper
    luminance. Windows uses registry values. The result is only Dark/Light.
    """
    info = {
        "available": False,
        "platform": sys.platform,
        "appearance": "unknown",
        "reduce_transparency": None,
        "wallpaper_luminance": None,
        "suggested_opacity": None,
        "suggested_material": None,
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
                material = "dark"
            else:
                base = 50 if dark else 38
                spread = 28 if dark else 32
                opacity = int(round(base + spread * (0.5 if luma is None else luma)))
                opacity = max(12, min(94, opacity))
                if auto:
                    material = "automatic"
                elif dark:
                    material = "dark"
                elif luma is not None and luma >= 0.55:
                    material = "clear"
                elif luma is not None and luma <= 0.25:
                    material = "tinted"
                else:
                    material = "default"

            appearance_label = ("Dark" if dark else "Light") + (" (Auto)" if auto else "")
            parts = [
                f"macOS {appearance_label}",
                f"Reduce Transparency {'On' if reduce else 'Off'}",
            ]
            if luma is not None:
                parts.append(f"wallpaper {int(luma * 100)}%")
            parts.append(
                f"→ Glass Stage {GLASS_STAGE_LABELS['dark' if dark else 'light']} (approx, {opacity}%)"
            )
            info.update({
                "available": True,
                "appearance": "dark" if dark else "light",
                "reduce_transparency": reduce,
                "wallpaper_luminance": luma,
                "suggested_opacity": opacity,
                "suggested_material": material,
                "summary": " · ".join(parts),
            })
        except Exception:
            info["summary"] = "Could not read macOS appearance; kept current Glass Stage."
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
            material = "dark" if (not transparent or dark) else "default"
            parts = [
                f"Windows {'Dark' if dark else 'Light'}",
                f"Transparency {'On' if transparent else 'Off'}",
                f"→ Glass Stage {GLASS_STAGE_LABELS['dark' if dark else 'light']} (approx, {opacity}%)",
            ]
            info.update({
                "available": True,
                "appearance": "dark" if dark else "light",
                "reduce_transparency": not transparent,
                "suggested_opacity": opacity,
                "suggested_material": material,
                "summary": " · ".join(parts),
            })
        except Exception:
            info["summary"] = "Could not read Windows theme; kept current Glass Stage."

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

def _backdrop_layers(layer):
    found = []
    if layer is None:
        return found
    try:
        cls = layer.className()
    except Exception:
        cls = ""
    if "Backdrop" in cls:
        found.append(layer)
    try:
        sublayers = layer.sublayers() or []
    except Exception:
        sublayers = []
    for sub in sublayers:
        found.extend(_backdrop_layers(sub))
    return found


class _MacosGlassScaleFilter(QObject):
    """Reapply the backdrop scale after AppKit rebuilds it on move/resize."""

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self._pending = 0

    def eventFilter(self, obj, event):
        try:
            if event.type() in (QEvent.Move, QEvent.Resize):
                scale = getattr(obj, "_glass_backdrop_scale", None)
                if scale is not None and self._pending == 0:
                    # Coalesce a burst of Move/Resize events into one pair of
                    # callbacks; repeated theme changes used to queue dozens.
                    self._pending = 2
                    QTimer.singleShot(0, self._flush_scale)
                    QTimer.singleShot(120, self._flush_scale)
        except Exception:
            pass
        return False

    def _flush_scale(self):
        if self._pending <= 0:
            return
        self._pending -= 1
        try:
            scale = getattr(self.owner, "_glass_backdrop_scale", None)
            if scale is not None:
                _apply_macos_glass_backdrop_scale(self.owner, scale)
        except Exception:
            pass


def _ensure_macos_glass_scale_filter(window):
    if getattr(window, "_glass_scale_filter", None) is None:
        filt = _MacosGlassScaleFilter(window)
        window._glass_scale_filter = filt
        try:
            window.installEventFilter(filt)
        except Exception:
            pass


def clear_macos_glass_state(window):
    """Forget native glass objects/filters after an effect is removed.

    ``pyqt_liquidglass.remove_glass_effect()`` removes the view from its
    superview but leaves ``window._glass_view`` pointing at it. Touching that
    stale AppKit view on later theme switches is both needless and a plausible
    source of intermittent crashes, so drop every cached reference here.
    """
    window._glass_view = None
    window._glass_backdrop_scale = None
    window._glass_scale_token = int(getattr(window, "_glass_scale_token", 0)) + 1
    filt = getattr(window, "_glass_scale_filter", None)
    if filt is not None:
        try:
            window.removeEventFilter(filt)
        except Exception:
            pass
        try:
            filt.deleteLater()
        except Exception:
            pass
        window._glass_scale_filter = None


def _apply_macos_glass_backdrop_scale(window, scale):
    glass_view = getattr(window, "_glass_view", None)
    if glass_view is None:
        return False
    try:
        applied = False
        for layer in _backdrop_layers(glass_view.layer()):
            try:
                layer.setValue_forKey_(float(scale), "scale")
                applied = True
            except Exception:
                pass
        return applied
    except Exception:
        return False


def set_macos_glass_backdrop_scale(window, scale):
    """Best-effort bokeh-like diffusion via the native backdrop layer.

    Public Clear captures the backdrop at 0.5 scale and Regular at 0.25.
    An intermediate downsample softens bright text without Regular's smear.
    AppKit rebuilds the backdrop layer after the initial style change and on
    move/resize, resetting ``scale`` to 0.5, so the value is reapplied both on
    a short timer series and from a Qt event filter. This is a private KVC
    detail, not a public blur-radius API; failures fall back to public Clear.
    """
    if sys.platform != "darwin":
        return
    try:
        scale = float(scale)
    except Exception:
        return
    token = int(getattr(window, "_glass_scale_token", 0)) + 1
    window._glass_scale_token = token
    window._glass_backdrop_scale = scale
    _ensure_macos_glass_scale_filter(window)

    def attempt():
        # Ignore stale retries from an earlier theme/material selection.
        if (getattr(window, "_glass_scale_token", None) != token
                or getattr(window, "_glass_backdrop_scale", None) != scale):
            return
        _apply_macos_glass_backdrop_scale(window, scale)

    attempt()
    for delay in (250, 500, 900, 1400):
        QTimer.singleShot(delay, attempt)


class _PanelGlassFilter(QObject):
    """Keep per-card NSGlassEffectViews aligned with translated Qt widgets."""

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner

    def eventFilter(self, obj, event):
        try:
            if event.type() in (QEvent.Move, QEvent.Resize,
                                QEvent.Show, QEvent.Hide):
                schedule_macos_panel_glass_sync(self.owner)
        except Exception:
            pass
        return False


def schedule_macos_panel_glass_sync(window):
    """Coalesce per-card glass syncs from many widgets into one callback."""
    if getattr(window, "_panel_glass_sync_pending", False):
        return
    generation = int(getattr(window, "_panel_glass_generation", 0))
    window._panel_glass_sync_pending = True

    def run():
        if not getattr(window, "_panel_glass_sync_pending", False):
            return
        if int(getattr(window, "_panel_glass_generation", 0)) != generation:
            window._panel_glass_sync_pending = False
            return
        window._panel_glass_sync_pending = False
        _sync_macos_panel_glass(window)

    QTimer.singleShot(0, run)


def remove_macos_panel_glass(window):
    records = list(getattr(window, "_panel_glass_views", []) or [])
    filt = getattr(window, "_panel_glass_filter", None)
    for rec in records:
        try:
            rec["view"].removeFromSuperview()
        except Exception:
            pass
        if filt is not None:
            try:
                rec["widget"].removeEventFilter(filt)
            except Exception:
                pass
    window._panel_glass_views = []
    window._panel_glass_container = None
    window._panel_glass_generation = int(
        getattr(window, "_panel_glass_generation", 0)) + 1
    window._panel_glass_sync_pending = False
    if filt is not None:
        try:
            window.removeEventFilter(filt)
        except Exception:
            pass
        try:
            filt.deleteLater()
        except Exception:
            pass
        window._panel_glass_filter = None


def _sync_macos_panel_glass(window):
    records = getattr(window, "_panel_glass_views", None)
    if not records:
        return
    try:
        from Foundation import NSMakeRect
        from pyqt_liquidglass._bridge import get_nswindow_from_widget

        ns_window = get_nswindow_from_widget(window)
        container = getattr(window, "_panel_glass_container", None)
        if ns_window is None or container is None:
            return
        bounds = container.bounds()
        container_height = float(bounds.size.height)
        origin_x = float(bounds.origin.x)
        origin_y = float(bounds.origin.y)
    except Exception:
        return

    # One destroyed/dialog-owned widget must not abort sync for every card.
    for rec in list(records):
        widget = rec["widget"]
        view = rec["view"]
        try:
            if widget.window() is not window:
                view.setHidden_(True)
                continue
            visible = bool(widget.isVisible() and not widget.isHidden())
            view.setHidden_(not visible)
            if not visible:
                continue
            pos = widget.mapTo(window, QPoint(0, 0))
            view.setFrame_(NSMakeRect(
                origin_x + float(pos.x()),
                origin_y + container_height - float(pos.y()) - float(widget.height()),
                float(widget.width()),
                float(widget.height()),
            ))
        except Exception:
            try:
                view.setHidden_(True)
            except Exception:
                pass


def configure_macos_panel_glass(window, theme, enable=True, profile=None,
                                stage="dark"):
    """Give each glass card its own native glass layer.

    QSS cannot blur the wallpaper behind a panel; it can only tint it. These
    NSGlassEffectViews sit behind the Qt content and above the window-wide
    material, so darker cards read as real glass instead of flat scrim.
    """
    if sys.platform != "darwin":
        return
    if not enable or theme not in ("liquid_glass", "macos27_glass"):
        remove_macos_panel_glass(window)
        return
    try:
        import objc
        from AppKit import NSColor
        from Foundation import NSMakeRect
        from pyqt_liquidglass._bridge import (get_nswindow_from_widget,
                                              get_nsview_from_widget)

        try:
            from AppKit import (NSGlassEffectViewStyleClear,
                                NSGlassEffectViewStyleRegular)
        except ImportError:
            NSGlassEffectViewStyleClear, NSGlassEffectViewStyleRegular = 1, 0

        ns_window = get_nswindow_from_widget(window)
        root_view = get_nsview_from_widget(window)
        if ns_window is None or root_view is None:
            return
        content_view = ns_window.contentView()
        # Match pyqt_liquidglass's sibling-injection strategy: the glass view
        # must be inserted into root_view's superview, not the window content
        # view when a custom container was swapped in. Otherwise AppKit treats
        # root_view as an invalid sibling and stacks the glass above Qt.
        superview = root_view.superview()
        container = superview if superview is not None else content_view
        if getattr(window, "_panel_glass_container", None) != container:
            remove_macos_panel_glass(window)
            window._panel_glass_container = container
        glass_cls = objc.lookUpClass("NSGlassEffectView")

        records = getattr(window, "_panel_glass_views", None)
        if records is None:
            records = []
            window._panel_glass_views = records

        filt = getattr(window, "_panel_glass_filter", None)
        if filt is None:
            filt = _PanelGlassFilter(window)
            window._panel_glass_filter = filt
            try:
                window.installEventFilter(filt)
            except Exception:
                pass

        # Dialogs are parented to MainWindow; their cards get their own native
        # views when the dialog is configured and must not be duplicated here.
        cards = [w for w in window.findChildren(QFrame, "glassCard")
                 if w.window() is window]
        wanted = {id(widget) for widget in cards}
        for rec in list(records):
            if id(rec["widget"]) not in wanted:
                try:
                    rec["view"].removeFromSuperview()
                except Exception:
                    pass
                try:
                    rec["widget"].removeEventFilter(filt)
                except Exception:
                    pass
                records.remove(rec)
        known = {id(rec["widget"]): rec for rec in records}

        use_clear = bool(theme == "macos27_glass"
                         and (not profile or profile.get("style", "clear") == "clear"))
        tint_alpha = GLASS_PANEL_TINT_ALPHA.get(stage, 0.12)
        tint_rgb = (1.0, 1.0, 1.0) if stage == "light" else (0.03, 0.033, 0.04)

        for widget in cards:
            rec = known.get(id(widget))
            if rec is None:
                view = glass_cls.alloc().initWithFrame_(NSMakeRect(0, 0, 1, 1))
                view.setAutoresizingMask_(_PANEL_GLASS_RESIZE_MASK)
                if superview is not None:
                    container.addSubview_positioned_relativeTo_(
                        view, _PANEL_GLASS_WINDOW_BELOW, root_view
                    )
                else:
                    container.addSubview_positioned_relativeTo_(
                        view, _PANEL_GLASS_WINDOW_BELOW, None
                    )
                rec = {"widget": widget, "view": view}
                records.append(rec)
            # Qt ignores duplicate installs; this also re-attaches cards whose
            # filter was dropped by remove_macos_panel_glass().
            try:
                widget.installEventFilter(filt)
            except Exception:
                pass
            view = rec["view"]
            view.setStyle_(NSGlassEffectViewStyleClear if use_clear
                           else NSGlassEffectViewStyleRegular)
            view.setCornerRadius_(16.0)
            view.setTintColor_(NSColor.colorWithCalibratedRed_green_blue_alpha_(
                tint_rgb[0], tint_rgb[1], tint_rgb[2], tint_alpha
            ))
            view.setAlphaValue_(1.0)
        _sync_macos_panel_glass(window)
    except Exception:
        pass


def configure_macos_glass(window, theme="macos27_glass", enable=True, opacity=None,
                          material=None):
    """Match the native glass material to the selected theme and mode.

    ``macos27_glass`` uses DarkAqua by default and the public Clear style for
    every preset; ``Glass Stage`` (Dark/Light only) switches DarkAqua/Aqua and
    controls native tint + QSS scrim density. The Regular style reads as a
    smeared blur over picture backgrounds, so it stays opt-in. Glass 2.0 keeps
    the native backdrop downsample at its full/sharpest value in normal mode
    and drops it to ~1/3 in Low-End CPU mode; it is best-effort private KVC,
    reapplied after AppKit rebuilds the layer and on move/resize, and falls
    back silently. ``opacity`` is only a fallback for old numeric sessions
    and Automatic's last system sync. The native view stays at full alpha so
    AppKit keeps rendering the material.
    """
    if sys.platform != "darwin":
        return
    try:
        from AppKit import (NSAppearance, NSAppearanceNameAqua,
                            NSAppearanceNameDarkAqua,
                            NSAppearanceNameVibrantDark, NSColor)
        try:
            from AppKit import NSGlassEffectViewStyleClear, NSGlassEffectViewStyleRegular
        except ImportError:
            # Older AppKit metadata may not expose the constants.
            NSGlassEffectViewStyleClear, NSGlassEffectViewStyleRegular = 1, 0
        from pyqt_liquidglass._bridge import get_nswindow_from_widget

        is_m27 = theme == "macos27_glass"
        stage = getattr(window, "glass_appearance", "dark") if is_m27 else "dark"
        if stage not in GLASS_STAGES:
            stage = "dark"
        # The UI exposes only Dark/Light stages; material profiles are an
        # internal legacy detail and overrides are stored per stage.
        material_key = (GLASS_STAGE_MATERIAL.get(stage, "dark") if is_m27
                        else canonical_glass_material(material))
        overrides = ((getattr(window, "glass_overrides", {}) or {}).get(stage)
                     if is_m27 else None)
        profile = (glass_material_profile(material_key, opacity, overrides, stage)
                   if is_m27 else None)

        ns_window = get_nswindow_from_widget(window)
        if ns_window is not None:
            if not enable:
                ns_window.setAppearance_(None)
            elif is_m27:
                # DarkAqua is the less-vibrant dark used by macOS Settings;
                # Aqua is the Clear/Light stage. This is a public appearance
                # choice, not detection of an exact macOS glass material mode.
                name = NSAppearanceNameAqua if stage == "light" else NSAppearanceNameDarkAqua
                ns_window.setAppearance_(NSAppearance.appearanceNamed_(name))
            else:
                ns_window.setAppearance_(NSAppearance.appearanceNamed_(NSAppearanceNameVibrantDark))

        # When disabling glass, never touch the stale AppKit view left behind
        # by remove_glass_effect(); clear_macos_glass_state() drops it instead.
        glass_view = getattr(window, "_glass_view", None) if enable else None
        if glass_view is not None and hasattr(glass_view, "setStyle_"):
            # Clear is the native-looking default; Regular is available as an
            # explicit advanced override for users matching other stages.
            use_clear = bool(
                enable and is_m27 and profile is not None
                and profile.get("style", "clear") == "clear"
            )
            glass_view.setStyle_(
                NSGlassEffectViewStyleClear if use_clear
                else NSGlassEffectViewStyleRegular
            )
        if glass_view is not None and hasattr(glass_view, "setCornerRadius_"):
            # Match the rounded window corners so the native material does not
            # leave a square edge / cut-off line under the QSS panels.
            glass_view.setCornerRadius_(14.0 if enable else 0.0)
        if enable:
            # Full quality stays at Clear's native 0.5; Low-End drops to ~1/3
            # for cheaper diffusion. Original Glass keeps its 0.25 look.
            if is_m27:
                owner = getattr(window, "mw", None) or window
                low_end = bool(getattr(getattr(owner, "engine", None),
                                       "low_end_mode", False))
                scale = (GLASS_BACKDROP_SCALE_LOW_END if low_end
                         else GLASS_BACKDROP_SCALE_FULL)
            else:
                scale = 0.25
            set_macos_glass_backdrop_scale(window, scale)
        else:
            # Cancel pending retries and stop the move/resize filter; the view
            # itself has already been removed from its superview.
            clear_macos_glass_state(window)
        if glass_view is not None and hasattr(glass_view, "setTintColor_"):
            if enable and profile is not None:
                # Clear / Light uses a white native tint so the panel reads as
                # a bright surface; Dark keeps the measured dark presets.
                tr, tg, tb = ((1.0, 1.0, 1.0) if stage == "light"
                              else profile["tint_rgb"])
                tint_alpha = profile["tint_alpha"]
            else:
                tr = tg = tb = 0.08
                tint_alpha = 0.0
            glass_view.setTintColor_(
                NSColor.colorWithCalibratedRed_green_blue_alpha_(
                    tr, tg, tb, tint_alpha
                )
            )
        if glass_view is not None and hasattr(glass_view, "setNeedsDisplay_"):
            glass_view.setNeedsDisplay_(True)
        # Do NOT lower NSGlassEffectView.alphaValue: AppKit then stops showing
        # the material and the window looks flat. Keep the native view at full
        # strength; modes are expressed through material + tint + QSS veil.
        if glass_view is not None and hasattr(glass_view, "setAlphaValue_"):
            glass_view.setAlphaValue_(1.0)
        # Each card gets its own native material; QSS alone cannot blur the
        # wallpaper behind a panel.
        configure_macos_panel_glass(window, theme, enable, profile, stage)
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

def build_liquid_qss(r=26, g=36, b=58, glass_pct=50, theme="classic", ambient_opacity=0.3, accent="#0a84ff", accent_end="#64d2ff", glass_clarity=45, glass_rim=DEFAULT_GLASS_RIM, glass_material=None, glass_overrides=None, glass_appearance="dark"):
    stage = glass_appearance if glass_appearance in GLASS_STAGES else "dark"

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
            # Native material/tint comes from configure_macos_glass(); QSS
            # supplies the panel scrim. Dark follows the native dark panels;
            # Clear/Light uses a white scrim with dark text. These are tint
            # densities, not a fake blur-radius control.
            key = GLASS_STAGE_MATERIAL.get(stage, "dark")
            profile = glass_material_profile(
                key, c_f, (glass_overrides or {}).get(stage), stage
            )
            card_a = int(profile["card_alpha"])
            list_a = int(profile["list_alpha"])
            # On Dark, white rim/button highlights read much brighter than
            # Light's black rim. Ease the upper half of the slider so moving
            # past ~50 no longer blows out the borders; Light keeps its curve.
            r_highlight = r_f
            if stage == "dark":
                r_highlight = min(r_f, 0.5) + 0.30 * max(0.0, r_f - 0.5)
            rim_a = int(10 + 46 * r_highlight)
            btn_a = int(18 + 170 * r_highlight)
            if stage == "light":
                text_color = "#1d1d1f"
                secondary_text = "#3c3c43"
                card_bg = f"rgba(255, 255, 255, {card_a})"
                card_hi = f"rgba(0, 0, 0, {max(8, rim_a // 2)})"
                card_lo = card_hi
                list_bg = f"rgba(245, 247, 251, {list_a})"
                input_bg = f"rgba(255, 255, 255, {int(160 + 80 * r_f)})"
                btn_bg = f"rgba(255, 255, 255, {int(120 + 120 * r_f)})"
                slider_bg = "rgba(0, 0, 0, 36)"
                slider_handle = "#1d1d1f"
                mid_rgb, low_rgb = "236, 236, 240", "226, 226, 230"
                set_mid, set_low = "240, 240, 244", "230, 230, 234"
            else:
                text_color = "#f5f5f7"
                secondary_text = "rgba(235, 235, 240, 190)"
                card_bg = f"rgba(10, 11, 13, {card_a})"
                card_hi = f"rgba(255, 255, 255, {rim_a})"
                card_lo = card_hi
                list_bg = f"rgba(8, 9, 11, {list_a})"
                input_bg = f"rgba(255, 255, 255, {int(18 + 160 * r_highlight)})"
                btn_bg = f"rgba(255, 255, 255, {btn_a})"
                slider_bg = "rgba(255, 255, 255, 46)"
                slider_handle = "#ffffff"
                mid_rgb, low_rgb = "22, 22, 24", "16, 16, 18"
                set_mid, set_low = "24, 24, 26", "18, 18, 20"
            tint_a = int(8 * ambient_opacity)
            base_a = 0
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

    light_surface = theme == "light" or (theme == "macos27_glass" and stage == "light")
    disabled_text = "#6e6e73" if light_surface else "rgba(142, 142, 147, 140)"
    disabled_btn_text = "#6e6e73" if light_surface else "#999"
    popup_bg = "#ffffff" if light_surface else (
        "rgba(26, 26, 28, 236)" if theme == "macos27_glass" else (
            "rgba(22, 25, 34, 236)" if theme == "liquid_glass" else "#1d212b"
        )
    )

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
        background-color: {popup_bg}; color: {text_color}; border: 1px solid {card_hi}; border-radius: 10px; padding: 4px; outline: none;
    }}
    QComboBox QAbstractItemView::item {{ padding: 3px 8px; border-radius: 5px; margin: 1px 2px; min-height: 14px; font-size: 12px; }}
    QComboBox QAbstractItemView::item:selected {{ background-color: {accent}; color: {btn_text}; }}
    QComboBox::drop-down {{ border: none; width: 20px; }}
    QPushButton {{ background: {btn_bg}; color: {text_color}; border: 1px solid {card_hi}; border-radius: 10px; padding: 8px 14px; font-weight: 500; }}
    QPushButton:hover {{ background-color: {'rgba(235,239,246,255)' if light_surface else 'rgba(255,255,255,38)'}; }}
    QPushButton:focus {{ border: 1px solid {accent}; }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border: 1px solid {accent}; }}
    QPushButton:checked {{ background-color: {accent}; border-top: 1px solid rgba(255,255,255,100); color: {btn_text}; }}
    QPushButton:disabled {{ background-color: rgba(0,0,0,10); color: {disabled_btn_text}; border: none; }}
    QPushButton#playBtn {{ font-size: 16px; padding: 10px 20px; border-radius: 12px; }}
    QSlider::groove:horizontal {{ height: 5px; background: {slider_bg}; border-radius: 2px; }}
    QSlider::sub-page:horizontal {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {accent}, stop:1 {accent_end}); border-radius: 2px; }}
    QSlider::handle:horizontal {{ background: {slider_handle}; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; }}
    QSlider::sub-page:horizontal:disabled {{ background: {slider_bg}; }}
    QSlider::handle:horizontal:disabled {{ background: rgba(150, 150, 155, 130); }}
    QScrollArea#settingsScroll, QScrollArea#mainScroll {{ background: transparent; border: 0; }}
    QWidget#mainContent {{ background: transparent; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; border: 0; }}
    QScrollBar::handle:vertical {{ background: {slider_bg}; border: 1px solid {card_hi}; border-radius: 5px; min-height: 32px; }}
    QScrollBar::handle:vertical:hover {{ background: {accent}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; background: transparent; border: 0; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
    QLabel:disabled {{ color: {disabled_text}; }}
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
        if shrinking:
            # The maximized layout grows the library's fixed height. Reset it
            # first so Qt/AppKit does not clamp the shrink part way and leave
            # the spectrum card taller than it was at the original size.
            try:
                self.win._reset_library_height_for_shrink()
            except Exception:
                pass
        self.win._is_resizing = True
        def _finish():
            self.win._is_resizing = False
            self.win._sync_main_content_height()
            self.win._last_cover_render_key = None
            self.win.update_cover_art_size()
        QTimer.singleShot(420, _finish)

        def _settle_shrink():
            # AppKit clamps the first shrink because the maximized layout has
            # grown the fixed-height cards. Push the exact geometry through in
            # the same event turn so the stretched intermediate size is never
            # painted.
            if not shrinking:
                return
            for _ in range(8):
                self.win.setGeometry(target_rect)
                QApplication.processEvents()
                if self.win.geometry().size() == target_rect.size():
                    break

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
                _settle_shrink()
                return
            except Exception:
                pass
        self.win.setGeometry(target_rect)
        _settle_shrink()

    def _toggle_zoom(self):
        self._click_count = 0
        if self.win.isFullScreen():
            self.win.showNormal()
            return
        avail = self.win.screen().availableGeometry()
        default_small = QRect(avail.x() + (avail.width() - 1100) // 2, avail.y() + (avail.height() - 820) // 2, 1100, 820)
        fg = self.win.frameGeometry()
        if fg.width() >= avail.width() - 25 and fg.height() >= avail.height() - 45:
            # _normal_geom is a Qt content geometry. AppKit's NSWindow frame
            # matches it under the full-size-content style mask, so passing
            # frameGeometry() here would restore 28pt too tall and get clamped.
            target = self._normal_geom or default_small
            self._normal_geom = None
            self._was_zoomed = False
            self._animate_to(target, shrinking=True)
        else:
            if fg.width() < avail.width() - 100:
                self._normal_geom = QRect(self.win.geometry())
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


# Lava runs at 30 FPS when idle and 15 FPS during playback. Both are smooth
# for a slow drift, and the lower rate keeps Classic/Glass from stealing
# frames from the 60 FPS spectrum timer.
_LAVA_INTERVAL_IDLE_MS = 33
_LAVA_INTERVAL_PLAYING_MS = 66


def _make_lava_sprite(rgb, peak_alpha, size=96):
    """Cache one Gaussian blob as a premultiplied RGBA image.

    The old paintEvent recomputed three 320x200 exponential fields on every
    frame. The blob shape only changes when the palette/glow/theme changes, so
    it is generated once here and the paint loop just blits it.
    """
    import numpy as np

    axis = np.linspace(-1.0, 1.0, size, dtype=np.float32)
    u = axis[None, :]
    v = axis[:, None]
    alpha = np.clip(np.exp(-(u * u + v * v) * 3.0) * float(peak_alpha),
                    0.0, 1.0)
    rgba = np.empty((size, size, 4), dtype=np.uint8)
    for ch in range(3):
        rgba[:, :, ch] = np.clip(float(rgb[ch]) * alpha, 0.0, 255.0).astype(np.uint8)
    rgba[:, :, 3] = (alpha * 255.0).astype(np.uint8)
    return QImage(rgba.data, size, size, size * 4,
                  QImage.Format_ARGB32_Premultiplied).copy()


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
        self._sprites = None
        self._sprite_key = None
        self._frame = None
        self._dither_tile = None
        self._timer = QTimer(self)
        self._timer.setInterval(_LAVA_INTERVAL_IDLE_MS)
        self._timer.timeout.connect(self._tick)

    def set_palette(self, colors):
        if colors and len(colors) >= 3:
            self._palette = list(colors[:3])
            self._sprite_key = None
            self.update()

    def sync_active(self):
        theme = getattr(self.mw, "current_theme", "classic")
        glow = float(getattr(self.mw, "ambient_opacity", 0.3))
        dyn = bool(getattr(self.mw, "dynamic_ambient", True))
        low_cpu = bool(getattr(getattr(self.mw, "engine", None), "low_end_mode", False))
        show = dyn and theme in ("classic", "light", "liquid_glass", "macos27_glass") and glow > 0.01
        # Glass 2.0 should feel like a native static material, not a lava lamp.
        active = show and not low_cpu and theme != "macos27_glass"
        self.setVisible(show)
        if active:
            playing = bool(getattr(getattr(self.mw, "engine", None), "is_playing", False))
            self._timer.setInterval(_LAVA_INTERVAL_PLAYING_MS if playing
                                    else _LAVA_INTERVAL_IDLE_MS)
            if not self._timer.isActive():
                self._timer.start()
        elif self._timer.isActive():
            self._timer.stop()
        self.update()

    def _tick(self):
        if not self.isVisible() or self.window().isMinimized():
            return
        # Keep the drift alive during playback, but at 15 FPS instead of 30.
        # The phase step compensates so the apparent speed stays the same.
        playing = bool(getattr(getattr(self.mw, "engine", None), "is_playing", False))
        interval = _LAVA_INTERVAL_PLAYING_MS if playing else _LAVA_INTERVAL_IDLE_MS
        if self._timer.interval() != interval:
            self._timer.setInterval(interval)
        self._phase += 0.016 if playing else 0.008
        self.update()

    def _peak_alpha(self, theme, glow):
        if theme == "macos27_glass":
            peak_scale = 0.09
        elif theme == "liquid_glass":
            peak_scale = 0.22
        elif theme == "light":
            peak_scale = 0.36
        else:
            peak_scale = 0.44
        return peak_scale * glow

    def _ensure_sprites(self, theme, glow):
        key = (tuple(self._palette), round(float(glow), 3), theme)
        if self._sprite_key != key:
            peak = self._peak_alpha(theme, glow)
            self._sprites = tuple(_make_lava_sprite(rgb, peak)
                                  for rgb in self._palette)
            self._sprite_key = key
        return self._sprites

    def paintEvent(self, event):
        from PySide6.QtGui import QPainter, QPixmap
        from PySide6.QtCore import QRectF
        import math, numpy as np

        theme = getattr(self.mw, "current_theme", "classic")
        if theme not in ("classic", "light", "liquid_glass", "macos27_glass"):
            return
        glow = max(0.0, min(1.0, float(getattr(self.mw, "ambient_opacity", 0.3))))
        if glow <= 0.01 or self.width() < 16 or self.height() < 16:
            return

        sprites = self._ensure_sprites(theme, glow)
        gw, gh = 256, 160
        if (self._frame is None or self._frame.width() != gw
                or self._frame.height() != gh):
            self._frame = QImage(gw, gh, QImage.Format_ARGB32_Premultiplied)
        if self._dither_tile is None:
            # 512x512 non-repeating 1-LSB grain, generated once instead of
            # tiled across the full window on every frame.
            rng = np.random.default_rng(1337)
            tile_arr = np.zeros((512, 512, 4), dtype=np.uint8)
            n = rng.integers(-1, 2, size=(512, 512), dtype=np.int16)
            tile_arr[:, :, :3] = np.where(n[:, :, None] > 0, 2, 0)
            tile_arr[:, :, 3] = np.where(n != 0, 2, 0)
            t_img = QImage(tile_arr.data, 512, 512, 512 * 4,
                           QImage.Format_ARGB32_Premultiplied).copy()
            self._dither_tile = QPixmap.fromImage(t_img)

        # Compose the three cached blobs into one small premultiplied frame.
        frame = self._frame
        frame.fill(Qt.transparent)
        fp = QPainter(frame)
        fp.setRenderHint(QPainter.SmoothPixmapTransform, True)
        t = self._phase
        blobs = (
            (0.28 + 0.22 * math.sin(t * 0.7),
             0.30 + 0.20 * math.cos(t * 0.5), 1.20),
            (0.72 + 0.20 * math.cos(t * 0.6),
             0.65 + 0.22 * math.sin(t * 0.8), 1.30),
            (0.50 + 0.25 * math.sin(t * 0.4 + 1.4),
             0.48 + 0.24 * math.cos(t * 0.65 + 0.8), 1.15),
        )
        for (ox, oy, scale), sprite in zip(blobs, sprites):
            cx, cy = ox * gw, oy * gh
            rw, rh = gw * scale, gh * scale * 1.333
            fp.drawImage(QRectF(cx - rw / 2.0, cy - rh / 2.0, rw, rh), sprite)
        if theme != "macos27_glass":
            fp.drawTiledPixmap(QRectF(0, 0, gw, gh), self._dither_tile)
        fp.end()

        # One smooth upscale of the small frame instead of a full-window
        # exponential field plus a full-window dither tile.
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.drawImage(self.rect(), frame)
        p.end()
