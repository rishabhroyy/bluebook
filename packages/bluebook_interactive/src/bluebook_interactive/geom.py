"""Pure geometry for Part 3: no models, no I/O, no state.

Frames: cam = OpenCV scene-camera frame (x right, y down, z forward, metres).
head = origin at the midpoint between the eyes, +x wearer's left, +y up, +z out of the face (metres).
P (3x4) maps head-frame points to glasses display px.
"""

from __future__ import annotations

import math

import numpy as np
from bluebook_core.protocol import Arrow, Primitive, Ring

# teach_page overlay band (display.teach_page img_l/img_r): anything outside is clipped by the compositor
BAND = (0.0, 88.0, 576.0, 232.0)
CENTER = ((BAND[0] + BAND[2]) / 2, (BAND[1] + BAND[3]) / 2)  # (288, 160)
RING_R = 40.0  # px; big enough to notice through the waveguide (20 was too small to register)
CHEVRON_INSET = 12.0  # px; keeps the arrowhead inside the band


def K_of(fx: float, fy: float, cx: float, cy: float) -> np.ndarray:
    return np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1]], dtype=float)


def backproject(K: np.ndarray, u: float, v: float, z: float) -> np.ndarray:
    """Pixel + z-depth (metres) -> 3D point in the camera frame."""
    return z * (np.linalg.inv(K) @ np.array([u, v, 1.0]))


def to_head(R: np.ndarray, t: np.ndarray, X_cam: np.ndarray) -> np.ndarray:
    """X_cam = R @ X_head + t  ->  X_head."""
    return R.T @ (np.asarray(X_cam, float) - t)


def project(P: np.ndarray, X: np.ndarray) -> np.ndarray:
    """Rows of (u, v, w); w <= 0 means the point is behind the viewer."""
    X = np.atleast_2d(np.asarray(X, float))
    h = (P @ np.c_[X, np.ones(len(X))].T).T
    w = h[:, 2:3]
    return np.c_[h[:, :2] / np.where(np.abs(w) < 1e-9, 1e-9, w), w]


def rodrigues(v) -> np.ndarray:
    """Rotation vector -> matrix."""
    v = np.asarray(v, float)
    th = float(np.linalg.norm(v))
    if th < 1e-12:
        return np.eye(3)
    a = v / th
    S = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]])
    return np.eye(3) + math.sin(th) * S + (1 - math.cos(th)) * S @ S


def rodrigues_inv(R: np.ndarray) -> np.ndarray:
    """Rotation matrix -> rotation vector (angles below ~pi)."""
    th = math.acos(max(-1.0, min(1.0, (np.trace(R) - 1) / 2)))
    if th < 1e-9:
        return np.zeros(3)
    ax = np.array([R[2, 1] - R[1, 2], R[0, 2] - R[2, 0], R[1, 0] - R[0, 1]])
    n = np.linalg.norm(ax)
    if n < 1e-9:  # th ~ pi: axis from the symmetric part
        k = int(np.argmax(np.diag(R)))
        a = R[:, k] + np.eye(3)[:, k]
        return a / np.linalg.norm(a) * th
    return ax / n * th


def rot_angle(R1: np.ndarray, R2: np.ndarray) -> float:
    """Angle (rad) of the rotation between R1 and R2."""
    return math.acos(max(-1.0, min(1.0, (np.trace(R1.T @ R2) - 1) / 2)))


def rot_between(a, b) -> np.ndarray:
    """Minimal rotation matrix taking direction a onto direction b."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    a, b = a / np.linalg.norm(a), b / np.linalg.norm(b)
    c = np.cross(a, b)
    s = float(np.linalg.norm(c))
    if s < 1e-12:
        if a @ b > 0:
            return np.eye(3)
        c = np.cross(a, [1.0, 0, 0] if abs(a[0]) < 0.9 else [0, 1.0, 0])  # antiparallel: any perpendicular axis
        return rodrigues(c / np.linalg.norm(c) * math.pi)
    return rodrigues(c / s * math.atan2(s, float(a @ b)))


def az_el(d) -> tuple[float, float]:
    """Direction in the head frame -> (azimuth, elevation) in degrees; +az = wearer's left, +el = up."""
    return math.degrees(math.atan2(d[0], d[2])), math.degrees(math.atan2(d[1], math.hypot(d[0], d[2])))


def dir_of(az: float, el: float) -> np.ndarray:
    """Inverse of az_el (degrees -> unit head-frame direction)."""
    a, e = math.radians(az), math.radians(el)
    return np.array([math.cos(e) * math.sin(a), math.sin(e), math.cos(e) * math.cos(a)])


def in_band(u: float, v: float, w: float, margin: float = 0.0) -> bool:
    """Is the point in front of the viewer and inside the band grown by `margin` px (negative shrinks)?"""
    x0, y0, x1, y1 = BAND
    return w > 0 and x0 - margin <= u <= x1 + margin and y0 - margin <= v <= y1 + margin


def edge_point(u: float, v: float, w: float) -> tuple[float, float]:
    """Where the chevron for an off-band point sits: on the inset band edge, along the direction from the centre.
    Behind the viewer the projection is mirrored, so the direction flips (continuous through w = 0)."""
    cx, cy = CENTER
    dx, dy = (u - cx, v - cy) if w > 0 else (cx - u, cy - v)
    if math.hypot(dx, dy) < 1e-6:
        dx, dy = 0.0, -1.0
    hx, hy = (BAND[2] - BAND[0]) / 2 - CHEVRON_INSET, (BAND[3] - BAND[1]) / 2 - CHEVRON_INSET
    s = min(hx / abs(dx) if dx else math.inf, hy / abs(dy) if dy else math.inf)
    return cx + dx * s, cy + dy * s


def place(u: float, v: float, w: float, chevron: bool | None = None) -> list[Primitive]:
    """In the visible band: one ring. Outside it: a chevron ("V") at the band edge pointing at the point.
    `chevron` forces the glyph (the renderer decides with hysteresis); None = decide by the band."""
    if chevron is None:
        chevron = not in_band(u, v, w)
    if not chevron:
        return [Ring(cx=u, cy=v, r=RING_R)]
    cx, cy = CENTER
    tx, ty = edge_point(u, v, w)
    dx, dy = tx - cx, ty - cy
    n = math.hypot(dx, dy)
    # just the "V": a 2 px shaft so draw() still knows the direction (cheaper, smaller PNG than a long arrow)
    return [Arrow(x0=tx - 2 * dx / n, y0=ty - 2 * dy / n, x1=tx, y1=ty)]
