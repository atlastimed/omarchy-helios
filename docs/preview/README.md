# The preview image

`preview.png` at the repository root is what the plugin marketplace shows. It
is composed, not a desktop screenshot, and every reading in it is invented:
no part of it comes from anyone's strap.

| File | What it is |
|---|---|
| `demo_state.py` | Makes a month of invented readings, pushes them through Helios's real store and summary, and writes the state the service would send. |
| `demo_daemon.py` | Serves that state on Helios's socket in place of the service. |
| `state.json` | The demo state the scene is drawn from. |
| `scene.py`, `scene.svg` | The sun, the heart-rate graph along the horizon with its labels, and the name in block letters, drawn from `state.json`. |
| `bar.png`, `panel.png`, `workouts.png`, `trends.png` | Captures of the real bar and panel showing a demo state. |
| `preview.html` | Lays the captures and the text over the scene. |

## Rendering

With the captures in place, two commands rebuild the image:

```bash
python3 docs/preview/scene.py docs/preview/state.json > docs/preview/scene.svg
chromium --headless --hide-scrollbars --force-device-scale-factor=2 --window-size=1280,720 --screenshot=preview.png docs/preview/preview.html
```

The typefaces (Adwaita Sans, JetBrains Mono) are installed with Omarchy.

## Taking the captures again

The captures were taken in a spare Omarchy machine (a VM), never on a desktop
with real readings on it, at a display scale of 2:

1. Make a state around midday, so the graph's 6 AM falls where the layout puts
   the sun: `PYTHONPATH=src uv run python docs/preview/demo_state.py state.json /tmp`.
2. On the spare machine, with the plugin installed and `helios.service`
   stopped, run `python3 demo_daemon.py state.json` and open the panel
   (`omarchy-shell atlastimed.helios open`).
3. Screenshot the screen (`grim`). The panel is taller than the screen: press
   `j` a few times between screenshots and stitch them, taking each row from
   the screenshot where it is furthest from the bottom edge.
4. Crop: the bar (the top 52 pixels) to `bar.png`; the panel with its border,
   as first opened, to `panel.png`; the Workouts and Trends sections of the
   stitched panel to `workouts.png` and `trends.png`.

The state that draws the scene and the state shown in the captures are two
runs of the same generator, made about three quarters of an hour apart: the
graph on the horizon and the chart in the panel show the same invented day,
a little shifted. To have them match exactly, use one state for both.
