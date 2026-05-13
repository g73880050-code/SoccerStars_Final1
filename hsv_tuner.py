"""
hsv_tuner.py — HSV Colour Tuner Screen
========================================
Kivy Screen with six sliders per object (Ball + Active Player disc).

Features
--------
- Live colour swatch updates as sliders move.
- "Save & Apply" writes to disk and pushes a live update to the service.
- "Reset Defaults" restores factory HSV ranges.
- Scrollable layout — works on small phone screens.
"""

from __future__ import annotations
import colorsys
from kivy.uix.screenmanager import Screen
from kivy.uix.boxlayout     import BoxLayout
from kivy.uix.gridlayout    import GridLayout
from kivy.uix.scrollview    import ScrollView
from kivy.uix.slider        import Slider
from kivy.uix.label         import Label
from kivy.uix.button        import Button
from kivy.uix.widget        import Widget
from kivy.graphics          import Color, Rectangle
from kivy.metrics           import dp


# ---------------------------------------------------------------------------
# ColourSwatch
# ---------------------------------------------------------------------------

class ColourSwatch(Widget):
    """Small filled rectangle that previews the midpoint of an HSV range."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._r = self._g = self._b = 0.5
        self.size_hint = (1, None)
        self.height    = dp(36)
        self.bind(pos=self._redraw, size=self._redraw)

    def set_hsv(self, h_cv: float, s_cv: float, v_cv: float):
        """
        Update from OpenCV HSV values (H 0-180, S 0-255, V 0-255).
        Converts to Kivy RGB for canvas drawing.
        """
        r, g, b = colorsys.hsv_to_rgb(h_cv / 180.0, s_cv / 255.0, v_cv / 255.0)
        self._r, self._g, self._b = r, g, b
        self._redraw()

    def _redraw(self, *_):
        self.canvas.clear()
        with self.canvas:
            Color(self._r, self._g, self._b, 1)
            Rectangle(pos=self.pos, size=self.size)


# ---------------------------------------------------------------------------
# HSVGroup — six sliders + swatch for one object
# ---------------------------------------------------------------------------

class HSVGroup(BoxLayout):
    """
    Labelled slider group for H-lo, S-lo, V-lo, H-hi, S-hi, V-hi.

    Parameters
    ----------
    title   : Section heading displayed above the sliders.
    initial : Dict with keys h_lo, s_lo, v_lo, h_hi, s_hi, v_hi.
    """

    _PARAMS = [
        ("H low",  "h_lo", 0, 180),
        ("S low",  "s_lo", 0, 255),
        ("V low",  "v_lo", 0, 255),
        ("H high", "h_hi", 0, 180),
        ("S high", "s_hi", 0, 255),
        ("V high", "v_hi", 0, 255),
    ]

    def __init__(self, title: str, initial: dict, **kwargs):
        super().__init__(orientation="vertical", spacing=dp(4),
                         padding=(dp(6), dp(4)), **kwargs)
        self._sliders: dict[str, Slider] = {}

        # Section heading
        self.add_widget(Label(
            text=f"[b]{title}[/b]", markup=True,
            font_size="15sp", halign="left",
            size_hint=(1, None), height=dp(28),
        ))

        # Slider grid: [label | slider | value]
        grid = GridLayout(cols=3, spacing=dp(3), size_hint=(1, None))
        grid.bind(minimum_height=grid.setter("height"))

        for label_text, key, lo, hi in self._PARAMS:
            init_val = initial.get(key, lo)

            name_lbl = Label(
                text=label_text, font_size="11sp",
                size_hint=(None, None), width=dp(50), height=dp(34),
            )
            slider = Slider(
                min=lo, max=hi, value=init_val,
                size_hint=(1, None), height=dp(34),
            )
            val_lbl = Label(
                text=str(int(init_val)), font_size="11sp",
                size_hint=(None, None), width=dp(34), height=dp(34),
            )
            self._sliders[key] = slider

            def _on_change(_, v, vl=val_lbl):
                vl.text = str(int(v))
                self._refresh_swatch()

            slider.bind(value=_on_change)
            grid.add_widget(name_lbl)
            grid.add_widget(slider)
            grid.add_widget(val_lbl)

        self.add_widget(grid)

        # Live colour preview swatch
        self._swatch = ColourSwatch()
        self.add_widget(self._swatch)
        self._refresh_swatch()

    # ------------------------------------------------------------------

    def _refresh_swatch(self):
        h = (self._sliders["h_lo"].value + self._sliders["h_hi"].value) / 2
        s = (self._sliders["s_lo"].value + self._sliders["s_hi"].value) / 2
        v = (self._sliders["v_lo"].value + self._sliders["v_hi"].value) / 2
        self._swatch.set_hsv(h, s, v)

    def get_values(self) -> dict:
        return {key: int(sl.value) for key, sl in self._sliders.items()}

    def set_values(self, values: dict):
        for key, sl in self._sliders.items():
            if key in values:
                sl.value = float(values[key])
        self._refresh_swatch()


# ---------------------------------------------------------------------------
# HSVTunerScreen
# ---------------------------------------------------------------------------

class HSVTunerScreen(Screen):
    """
    Full-screen settings panel embedded in the app's ScreenManager.

    Public API
    ----------
    get_prefs() → dict  : serialisable prefs dict for AnalyzerConfig.from_prefs()
    set_prefs(dict)     : populate sliders from a previously saved prefs dict
    """

    _BALL_DEFAULTS   = {"h_lo": 0,   "s_lo": 0,   "v_lo": 200,
                        "h_hi": 180, "s_hi": 40,  "v_hi": 255}
    _PLAYER_DEFAULTS = {"h_lo": 100, "s_lo": 150, "v_lo": 100,
                        "h_hi": 130, "s_hi": 255, "v_hi": 255}

    def __init__(self, app_ref, **kwargs):
        super().__init__(name="hsv_tuner", **kwargs)
        self._app = app_ref
        self._build_ui()

    # ------------------------------------------------------------------
    # UI build
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(8))

        # ---- Header ----
        header = BoxLayout(size_hint=(1, None), height=dp(46), spacing=dp(8))
        back = Button(text="< Back", size_hint=(None, 1), width=dp(80),
                      background_color=(0.3, 0.3, 0.3, 1))
        back.bind(on_release=lambda *_: setattr(self.manager, "current", "home"))
        header.add_widget(back)
        header.add_widget(Label(
            text="[b]HSV Colour Tuner[/b]", markup=True, font_size="17sp"))
        root.add_widget(header)

        # ---- Scrollable sliders ----
        scroll = ScrollView(size_hint=(1, 1))
        inner  = BoxLayout(orientation="vertical", spacing=dp(14),
                           size_hint=(1, None), padding=(0, dp(4)))
        inner.bind(minimum_height=inner.setter("height"))

        prefs = self._app.hsv_prefs
        self._ball_grp   = HSVGroup("Ball",          prefs.get("ball",   self._BALL_DEFAULTS))
        self._player_grp = HSVGroup("Active Player", prefs.get("player", self._PLAYER_DEFAULTS))

        inner.add_widget(self._ball_grp)
        inner.add_widget(_Divider())
        inner.add_widget(self._player_grp)

        scroll.add_widget(inner)
        root.add_widget(scroll)

        # ---- Action buttons ----
        btn_row = BoxLayout(size_hint=(1, None), height=dp(50), spacing=dp(10))

        reset = Button(text="Reset Defaults",
                       background_color=(0.38, 0.38, 0.38, 1))
        reset.bind(on_release=self._reset)

        save = Button(text="Save & Apply",
                      background_color=(0.18, 0.68, 0.28, 1))
        save.bind(on_release=self._save)

        btn_row.add_widget(reset)
        btn_row.add_widget(save)
        root.add_widget(btn_row)

        self.add_widget(root)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_prefs(self) -> dict:
        return {
            "ball":   self._ball_grp.get_values(),
            "player": self._player_grp.get_values(),
        }

    def set_prefs(self, prefs: dict):
        if "ball"   in prefs:
            self._ball_grp.set_values(prefs["ball"])
        if "player" in prefs:
            self._player_grp.set_values(prefs["player"])

    # ------------------------------------------------------------------
    # Button handlers
    # ------------------------------------------------------------------

    def _save(self, *_):
        self._app.save_hsv_prefs(self.get_prefs())
        self.manager.current = "home"

    def _reset(self, *_):
        self._ball_grp.set_values(self._BALL_DEFAULTS)
        self._player_grp.set_values(self._PLAYER_DEFAULTS)


# ---------------------------------------------------------------------------
# Divider helper
# ---------------------------------------------------------------------------

class _Divider(Widget):
    """1 dp horizontal rule."""
    def __init__(self, **kwargs):
        super().__init__(size_hint=(1, None), height=dp(1), **kwargs)
        self.bind(pos=self._draw, size=self._draw)

    def _draw(self, *_):
        self.canvas.clear()
        with self.canvas:
            Color(0.35, 0.35, 0.35, 1)
            Rectangle(pos=self.pos, size=self.size)
