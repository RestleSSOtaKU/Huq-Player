"""A small WebGPU-style liquid-glass lens, rendered with NumPy + QPainter.

This ports the effect math from arya3d/liquid-glass-webgpu's glassPassShader.ts
(rounded-box SDF -> lens bend -> RGB dispersion -> Fresnel/rim) into a PySide6
QWidget overlay, so REXA gets the refraction look without WebGPU or a browser.

The source is the album cover pixmap underneath the lens. The result is cached
and only recomputed when the cover, lens size, or theme changes.

Effect math adapted from https://github.com/arya3d/liquid-glass-webgpu
(MIT License, Copyright (c) 2026 arya3d). Full notice in
player_ui/LICENSES/liquid-glass-webgpu.LICENSE.
"""

import numpy as np
from PySide6.QtCore import Qt, QRect
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QWidget


class GlassLensOverlay(QWidget):
    def __init__(self, mw, parent=None):
        super().__init__(parent)
        self.mw = mw
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)
        self.setStyleSheet("background: transparent; border: none;")
        self._source = QPixmap()
        self._cache_key = None
        self._image = None
        self._ox = self._oy = 0

    def set_source(self, pixmap):
        self._source = QPixmap(pixmap) if isinstance(pixmap, QPixmap) and not pixmap.isNull() else QPixmap()
        self._cache_key = None
        self.sync_active()
        self.update()

    def sync_active(self):
        # The lens is a WebGPU-style flourish for the original Glass theme.
        # Glass 2.0 is meant to look like native macOS Settings, which
        # has no such pill, so keep it hidden there. Also hide when there is
        # no real cover to refract (never paint the accent-gradient fallback).
        theme = getattr(self.mw, "current_theme", "classic")
        visible = theme == "liquid_glass" and not self._source.isNull()
        self.setVisible(visible)
        if visible:
            self.sync_geometry()

    def sync_geometry(self):
        parent = self.parentWidget()
        if parent is None:
            return
        w = max(62, min(112, int(parent.width() * 0.72)))
        h = max(24, int(w * 0.38))
        x = (parent.width() - w) // 2
        y = max(6, min(int(parent.height() * 0.64), parent.height() - h - 6))
        if self.geometry() != QRect(x, y, w, h):
            self.setGeometry(x, y, w, h)
            self._cache_key = None
        self._ox, self._oy = x, y
        self.update()

    @staticmethod
    def _sdf(px, py, half_x, half_y, radius):
        qx = np.abs(px) - half_x + radius
        qy = np.abs(py) - half_y + radius
        outside = np.hypot(np.maximum(qx, 0.0), np.maximum(qy, 0.0))
        inside = np.minimum(np.maximum(qx, qy), 0.0)
        return outside + inside - radius

    @staticmethod
    def _sample(src, sx, sy):
        h, w = src.shape[:2]
        x0 = np.clip(np.floor(sx).astype(np.int32), 0, w - 1)
        y0 = np.clip(np.floor(sy).astype(np.int32), 0, h - 1)
        x1 = np.clip(x0 + 1, 0, w - 1)
        y1 = np.clip(y0 + 1, 0, h - 1)
        fx = np.clip(sx - x0, 0.0, 1.0)[..., None]
        fy = np.clip(sy - y0, 0.0, 1.0)[..., None]
        return (src[y0, x0] * (1 - fx) * (1 - fy) + src[y1, x0] * (1 - fx) * fy
                + src[y0, x1] * fx * (1 - fy) + src[y1, x1] * fx * fy)

    def _fallback_source(self, w, h):
        a = QColor(getattr(self.mw, "current_accent", "#0a84ff"))
        b = QColor(getattr(self.mw, "current_accent_end", "#64d2ff"))
        ca = np.array([a.red(), a.green(), a.blue()], np.float32)[None, None, :]
        cb = np.array([b.red(), b.green(), b.blue()], np.float32)[None, None, :]
        xs = np.linspace(0, 1, w * 3, dtype=np.float32)[None, :, None] * 0.6
        ys = np.linspace(0, 1, h * 3, dtype=np.float32)[:, None, None] * 0.4
        t = xs + ys
        rgb = np.clip(ca * (1 - t) + cb * t, 0, 255).astype(np.uint8)
        alpha = np.full((h * 3, w * 3, 1), 255, np.uint8)
        arr = np.concatenate([rgb, alpha], axis=2)
        return QImage(arr.data, w * 3, h * 3, w * 3 * 4, QImage.Format_RGBA8888).copy()

    def _render(self, image, w, h, ox, oy):
        raw = np.frombuffer(image.constBits(), np.uint8).reshape(image.height(), image.bytesPerLine() // 4, 4)
        src = raw[:, :image.width(), :3].astype(np.float32)

        aspect = w / h
        ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
        px = (xs - (w - 1) * 0.5) / h
        py = (ys - (h - 1) * 0.5) / h

        half_x, half_y = 0.5 * aspect, 0.32
        radius = min(0.15 * aspect, half_y * 0.98)
        d = self._sdf(px, py, half_x, half_y, radius)

        cov = np.clip(0.5 - d * h / 1.5, 0.0, 1.0)
        alpha = cov * cov * (3.0 - 2.0 * cov)

        eps = 1.0 / h
        nx = (self._sdf(px + eps, py, half_x, half_y, radius)
              - self._sdf(px - eps, py, half_x, half_y, radius)) / (2 * eps)
        ny = (self._sdf(px, py + eps, half_x, half_y, radius)
              - self._sdf(px, py - eps, half_x, half_y, radius)) / (2 * eps)
        inv = 1.0 / np.sqrt(nx * nx + ny * ny + 1.0)
        nx, ny, nz = nx * inv, ny * inv, inv

        fresnel = 0.04 + 0.96 * np.power(1.0 - nz, 5.0)
        depth = np.clip(-d / 0.30, 0.0, 1.0)
        bend = np.power(depth, 0.25) * 1.57
        sb, sb_r, sb_b = np.sin(bend), np.sin(bend * 0.98), np.sin(bend * 1.02)
        spread = (0.008 * (1.0 - nz * 0.909))[..., None]
        off = np.stack([px * sb, py * sb], axis=-1) * spread

        def lookup(lx, ly):
            return ox + (lx / aspect + 0.5) * w, oy + (ly + 0.5) * h

        behind = self._sample(src, ox + xs, oy + ys)
        red = self._sample(src, *lookup((px - off[..., 0]) * sb_r, (py - off[..., 1]) * sb_r))[..., 0]
        green = self._sample(src, *lookup(px * sb, py * sb))[..., 1]
        blue = self._sample(src, *lookup((px + off[..., 0]) * sb_b, (py + off[..., 1]) * sb_b))[..., 2]
        refracted = np.stack([red, green, blue], axis=-1)

        ring_cov = lambda dist: np.clip(0.5 - dist * h / 0.75, 0.0, 1.0)
        ring = ring_cov(d - eps) - ring_cov(d)
        light = np.array([0.3, -0.5, 1.0], np.float32)
        light /= np.linalg.norm(light)
        half_vec = np.array([0.3, -0.5, 2.0], np.float32)
        half_vec /= np.linalg.norm(half_vec)
        spec = np.power(np.maximum(nx * half_vec[0] + ny * half_vec[1] + nz * half_vec[2], 0.0), 32.0)
        edge = ring * (1.0 + fresnel * 2.0) + spec * ring * 0.5
        rim = np.power(1.0 - nz, 3.0) * cov * 0.15
        glass = (refracted + (edge + rim)[..., None]) * (1.0 + fresnel * 0.3)[..., None]
        colour = behind * (1.0 - alpha)[..., None] + glass * alpha[..., None]

        out = np.empty((h, w, 4), np.uint8)
        out[..., :3] = np.clip(colour, 0, 255).astype(np.uint8)
        out[..., 3] = np.clip(alpha * 255.0, 0, 255).astype(np.uint8)
        out[..., :3] = (out[..., :3].astype(np.float32) * (out[..., 3:4].astype(np.float32) / 255.0)).astype(np.uint8)
        return QImage(out.data, w, h, w * 4, QImage.Format_ARGB32_Premultiplied).copy()

    def paintEvent(self, event):
        w, h = self.width(), self.height()
        if w < 8 or h < 8 or self._source.isNull():
            # No real album cover: do not draw an accent-colored fake pill.
            self._image = None
            return
        key = (self._source.cacheKey(), w, h, self._ox, self._oy)
        if key != self._cache_key:
            self._cache_key = key
            image = self._source.toImage().convertToFormat(QImage.Format_RGBA8888)
            self._image = self._render(image, w, h, self._ox, self._oy)
        if self._image is None:
            return
        painter = QPainter(self)
        painter.drawImage(0, 0, self._image)
        painter.end()
