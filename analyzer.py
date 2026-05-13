"""
analyzer.py — OpenCV / NumPy Detection, Trajectory & Turn-Detection Engine
============================================================================
Portable module (no Android imports).  Consumed by service/main.py.

Features
--------
- AnalyzerConfig      : HSV ranges, physics params, resolution scale factor.
- TurnDetectorConfig  : Sensitivity knobs for the auto-detector.
- TurnDetector        : Two-strategy "your turn" detector.
    Strategy 1 — Aiming-line Hough scan:
        Thresholds the region around the player disc for bright pixels,
        masks out the disc itself, then runs HoughLinesP.  The aiming
        guide drawn by Soccer Stars is a long bright line that fires this.
    Strategy 2 — Motion detection:
        Frame-difference in the player ROI between consecutive frames.
        The moving aiming arrow registers as pixel change even when
        Strategy 1 misses (e.g. faint dotted line).
- analyse_frame()     : Full pipeline — scale → detect → trajectory.
- check_turn()        : Lightweight turn-detection pass for hibernate mode.

Power-saving note
-----------------
scale_factor=0.5 (default) halves width/height → 25 % pixel count.
Detected coordinates are scaled back to native resolution before return.
"""

from __future__ import annotations
import numpy as np
import cv2
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# AnalyzerConfig
# ---------------------------------------------------------------------------

@dataclass
class AnalyzerConfig:
    """Detection and physics configuration."""

    # Ball colour — default: white / very bright
    ball_lower_hsv: np.ndarray = field(
        default_factory=lambda: np.array([0, 0, 200], np.uint8))
    ball_upper_hsv: np.ndarray = field(
        default_factory=lambda: np.array([180, 40, 255], np.uint8))

    # Active player disc colour — default: bright blue
    player_lower_hsv: np.ndarray = field(
        default_factory=lambda: np.array([100, 150, 100], np.uint8))
    player_upper_hsv: np.ndarray = field(
        default_factory=lambda: np.array([130, 255, 255], np.uint8))

    # Minimum contour area (px²) — noise filter
    ball_min_area: int   = 200
    player_min_area: int = 500

    # Physics
    max_bounces: int = 5
    ray_length: int  = 800
    margin: int      = 10

    # Resolution scaling: 0.5 → analyse at 50 % W×H (25 % pixels).
    # Coords are scaled back to native resolution after detection.
    scale_factor: float = 0.5

    @classmethod
    def from_prefs(cls, prefs: dict) -> "AnalyzerConfig":
        """
        Build from the JSON prefs dict written by HSVTunerScreen.

        Expected format::

            {
                "ball":         {"h_lo", "s_lo", "v_lo", "h_hi", "s_hi", "v_hi"},
                "player":       { same },
                "scale_factor": float   (optional)
            }
        """
        cfg = cls()
        b = prefs.get("ball", {})
        p = prefs.get("player", {})

        if b:
            cfg.ball_lower_hsv = np.array(
                [b.get("h_lo", 0),   b.get("s_lo", 0),   b.get("v_lo", 200)], np.uint8)
            cfg.ball_upper_hsv = np.array(
                [b.get("h_hi", 180), b.get("s_hi", 40),  b.get("v_hi", 255)], np.uint8)
        if p:
            cfg.player_lower_hsv = np.array(
                [p.get("h_lo", 100), p.get("s_lo", 150), p.get("v_lo", 100)], np.uint8)
            cfg.player_upper_hsv = np.array(
                [p.get("h_hi", 130), p.get("s_hi", 255), p.get("v_hi", 255)], np.uint8)

        if "scale_factor" in prefs:
            cfg.scale_factor = max(0.1, min(1.0, float(prefs["scale_factor"])))

        return cfg


# ---------------------------------------------------------------------------
# TurnDetectorConfig
# ---------------------------------------------------------------------------

@dataclass
class TurnDetectorConfig:
    """
    Sensitivity parameters for the "your turn" auto-detector.

    All pixel measurements are in **native** resolution.  The detector
    internally rescales its ROI crop, not the global frame.

    Attributes
    ----------
    enabled              : Master on/off switch (toggle from HomeScreen).
    scan_radius          : Half-side of the square ROI centred on player (px).
    line_brightness      : Minimum brightness (0-255) to include in Hough mask.
    hough_threshold      : Hough accumulator votes needed to accept a line.
    min_line_length      : Shortest aiming-line segment accepted (px).
    max_line_gap         : Maximum gap inside a line before it is split (px).
    motion_pixel_thresh  : Per-pixel absolute diff counted as "moved".
    motion_area_fraction : Fraction of ROI pixels that must move (0-1).
    """
    enabled: bool             = True
    scan_radius: int          = 180
    line_brightness: int      = 180
    hough_threshold: int      = 20
    min_line_length: int      = 40
    max_line_gap: int         = 15
    motion_pixel_thresh: int  = 25
    motion_area_fraction: float = 0.02

    @classmethod
    def from_prefs(cls, prefs: dict) -> "TurnDetectorConfig":
        cfg = cls()
        td = prefs.get("turn_detector", {})
        if "enabled" in td:
            cfg.enabled = bool(td["enabled"])
        for key in ("scan_radius", "line_brightness", "hough_threshold",
                    "min_line_length", "max_line_gap", "motion_pixel_thresh"):
            if key in td:
                setattr(cfg, key, int(td[key]))
        if "motion_area_fraction" in td:
            cfg.motion_area_fraction = float(td["motion_area_fraction"])
        return cfg


# ---------------------------------------------------------------------------
# TurnDetector
# ---------------------------------------------------------------------------

class TurnDetector:
    """
    Stateful detector — call is_your_turn() once per hibernate-cycle frame.

    Keeps a single previous-frame reference for motion detection.
    Reset between overlay start/stop by constructing a new instance.
    """

    def __init__(self) -> None:
        self._prev_gray: Optional[np.ndarray] = None

    def is_your_turn(
        self,
        frame: np.ndarray,
        player: Optional["DetectedObject"],
        cfg: TurnDetectorConfig,
    ) -> bool:
        """
        Return True when the detector believes it is the player's turn.

        Parameters
        ----------
        frame  : Full-resolution BGR frame from MediaProjection.
        player : Detected active player disc (or None → always False).
        cfg    : Sensitivity configuration.
        """
        if not cfg.enabled or player is None:
            self._prev_gray = None
            return False

        line_hit   = self._detect_aiming_line(frame, player, cfg)
        motion_hit = self._detect_motion(frame, player, cfg)
        return line_hit or motion_hit

    # ------------------------------------------------------------------
    # Strategy 1 — Hough aiming-line scan
    # ------------------------------------------------------------------

    def _detect_aiming_line(
        self,
        frame: np.ndarray,
        player: "DetectedObject",
        cfg: TurnDetectorConfig,
    ) -> bool:
        """
        Look for a long bright line extending away from the player disc.

        Steps
        -----
        1. Crop square ROI around player (cfg.scan_radius on each side).
        2. Convert to grayscale and threshold at cfg.line_brightness.
        3. Erase the player disc itself (circle mask) to remove its own
           bright body from the Hough input.
        4. Run HoughLinesP — any accepted line means the aiming guide is visible.
        """
        roi, cx, cy = self._player_roi(frame, player, cfg.scan_radius)
        if roi.size == 0:
            return False

        gray    = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        _, mask = cv2.threshold(gray, cfg.line_brightness, 255, cv2.THRESH_BINARY)

        # Erase the bright player disc so it does not generate false lines
        disc_r = max(player.radius + 10, 18)
        cv2.circle(mask, (cx, cy), disc_r, 0, -1)

        lines = cv2.HoughLinesP(
            mask,
            rho=1,
            theta=np.pi / 180,
            threshold=cfg.hough_threshold,
            minLineLength=cfg.min_line_length,
            maxLineGap=cfg.max_line_gap,
        )
        return lines is not None and len(lines) > 0

    # ------------------------------------------------------------------
    # Strategy 2 — Motion detection via frame difference
    # ------------------------------------------------------------------

    def _detect_motion(
        self,
        frame: np.ndarray,
        player: "DetectedObject",
        cfg: TurnDetectorConfig,
    ) -> bool:
        """
        Detect significant pixel change in the player ROI between frames.

        Returns False on the first call (no previous frame to compare).
        """
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape

        r  = cfg.scan_radius
        x1 = max(0, player.x - r)
        y1 = max(0, player.y - r)
        x2 = min(w, player.x + r)
        y2 = min(h, player.y + r)

        curr_roi = gray[y1:y2, x1:x2]

        if self._prev_gray is None or self._prev_gray.shape != gray.shape:
            self._prev_gray = gray
            return False

        prev_roi = self._prev_gray[y1:y2, x1:x2]

        if curr_roi.shape != prev_roi.shape or curr_roi.size == 0:
            self._prev_gray = gray
            return False

        diff     = cv2.absdiff(curr_roi, prev_roi)
        changed  = int(np.count_nonzero(diff > cfg.motion_pixel_thresh))
        fraction = changed / curr_roi.size

        self._prev_gray = gray
        return fraction > cfg.motion_area_fraction

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    @staticmethod
    def _player_roi(
        frame: np.ndarray,
        player: "DetectedObject",
        radius: int,
    ) -> tuple[np.ndarray, int, int]:
        """
        Crop a square ROI centred on the player disc.

        Returns (roi_bgr, cx_in_roi, cy_in_roi).
        """
        h, w = frame.shape[:2]
        x1 = max(0, player.x - radius)
        y1 = max(0, player.y - radius)
        x2 = min(w, player.x + radius)
        y2 = min(h, player.y + radius)
        roi = frame[y1:y2, x1:x2]
        cx  = player.x - x1
        cy  = player.y - y1
        return roi, cx, cy


# ---------------------------------------------------------------------------
# Detection primitives
# ---------------------------------------------------------------------------

@dataclass
class DetectedObject:
    x: int
    y: int
    radius: int

    @property
    def centre_f(self) -> np.ndarray:
        return np.array([self.x, self.y], dtype=np.float64)

    def to_list(self) -> list:
        return [self.x, self.y, self.radius]

    def scaled_up(self, factor: float) -> "DetectedObject":
        """Return a new object with coordinates scaled to native resolution."""
        if factor >= 1.0:
            return self
        inv = 1.0 / factor
        return DetectedObject(
            int(round(self.x * inv)),
            int(round(self.y * inv)),
            int(round(self.radius * inv)),
        )


def _detect_by_colour(
    frame: np.ndarray,
    lower: np.ndarray,
    upper: np.ndarray,
    min_area: int,
) -> Optional[DetectedObject]:
    """HSV mask → morphological cleanup → largest contour → enclosing circle."""
    hsv  = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, lower, upper)

    k    = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,  k, iterations=2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k, iterations=2)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    valid = [c for c in contours if cv2.contourArea(c) >= min_area]
    if not valid:
        return None

    largest    = max(valid, key=cv2.contourArea)
    (cx, cy), r = cv2.minEnclosingCircle(largest)
    return DetectedObject(int(cx), int(cy), int(r))


def detect_ball(frame: np.ndarray, cfg: AnalyzerConfig) -> Optional[DetectedObject]:
    return _detect_by_colour(
        frame, cfg.ball_lower_hsv, cfg.ball_upper_hsv, cfg.ball_min_area)


def detect_player(frame: np.ndarray, cfg: AnalyzerConfig) -> Optional[DetectedObject]:
    return _detect_by_colour(
        frame, cfg.player_lower_hsv, cfg.player_upper_hsv, cfg.player_min_area)


# ---------------------------------------------------------------------------
# Trajectory physics
# ---------------------------------------------------------------------------

def _reflect(d: np.ndarray, n: np.ndarray) -> np.ndarray:
    """Reflect unit direction d off a surface with unit normal n."""
    n = n / np.linalg.norm(n)
    return d - 2.0 * np.dot(d, n) * n


def _intersect_v(origin, direction, x_wall, y_min, y_max):
    dx = direction[0]
    if abs(dx) < 1e-9:
        return None, None
    t = (x_wall - origin[0]) / dx
    if t <= 1e-3:
        return None, None
    y = origin[1] + t * direction[1]
    if y_min <= y <= y_max:
        return t, np.array([x_wall, y])
    return None, None


def _intersect_h(origin, direction, y_wall, x_min, x_max):
    dy = direction[1]
    if abs(dy) < 1e-9:
        return None, None
    t = (y_wall - origin[1]) / dy
    if t <= 1e-3:
        return None, None
    x = origin[0] + t * direction[0]
    if x_min <= x <= x_max:
        return t, np.array([x, y_wall])
    return None, None


def compute_trajectory(
    ball: DetectedObject,
    player: DetectedObject,
    frame_shape: tuple,
    cfg: AnalyzerConfig,
) -> list[tuple[int, int]]:
    """
    Ray-cast with wall reflections (r = d - 2(d·n)n).

    Direction is player→ball extended beyond the ball.
    Returns list of (x, y) native-resolution waypoints.
    """
    h, w = frame_shape[:2]
    m    = cfg.margin
    x_min, x_max = float(m), float(w - m)
    y_min, y_max = float(m), float(h - m)

    raw  = ball.centre_f - player.centre_f
    norm = np.linalg.norm(raw)
    if norm < 1e-6:
        return [(ball.x, ball.y)]

    direction = raw / norm
    pos       = ball.centre_f.copy()
    waypoints: list[tuple[int, int]] = [(ball.x, ball.y)]

    walls = [
        (_intersect_v, (x_min, y_min, y_max), np.array([ 1.0,  0.0])),
        (_intersect_v, (x_max, y_min, y_max), np.array([-1.0,  0.0])),
        (_intersect_h, (y_min, x_min, x_max), np.array([ 0.0,  1.0])),
        (_intersect_h, (y_max, x_min, x_max), np.array([ 0.0, -1.0])),
    ]

    for _ in range(cfg.max_bounces + 1):
        best_t  = np.inf
        best_pt = pos + direction * cfg.ray_length
        best_n  = None

        for fn, args, normal in walls:
            t, pt = fn(pos, direction, *args)
            if t is not None and t < best_t:
                best_t, best_pt, best_n = t, pt, normal

        waypoints.append((int(best_pt[0]), int(best_pt[1])))

        if best_n is None or best_t >= cfg.ray_length:
            break

        direction = _reflect(direction, best_n)
        pos       = best_pt

    return waypoints


# ---------------------------------------------------------------------------
# High-level pipelines
# ---------------------------------------------------------------------------

def analyse_frame(frame: np.ndarray, cfg: AnalyzerConfig) -> dict:
    """
    Full detection + trajectory pipeline.

    Resolution scaling: frame is downscaled by cfg.scale_factor before all
    OpenCV work; detected coordinates are scaled back to native resolution.

    Returns
    -------
    dict  "waypoints": [[x,y], ...]   (native res)
          "ball":      [x,y,r] | null
          "player":    [x,y,r] | null
    """
    sf = cfg.scale_factor
    if sf < 1.0:
        small = cv2.resize(frame, (0, 0), fx=sf, fy=sf,
                           interpolation=cv2.INTER_LINEAR)
    else:
        small = frame

    ball_s   = detect_ball(small, cfg)
    player_s = detect_player(small, cfg)

    ball   = ball_s.scaled_up(sf)   if ball_s   else None
    player = player_s.scaled_up(sf) if player_s else None

    waypoints: list[list[int]] = []
    if ball is not None and player is not None:
        pts       = compute_trajectory(ball, player, frame.shape, cfg)
        waypoints = [[x, y] for x, y in pts]

    return {
        "waypoints": waypoints,
        "ball":      ball.to_list()   if ball   else None,
        "player":    player.to_list() if player else None,
    }


def check_turn(
    frame: np.ndarray,
    cfg: AnalyzerConfig,
    turn_cfg: TurnDetectorConfig,
    detector: TurnDetector,
) -> bool:
    """
    Lightweight "is it my turn?" check for hibernate mode.

    Only runs player-disc detection (one colour mask) + the TurnDetector.
    No ball detection, no trajectory — fast enough at HIBERNATE_INTERVAL cadence.
    """
    if not turn_cfg.enabled:
        return False

    sf = cfg.scale_factor
    if sf < 1.0:
        small = cv2.resize(frame, (0, 0), fx=sf, fy=sf,
                           interpolation=cv2.INTER_LINEAR)
    else:
        small = frame

    player_s = detect_player(small, cfg)
    player   = player_s.scaled_up(sf) if player_s else None

    return detector.is_your_turn(frame, player, turn_cfg)
