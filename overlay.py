"""
overlay.py — Floating Overlay Manager
======================================
Manages two Android WindowManager views on top of Soccer Stars:

1. Floating button  — draggable, always-on-top toggle.
   • ACTION_DOWN  → send "wake" (on-demand activation)
   • Short tap    → toggle canvas + send hibernate/active
   • Status ring  → green when turn detected, grey when hibernating

2. Prediction canvas — full-screen transparent View.
   • Draws trajectory polyline, bounce dots, ball/player circles.
   • postInvalidate() triggered by IPC thread on every result packet.
   • FLAG_NOT_TOUCHABLE so finger events pass through to Soccer Stars.

Power-saving signals sent to service
-------------------------------------
  button touch-down   → "wake"
  canvas hidden       → "set_power hibernate"
  canvas shown        → "set_power active"
  notify_background() → "set_power hibernate"  (on_pause)
  notify_foreground() → "set_power active"      (on_resume)
  set_auto_detect()   → "set_auto_detect"

IPC (service → overlay): UDP 54321, JSON per frame.
IPC (overlay → service): UDP 54322, JSON commands.
"""

from __future__ import annotations
import os
import json
import threading
import socket
from kivy.logger import Logger

IS_ANDROID = (
    os.environ.get("ANDROID_ARGUMENT") is not None
    or os.path.exists("/system/build.prop")
)

if IS_ANDROID:
    from jnius import autoclass, cast, PythonJavaClass, java_method  # type: ignore

    PythonActivity = autoclass("org.kivy.android.PythonActivity")
    Context        = autoclass("android.content.Context")
    LayoutParams   = autoclass("android.view.WindowManager$LayoutParams")
    PixelFormat    = autoclass("android.graphics.PixelFormat")
    Gravity        = autoclass("android.view.Gravity")
    ImageView      = autoclass("android.widget.ImageView")
    Paint          = autoclass("android.graphics.Paint")

IPC_OVERLAY_PORT = 54321   # service → overlay
IPC_SERVICE_PORT = 54322   # overlay → service


# ---------------------------------------------------------------------------
# FloatingOverlayManager
# ---------------------------------------------------------------------------

class FloatingOverlayManager:
    """
    Lifetime manager for the two overlay views.

    Parameters
    ----------
    hsv_prefs              : Loaded from disk by SoccerStarsApp.
    media_projection_token : (result_code, data) from Activity result.
    auto_detect_enabled    : Initial state of the turn auto-detector.
    """

    def __init__(
        self,
        hsv_prefs: dict,
        media_projection_token=None,
        auto_detect_enabled: bool = True,
    ):
        self._hsv_prefs   = hsv_prefs
        self._mp_token    = media_projection_token
        self._auto_detect = auto_detect_enabled

        self._wm           = None
        self._btn_view     = None
        self._canvas_view  = None
        self._ipc_thread   = None
        self._running      = False

        # Latest data from service — read by _PredictionView.onDraw()
        self.trajectory: list      = []
        self.ball: list | None     = None
        self.player: list | None   = None
        self.turn_detected: bool   = False
        self.hibernating: bool     = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self):
        if IS_ANDROID:
            self._setup_wm()
            self._add_floating_button()
            self._add_prediction_canvas()
        self._running = True
        self._ipc_thread = threading.Thread(target=self._ipc_loop, daemon=True)
        self._ipc_thread.start()
        self._start_capture_service()
        Logger.info("Overlay: started.")

    def stop(self):
        self._running = False
        if IS_ANDROID and self._wm is not None:
            for view in (self._btn_view, self._canvas_view):
                try:
                    if view:
                        self._wm.removeView(view)
                except Exception as exc:
                    Logger.warning(f"Overlay: removeView error: {exc}")
        self._stop_capture_service()
        Logger.info("Overlay: stopped.")

    def update_hsv_prefs(self, prefs: dict):
        self._hsv_prefs = prefs
        self._send_cmd({"cmd": "set_hsv", "prefs": prefs})

    def notify_background(self):
        """Called by SoccerStarsApp.on_pause() — hibernate the service."""
        Logger.info("Overlay: background → hibernating service.")
        self._send_cmd({"cmd": "set_power", "state": "hibernate"})

    def notify_foreground(self):
        """Called by SoccerStarsApp.on_resume() — wake the service."""
        Logger.info("Overlay: foreground → activating service.")
        if IS_ANDROID and self._canvas_view is not None:
            VISIBLE = 0
            if self._canvas_view.getVisibility() == VISIBLE:
                self._send_cmd({"cmd": "set_power", "state": "active"})
        else:
            self._send_cmd({"cmd": "set_power", "state": "active"})

    def set_auto_detect(self, enabled: bool):
        """Toggle the turn auto-detector and inform the service."""
        self._auto_detect = enabled
        self._send_cmd({"cmd": "set_auto_detect", "enabled": enabled})
        Logger.info(f"Overlay: auto_detect → {enabled}")

    # ------------------------------------------------------------------
    # IPC helper
    # ------------------------------------------------------------------

    def _send_cmd(self, msg: dict):
        payload = json.dumps(msg).encode()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.sendto(payload, ("127.0.0.1", IPC_SERVICE_PORT))
            s.close()
        except Exception as exc:
            Logger.warning(f"Overlay: _send_cmd error: {exc}")

    # ------------------------------------------------------------------
    # WindowManager helpers
    # ------------------------------------------------------------------

    def _setup_wm(self):
        ctx      = PythonActivity.mActivity
        self._wm = cast("android.view.WindowManager",
                        ctx.getSystemService(Context.WINDOW_SERVICE))

    def _make_lp(self, w, h, gravity, x=0, y=0, flags_extra=0):
        TYPE  = 2038            # TYPE_APPLICATION_OVERLAY
        FLAGS = 0x00000008 | 0x00000100 | flags_extra
        lp = LayoutParams(w, h, TYPE, FLAGS, PixelFormat.TRANSLUCENT)
        lp.gravity = gravity
        lp.x, lp.y = x, y
        return lp

    # ------------------------------------------------------------------
    # Floating button
    # ------------------------------------------------------------------

    def _add_floating_button(self):
        ctx = PythonActivity.mActivity
        btn = ImageView(ctx)
        btn.setImageResource(ctx.getResources().getIdentifier(
            "ic_launcher", "mipmap", ctx.getPackageName()))
        btn.setAlpha(0.88)

        lp = self._make_lp(120, 120, Gravity.TOP | Gravity.START, x=40, y=180)

        listener = _DragToggleTouchListener(
            wm=self._wm,
            lp=lp,
            on_tap=self._on_tap,
            on_down=self._on_touch_down,
        )
        btn.setOnTouchListener(listener)
        self._wm.addView(btn, lp)
        self._btn_view = btn
        Logger.info("Overlay: floating button added.")

    def _on_touch_down(self):
        """Finger touches button → immediately wake service (on-demand)."""
        self._send_cmd({"cmd": "wake"})

    def _on_tap(self):
        """Short tap → toggle canvas + matching power command."""
        if self._canvas_view is None:
            return
        if IS_ANDROID:
            VISIBLE, INVISIBLE = 0, 4
            vis = self._canvas_view.getVisibility()
            if vis == VISIBLE:
                self._canvas_view.setVisibility(INVISIBLE)
                self._send_cmd({"cmd": "set_power", "state": "hibernate"})
            else:
                self._canvas_view.setVisibility(VISIBLE)
                self._send_cmd({"cmd": "set_power", "state": "active"})

    # ------------------------------------------------------------------
    # Prediction canvas
    # ------------------------------------------------------------------

    def _add_prediction_canvas(self):
        ctx = PythonActivity.mActivity
        lp  = self._make_lp(
            -1, -1,
            Gravity.TOP | Gravity.START,
            flags_extra=0x00000010,    # FLAG_NOT_TOUCHABLE — passes touches through
        )
        view = _PredictionView(ctx, overlay=self)
        self._wm.addView(view, lp)
        self._canvas_view = view
        Logger.info("Overlay: prediction canvas added.")

    # ------------------------------------------------------------------
    # IPC listener (service → overlay)
    # ------------------------------------------------------------------

    def _ipc_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", IPC_OVERLAY_PORT))
        sock.settimeout(1.0)
        Logger.info(f"Overlay: IPC listener on UDP {IPC_OVERLAY_PORT}")

        while self._running:
            try:
                data, _ = sock.recvfrom(65535)
                msg      = json.loads(data.decode())

                self.trajectory    = msg.get("waypoints", [])
                self.ball          = msg.get("ball")
                self.player        = msg.get("player")
                self.turn_detected = bool(msg.get("turn_detected", False))
                self.hibernating   = bool(msg.get("hibernating", False))

                if IS_ANDROID and self._canvas_view is not None:
                    self._canvas_view.postInvalidate()
                    if self._btn_view is not None:
                        self._btn_view.postInvalidate()

            except socket.timeout:
                continue
            except Exception as exc:
                Logger.warning(f"Overlay IPC error: {exc}")

        sock.close()

    # ------------------------------------------------------------------
    # Service control
    # ------------------------------------------------------------------

    def _start_capture_service(self):
        if not IS_ANDROID:
            return
        Intent      = autoclass("android.content.Intent")
        PySvc       = autoclass("org.kivy.android.PythonService")
        ctx         = PythonActivity.mActivity
        svc_intent  = Intent(ctx, PySvc)
        payload     = json.dumps({
            "hsv_prefs":         self._hsv_prefs,
            "auto_detect":       self._auto_detect,
        })
        svc_intent.putExtra("python_service_argument", payload)
        ctx.startForegroundService(svc_intent)
        Logger.info("Overlay: capture service started.")

    def _stop_capture_service(self):
        if not IS_ANDROID:
            return
        try:
            Intent = autoclass("android.content.Intent")
            PySvc  = autoclass("org.kivy.android.PythonService")
            ctx    = PythonActivity.mActivity
            ctx.stopService(Intent(ctx, PySvc))
        except Exception as exc:
            Logger.warning(f"Overlay: stopService error: {exc}")


# ---------------------------------------------------------------------------
# Android Java helper classes
# ---------------------------------------------------------------------------

if IS_ANDROID:

    class _DragToggleTouchListener(PythonJavaClass):
        """
        OnTouchListener for the floating button.

        • ACTION_DOWN : record start pos; fire on_down() immediately.
        • ACTION_MOVE : reposition button via WindowManager.updateViewLayout.
        • ACTION_UP   : if total movement < 8 px → fire on_tap().
        """
        __javainterfaces__ = ["android/view/View$OnTouchListener"]
        __javacontext__    = "app"

        def __init__(self, wm, lp, on_tap, on_down=None):
            super().__init__()
            self._wm      = wm
            self._lp      = lp
            self._on_tap  = on_tap
            self._on_down = on_down
            self._lx = self._ly = self._sx = self._sy = 0.0

        @java_method("(Landroid/view/View;Landroid/view/MotionEvent;)Z")
        def onTouch(self, view, event):
            action = event.getAction()
            DOWN, MOVE, UP = 0, 2, 1

            if action == DOWN:
                self._lx = self._sx = event.getRawX()
                self._ly = self._sy = event.getRawY()
                if self._on_down:
                    self._on_down()
                return True

            if action == MOVE:
                self._lp.x += int(event.getRawX() - self._lx)
                self._lp.y += int(event.getRawY() - self._ly)
                self._wm.updateViewLayout(view, self._lp)
                self._lx = event.getRawX()
                self._ly = event.getRawY()
                return True

            if action == UP:
                if abs(event.getRawX() - self._sx) + abs(event.getRawY() - self._sy) < 8:
                    self._on_tap()
                return True

            return False

    class _PredictionView(autoclass("android.view.View")):
        """
        Custom View that renders the trajectory and status indicators.

        Status ring on the floating button area is drawn here as a
        small coloured circle in the corner of the screen.
        """
        __javacontext__ = "app"

        def __init__(self, ctx, overlay: FloatingOverlayManager):
            super().__init__(ctx)
            self._ov    = overlay
            self._paint = Paint()
            self._paint.setAntiAlias(True)
            self._paint.setStrokeWidth(4.0)

        def onDraw(self, canvas):
            canvas.drawColor(0x00000000)   # transparent

            ov         = self._ov
            trajectory = ov.trajectory
            ball       = ov.ball
            player     = ov.player

            # ---- Trajectory polyline ----
            self._paint.setColor(0xFF00FF00)   # bright green
            self._paint.setStyle(Paint.Style.STROKE)
            self._paint.setStrokeWidth(4.0)
            for i in range(1, len(trajectory)):
                x0, y0 = trajectory[i - 1]
                x1, y1 = trajectory[i]
                canvas.drawLine(float(x0), float(y0), float(x1), float(y1), self._paint)

            # ---- Bounce markers ----
            self._paint.setColor(0xFFFFA500)   # orange
            self._paint.setStyle(Paint.Style.FILL)
            for pt in trajectory[1:-1]:
                canvas.drawCircle(float(pt[0]), float(pt[1]), 12.0, self._paint)

            # ---- Ball outline ----
            if ball:
                self._paint.setColor(0xFF2196F3)   # blue
                self._paint.setStyle(Paint.Style.STROKE)
                self._paint.setStrokeWidth(3.0)
                canvas.drawCircle(float(ball[0]), float(ball[1]),
                                  float(ball[2]) + 6.0, self._paint)

            # ---- Player outline ----
            if player:
                self._paint.setColor(0xFFF44336)   # red
                self._paint.setStyle(Paint.Style.STROKE)
                self._paint.setStrokeWidth(3.0)
                canvas.drawCircle(float(player[0]), float(player[1]),
                                  float(player[2]) + 6.0, self._paint)

            # ---- Auto-detect status dot (top-left corner, near button) ----
            # Green  = turn detected / engine woke automatically
            # Yellow = auto-detect watching (hibernate, no turn yet)
            # Grey   = hibernating with auto-detect off
            if ov.turn_detected:
                dot_colour = 0xFF4CAF50    # green
            elif not ov.hibernating:
                dot_colour = 0xFF4CAF50    # green (active, player on screen)
            else:
                dot_colour = 0xFFFFEB3B    # yellow — watching

            self._paint.setColor(dot_colour)
            self._paint.setStyle(Paint.Style.FILL)
            canvas.drawCircle(170.0, 198.0, 12.0, self._paint)   # next to button

            # Outer ring
            self._paint.setColor(0xFFFFFFFF)
            self._paint.setStyle(Paint.Style.STROKE)
            self._paint.setStrokeWidth(2.0)
            canvas.drawCircle(170.0, 198.0, 12.0, self._paint)
