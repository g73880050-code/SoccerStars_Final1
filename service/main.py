"""
service/main.py — Android Background Capture Service
=====================================================
Runs as an Android Foreground Service via python-for-android.

Architecture
------------
                    ┌─────────────────────┐
   MediaProjection  │  ScreenCaptureManager│  BGR frames
   ─────────────►  │  (ImageReader)       │ ──────────►  analyse_frame()
                    └─────────────────────┘                    │
                                                               │ JSON result
                    ┌─────────────────────┐                    ▼
   Overlay (UDP     │  IPC listener thread │          _send_result() ──► UDP 54321
   54322) ────────► │  (commands)         │
                    └─────────────────────┘

Power-saving loop
-----------------
ACTIVE    : capture → analyse_frame() → broadcast, capped at TARGET_FPS.
HIBERNATE : drain ImageReader silently; run check_turn() every
            TURN_CHECK_INTERVAL — if turn detected, self-wake for
            WAKE_HOLD_SECONDS.
WAKE GRANT: "wake" IPC command (button touch-down) overrides hibernate
            for WAKE_HOLD_SECONDS regardless of power state.

IPC commands (UDP 54322)
------------------------
set_hsv         {"cmd":"set_hsv","prefs":{...}}
set_power       {"cmd":"set_power","state":"active"|"hibernate"}
wake            {"cmd":"wake"}
set_scale       {"cmd":"set_scale","factor":float}
set_auto_detect {"cmd":"set_auto_detect","enabled":bool}
"""

from __future__ import annotations
import json
import os
import sys
import socket
import threading
import time
import numpy as np
from kivy.logger import Logger

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from analyzer import (
    AnalyzerConfig, TurnDetectorConfig, TurnDetector,
    analyse_frame, check_turn,
)

IS_ANDROID = os.path.exists("/system/build.prop")

IPC_OVERLAY_PORT = 54321
IPC_SERVICE_PORT = 54322

NOTIFICATION_ID  = 1001
CHANNEL_ID       = "SoccerStarsChannel"

# ---------------------------------------------------------------------------
# Power-saving constants
# ---------------------------------------------------------------------------
TARGET_FPS           = 15
FRAME_INTERVAL       = 1.0 / TARGET_FPS
HIBERNATE_INTERVAL   = 0.5        # seconds per hibernate cycle
WAKE_HOLD_SECONDS    = 3.0        # on-demand active burst after "wake" command
TURN_CHECK_INTERVAL  = 0.5        # how often to run check_turn() in hibernate

POWER_ACTIVE    = "active"
POWER_HIBERNATE = "hibernate"

# ---------------------------------------------------------------------------
# Android imports
# ---------------------------------------------------------------------------
if IS_ANDROID:
    from jnius import autoclass, cast  # type: ignore

    PythonService  = autoclass("org.kivy.android.PythonService")
    Handler        = autoclass("android.os.Handler")
    Looper         = autoclass("android.os.Looper")
    ImageReader    = autoclass("android.media.ImageReader")
    DisplayMetrics = autoclass("android.util.DisplayMetrics")
    Context        = autoclass("android.content.Context")
    NotifChannel   = autoclass("android.app.NotificationChannel")
    NotifBuilder   = autoclass("androidx.core.app.NotificationCompat$Builder")


# ---------------------------------------------------------------------------
# Foreground notification
# ---------------------------------------------------------------------------

def _create_notification_channel(ctx):
    if not IS_ANDROID:
        return
    mgr = cast("android.app.NotificationManager",
               ctx.getSystemService(Context.NOTIFICATION_SERVICE))
    mgr.createNotificationChannel(NotifChannel(CHANNEL_ID, "Soccer Stars Analyzer", 2))


def _build_notification(ctx):
    return (NotifBuilder(ctx, CHANNEL_ID)
            .setContentTitle("Soccer Stars Analyzer")
            .setContentText("Overlay active — capturing screen")
            .setSmallIcon(ctx.getResources().getIdentifier(
                "ic_launcher", "mipmap", ctx.getPackageName()))
            .build())


# ---------------------------------------------------------------------------
# Screen capture
# ---------------------------------------------------------------------------

class ScreenCaptureManager:
    """Wraps Android MediaProjection + ImageReader."""

    def __init__(self, result_code: int, data_intent, width: int, height: int, density: int):
        self._result_code    = result_code
        self._data_intent    = data_intent
        self.width           = width
        self.height          = height
        self._density        = density
        self._projection     = None
        self._image_reader   = None
        self._virtual_display = None

    def start(self):
        if not IS_ANDROID:
            Logger.info("ScreenCapture: no-op on non-Android platform.")
            return

        ctx    = PythonService.mService
        mp_mgr = cast("android.media.projection.MediaProjectionManager",
                      ctx.getSystemService(Context.MEDIA_PROJECTION_SERVICE))
        self._projection  = mp_mgr.getMediaProjection(self._result_code, self._data_intent)
        RGBA_8888         = 1
        self._image_reader = ImageReader.newInstance(self.width, self.height, RGBA_8888, 2)
        VirtualDisplay     = autoclass("android.hardware.display.VirtualDisplay")
        self._virtual_display = self._projection.createVirtualDisplay(
            "SoccerStarsCapture", self.width, self.height, self._density, 4,
            self._image_reader.getSurface(), None, Handler(Looper.getMainLooper()),
        )
        Logger.info("ScreenCapture: MediaProjection started.")

    def acquire_bgr(self) -> "np.ndarray | None":
        """Acquire one BGR frame; return None if no frame is ready."""
        if not IS_ANDROID or self._image_reader is None:
            return None
        image = self._image_reader.acquireLatestImage()
        if image is None:
            return None
        try:
            planes       = image.getPlanes()
            buf          = planes[0].getBuffer()
            row_stride   = planes[0].getRowStride()
            pixel_stride = planes[0].getPixelStride()
            raw          = bytearray(buf.remaining())
            buf.get(raw)
            flat   = np.frombuffer(raw, dtype=np.uint8)
            n_cols = row_stride // pixel_stride
            rgba   = flat.reshape((self.height, n_cols, 4))[:, :self.width, :]
            return np.ascontiguousarray(rgba[:, :, [2, 1, 0]])
        finally:
            image.close()

    def drain(self):
        """Discard the latest buffered frame without processing it."""
        if not IS_ANDROID or self._image_reader is None:
            return
        try:
            img = self._image_reader.acquireLatestImage()
            if img is not None:
                img.close()
        except Exception:
            pass

    def stop(self):
        if IS_ANDROID:
            try:
                if self._virtual_display:
                    self._virtual_display.release()
                if self._projection:
                    self._projection.stop()
            except Exception as exc:
                Logger.warning(f"ScreenCapture: stop error: {exc}")


# ---------------------------------------------------------------------------
# IPC helpers
# ---------------------------------------------------------------------------

def _send_result(sock: socket.socket, result: dict):
    sock.sendto(json.dumps(result).encode(), ("127.0.0.1", IPC_OVERLAY_PORT))


def _listen_for_commands(
    cfg_holder: list,
    turn_cfg_holder: list,
    power_holder: list,
    wake_until_holder: list,
    stop_event: threading.Event,
):
    """
    UDP command listener thread.

    Commands
    --------
    set_hsv         : Rebuild AnalyzerConfig from new HSV ranges.
    set_power       : Switch power state ("active" | "hibernate").
    wake            : Grant WAKE_HOLD_SECONDS of active processing.
    set_scale       : Change resolution scale factor live.
    set_auto_detect : Enable / disable the turn auto-detector.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", IPC_SERVICE_PORT))
    sock.settimeout(1.0)
    Logger.info(f"Service: command listener on UDP {IPC_SERVICE_PORT}")

    while not stop_event.is_set():
        try:
            data, _ = sock.recvfrom(8192)
            msg      = json.loads(data.decode())
            cmd      = msg.get("cmd")

            if cmd == "set_hsv":
                cfg_holder[0] = AnalyzerConfig.from_prefs(msg.get("prefs", {}))
                Logger.info("Service: HSV prefs updated.")

            elif cmd == "set_power":
                power_holder[0] = msg.get("state", POWER_ACTIVE)
                Logger.info(f"Service: power → {power_holder[0]}")

            elif cmd == "wake":
                wake_until_holder[0] = time.monotonic() + WAKE_HOLD_SECONDS
                Logger.info(f"Service: wake granted for {WAKE_HOLD_SECONDS}s")

            elif cmd == "set_scale":
                factor = float(msg.get("factor", 0.5))
                cfg_holder[0].scale_factor = max(0.1, min(1.0, factor))
                Logger.info(f"Service: scale_factor → {cfg_holder[0].scale_factor}")

            elif cmd == "set_auto_detect":
                turn_cfg_holder[0].enabled = bool(msg.get("enabled", True))
                Logger.info(f"Service: auto_detect → {turn_cfg_holder[0].enabled}")

        except socket.timeout:
            continue
        except Exception as exc:
            Logger.warning(f"Service cmd listener error: {exc}")

    sock.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_screen_dimensions() -> tuple[int, int, int]:
    if not IS_ANDROID:
        return 1080, 1920, 480
    ctx = PythonService.mService
    wm  = cast("android.view.WindowManager",
               ctx.getSystemService(Context.WINDOW_SERVICE))
    dm  = DisplayMetrics()
    wm.getDefaultDisplay().getRealMetrics(dm)
    return dm.widthPixels, dm.heightPixels, dm.densityDpi


def _parse_startup_args() -> dict:
    arg = os.environ.get("PYTHON_SERVICE_ARGUMENT", "{}")
    try:
        return json.loads(arg)
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Service main loop
# ---------------------------------------------------------------------------

def main():
    """
    Entry point called by python-for-android.

    Loop states
    -----------
    ACTIVE      Acquire frame → analyse_frame() → broadcast.
                Hard-capped at TARGET_FPS via FRAME_INTERVAL sleep.

    HIBERNATE   Drain ImageReader to prevent buffer overflow.
                Every TURN_CHECK_INTERVAL seconds, acquire one frame and
                run the lightweight check_turn() (player colour mask +
                Hough/motion scan). If a turn is detected, grant
                WAKE_HOLD_SECONDS of ACTIVE processing automatically.

    WAKE GRANT  Overrides HIBERNATE for WAKE_HOLD_SECONDS.
                Triggered by "wake" IPC command (button touch-down).
    """
    Logger.info("SoccerStarsService: starting.")
    args = _parse_startup_args()

    # Shared mutable state (single-element lists — thread-safe for GIL)
    cfg_holder        = [AnalyzerConfig.from_prefs(args.get("hsv_prefs", {}))]
    turn_cfg_holder   = [TurnDetectorConfig.from_prefs(args.get("hsv_prefs", {}))]
    power_holder      = [POWER_ACTIVE]
    wake_until_holder = [0.0]

    turn_detector = TurnDetector()

    # Foreground notification
    if IS_ANDROID:
        ctx = PythonService.mService
        _create_notification_channel(ctx)
        PythonService.mService.startForeground(NOTIFICATION_ID, _build_notification(ctx))

    # MediaProjection capture
    mp_result_code = int(os.environ.get("MP_RESULT_CODE", "-1"))
    mp_data_raw    = os.environ.get("MP_DATA", None)
    width, height, density = _get_screen_dimensions()

    if IS_ANDROID and mp_result_code != -1:
        capture = ScreenCaptureManager(mp_result_code, mp_data_raw, width, height, density)
        capture.start()
    else:
        Logger.info("SoccerStarsService: no MediaProjection token — idle mode.")
        capture = None

    # Command listener thread
    stop_event = threading.Event()
    threading.Thread(
        target=_listen_for_commands,
        args=(cfg_holder, turn_cfg_holder, power_holder, wake_until_holder, stop_event),
        daemon=True,
    ).start()

    out_sock              = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    last_frame_time       = 0.0
    last_turn_check_time  = 0.0
    sent_hibernate_clear  = False

    try:
        while not stop_event.is_set():
            now        = time.monotonic()
            wake_active = now < wake_until_holder[0]
            is_active   = (power_holder[0] == POWER_ACTIVE) or wake_active

            # ----------------------------------------------------------------
            # HIBERNATE MODE
            # ----------------------------------------------------------------
            if not is_active:
                # Clear canvas once on entry
                if not sent_hibernate_clear:
                    _send_result(out_sock, {
                        "waypoints": [], "ball": None, "player": None,
                        "turn_detected": False, "hibernating": True,
                    })
                    sent_hibernate_clear = True
                    Logger.info("SoccerStarsService: hibernating.")

                # Drain the image buffer
                if capture is not None:
                    capture.drain()

                # Auto-detect: run turn check at TURN_CHECK_INTERVAL cadence
                if (capture is not None and
                        turn_cfg_holder[0].enabled and
                        now - last_turn_check_time >= TURN_CHECK_INTERVAL):
                    last_turn_check_time = now
                    frame = capture.acquire_bgr()
                    if frame is not None:
                        if check_turn(frame, cfg_holder[0], turn_cfg_holder[0], turn_detector):
                            wake_until_holder[0] = now + WAKE_HOLD_SECONDS
                            Logger.info("TurnDetector: your turn — waking engine.")
                            _send_result(out_sock, {
                                "waypoints": [], "ball": None, "player": None,
                                "turn_detected": True, "hibernating": False,
                            })
                            sent_hibernate_clear = False
                            continue  # skip remaining sleep; re-evaluate at top

                time.sleep(HIBERNATE_INTERVAL)
                continue

            # ----------------------------------------------------------------
            # ACTIVE MODE
            # ----------------------------------------------------------------
            sent_hibernate_clear = False

            # FPS cap
            elapsed = now - last_frame_time
            if elapsed < FRAME_INTERVAL:
                time.sleep(FRAME_INTERVAL - elapsed)
                continue

            if capture is None:
                # No capture — send heartbeat
                _send_result(out_sock, {
                    "waypoints": [], "ball": None, "player": None,
                    "turn_detected": False, "hibernating": False,
                })
                time.sleep(FRAME_INTERVAL)
                last_frame_time = time.monotonic()
                continue

            frame = capture.acquire_bgr()
            if frame is None:
                time.sleep(0.01)
                continue

            result = analyse_frame(frame, cfg_holder[0])
            result["turn_detected"] = bool(result.get("player") is not None)
            result["hibernating"]   = False
            _send_result(out_sock, result)
            last_frame_time = time.monotonic()

    except KeyboardInterrupt:
        Logger.info("SoccerStarsService: interrupted.")
    finally:
        stop_event.set()
        if capture is not None:
            capture.stop()
        out_sock.close()
        Logger.info("SoccerStarsService: stopped.")


if __name__ == "__main__":
    main()

