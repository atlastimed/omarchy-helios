"""Draw the preview's scene: the name in block letters, the demo day's heart
rate as a labelled graph along the horizon, and the sun rising where that
graph reaches 6 AM.

    python3 docs/preview/scene.py docs/preview/state.json > docs/preview/scene.svg

The SVG is the size of the preview (1280x720) and transparent elsewhere;
preview.html lays the text, the excerpts and the captures over it.
"""

import itertools
import json
import sys
from datetime import datetime
from pathlib import Path

WIDTH, HEIGHT = 1280, 720
HORIZON = 462  # the graph's baseline
GRAPH_END = 824  # the last reading: just short of the panel
FLOOR = 50  # bpm on the baseline
RISE = 0.45  # pixels a beat
SUN_RADIUS = 168
SUN_LIFT = 26  # how far the sun's centre is above the horizon
MONO = "JetBrainsMono Nerd Font, JetBrains Mono, monospace"
LABEL = f'font-family="{MONO}" font-size="9.5" font-weight="700" letter-spacing="1.1"'


# The name, in the block letters Omarchy draws its own in: built from full and
# half blocks on a terminal's grid. O and H are as in Omarchy's logo; E, L, I
# and S are drawn to the same nine-column pattern. Each letter is its rows,
# top to bottom; a space is an empty cell.
LETTERS = {
    "H": [
        "  ▄█   █▄  ",
        " ███   ███ ",
        " ███   ███ ",
        "▄███▄▄▄███▄",
        "▀███▀▀▀███▀",
        " ███   ███ ",
        " ███   ███ ",
        " ███   █▀  ",
    ],
    "E": [
        "  ▄███████",
        " ███   ███",
        " ███   █▀ ",
        "▄███▄▄▄   ",
        "▀███▀▀▀   ",
        " ███   █▄ ",
        " ███   ███",
        " █████████",
    ],
    "L": [
        " ▄█      ",
        "███      ",
        "███      ",
        "███      ",
        "███      ",
        "███      ",
        "███▌   ▄█",
        "█████████",
    ],
    "I": [
        " ▄█ ",
        "███ ",
        "███▌",
        "███▌",
        "███▌",
        "███ ",
        "███ ",
        "█▀  ",
    ],
    "O": [
        " ▄█████▄ ",
        "███   ███",
        "███   ███",
        "███   ███",
        "███   ███",
        "███   ███",
        "███   ███",
        " ▀█████▀ ",
    ],
    "S": [
        "  ▄███████",
        " ███   ███",
        " ███   █▀ ",
        " ███      ",
        "▀█████████",
        "       ███",
        "  ▄█   ███",
        "▄███████▀ ",
    ],
}
CELL_W, CELL_H = 7, 14  # a terminal cell is about twice as tall as wide
NAME_AT = (40, 66)
# Which part of its cell each block character fills: x, y, width, height in halves.
BLOCKS = {"█": (0, 0, 2, 2), "▄": (0, 1, 2, 1), "▀": (0, 0, 2, 1), "▌": (0, 0, 1, 2), "▐": (1, 0, 1, 2)}


def block_name(word: str) -> list[str]:
    """`word` as SVG rectangles, letters two cells apart."""
    left, top = NAME_AT
    rows = len(next(iter(LETTERS.values())))
    out = ['<g fill="url(#name)" shape-rendering="crispEdges">']
    column = 0
    for letter in word:
        glyph = LETTERS[letter]
        for row, cells in enumerate(glyph):
            # Runs of the same block become one rectangle, so no seams show between cells.
            for kind, run in itertools.groupby(enumerate(cells), key=lambda cell: cell[1]):
                if kind not in BLOCKS:
                    continue
                run = list(run)
                x, y, w, h = BLOCKS[kind]
                if w == 2:
                    out.append(
                        f'<rect x="{left + (column + run[0][0]) * CELL_W}" y="{top + row * CELL_H + y * CELL_H // 2}" '
                        f'width="{len(run) * CELL_W}" height="{h * CELL_H // 2}"/>'
                    )
                else:
                    for index, _ in run:
                        out.append(
                            f'<rect x="{left + (column + index) * CELL_W + x * CELL_W / 2}" y="{top + row * CELL_H}" '
                            f'width="{CELL_W / 2}" height="{CELL_H}"/>'
                        )
        column += len(glyph[0]) + 2
    out.append("</g>")
    assert all(len(g) == rows and len({len(r) for r in g}) == 1 for g in LETTERS.values())
    return out


def hour_label(at: datetime) -> str:
    return at.strftime("%I %p").lstrip("0")


def main() -> None:
    day = json.loads(Path(sys.argv[1]).read_text())["connected"]["day"]
    start, end = day["start"], day["end"]

    def x_at(ts: float) -> float:
        return (ts - start) / (end - start) * GRAPH_END

    spots = [(x_at(at), HORIZON - max(0.0, mean - FLOOR) * RISE) for at, mean, _low, _high in day["heartRate"]]
    # Rounded: each reading pulls the line towards it, and the line passes
    # through the midpoints between neighbours.
    line = [f"M{spots[0][0]:.1f},{spots[0][1]:.1f}"]
    for (x, y), (nx, ny) in itertools.pairwise(spots[1:]):
        line.append(f"Q{x:.1f},{y:.1f} {(x + nx) / 2:.1f},{(y + ny) / 2:.1f}")
    last_x, last_y = spots[-1]
    line.append(f"L{last_x:.1f},{last_y:.1f}")
    path = " ".join(line)

    # Every six hours on the clock, and the sunrise among them.
    ticks = []
    at = datetime.fromtimestamp(start).replace(minute=0, second=0, microsecond=0)
    hours = itertools.count(1)
    while (stamp := at.timestamp() + 3600 * next(hours)) < end - 5400:
        when = datetime.fromtimestamp(stamp)
        if when.hour % 6 == 0:
            ticks.append((x_at(stamp), hour_label(when), when.hour))
    # The layout in preview.html leaves the sun room between the text and
    # the panel, which is where 6 AM falls for a state made around midday.
    sunrise = [x for x, _label, hour in ticks if hour == 6]
    if not sunrise or not 540 <= sunrise[0] <= 660:
        sys.exit("scene.py: make the demo state between about 11:00 and 14:00, so 6 AM falls where the sun fits")
    sun_x = sunrise[0]
    sun_y = HORIZON - SUN_LIFT

    peak = max(day["heartRate"], key=lambda row: row[1])
    peak_x, peak_y = x_at(peak[0]), HORIZON - (peak[1] - FLOOR) * RISE

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}">',
        "<defs>",
        '<linearGradient id="sun" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0" stop-color="#ffe2a6"/><stop offset="0.55" stop-color="#ffa868"/>'
        '<stop offset="1" stop-color="#f7768e"/></linearGradient>',
        '<radialGradient id="halo"><stop offset="0.42" stop-color="#ffba82" stop-opacity="0.5"/>'
        '<stop offset="0.6" stop-color="#fa8282" stop-opacity="0.17"/>'
        '<stop offset="1" stop-color="#fa8282" stop-opacity="0"/></radialGradient>',
        '<linearGradient id="land" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0" stop-color="#151624"/><stop offset="0.4" stop-color="#0f1018"/>'
        '<stop offset="1" stop-color="#0c0d14"/></linearGradient>',
        # The line is lit where it passes the sun.
        f'<linearGradient id="lit" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="{GRAPH_END}" y2="0">'
        '<stop offset="0" stop-color="#7aa2f7"/><stop offset="0.5" stop-color="#8fa4f2"/>'
        f'<stop offset="{sun_x / GRAPH_END - 0.13:.3f}" stop-color="#d9a6d6"/>'
        f'<stop offset="{sun_x / GRAPH_END:.3f}" stop-color="#ffd9a0"/>'
        f'<stop offset="{min(1.0, sun_x / GRAPH_END + 0.13):.3f}" stop-color="#d9a6d6"/>'
        '<stop offset="1" stop-color="#8fa4f2"/></linearGradient>',
        '<filter id="glow" x="-5%" y="-80%" width="110%" height="260%"><feGaussianBlur stdDeviation="4"/></filter>',
        # The name takes a little of the sun's colour towards its foot.
        f'<linearGradient id="name" gradientUnits="userSpaceOnUse" x1="0" y1="{NAME_AT[1]}" x2="0" '
        f'y2="{NAME_AT[1] + 8 * CELL_H}"><stop offset="0.35" stop-color="#fff4de"/>'
        '<stop offset="1" stop-color="#ffd3a1"/></linearGradient>',
        "</defs>",
        f'<circle cx="{sun_x:.1f}" cy="{sun_y}" r="{SUN_RADIUS * 2.4:.0f}" fill="url(#halo)"/>',
        f'<circle cx="{sun_x:.1f}" cy="{sun_y}" r="{SUN_RADIUS}" fill="url(#sun)"/>',
        f'<path d="{path} L{WIDTH + 10},{last_y:.1f} L{WIDTH + 10},{HEIGHT} L-10,{HEIGHT} Z" fill="url(#land)"/>',
        f'<path d="{path}" fill="none" stroke="url(#lit)" stroke-width="5" opacity="0.45" filter="url(#glow)"/>',
        f'<path d="{path}" fill="none" stroke="url(#lit)" stroke-width="2" '
        'stroke-linejoin="round" stroke-linecap="round"/>',
        # Now: where the line ends, as the panel's own chart marks it.
        f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="4.2" fill="#8fa4f2" stroke="#e6e9fb" stroke-width="1.2"/>',
        f'<circle cx="{peak_x:.1f}" cy="{peak_y:.1f}" r="2.6" fill="#e6e9fb"/>',
        f'<text x="{peak_x:.1f}" y="{peak_y - 11:.1f}" text-anchor="middle" fill="#e6e9fb" {LABEL}>'
        f"{peak[1]:.0f} BPM</text>",
        f'<text x="40" y="{HORIZON - 30}" fill="#8b93b8" {LABEL}>HEART RATE · 24 H</text>',
    ]
    for x, label, _hour in ticks:
        out.append(f'<rect x="{x - 0.5:.1f}" y="{HORIZON + 5}" width="1" height="4" fill="#5d6488"/>')
        out.append(f'<text x="{x:.1f}" y="{HORIZON + 21}" text-anchor="middle" fill="#7b83a8" {LABEL}>{label}</text>')
    out.append(f'<text x="{last_x + 2:.1f}" y="{HORIZON + 21}" text-anchor="end" fill="#aab2d8" {LABEL}>NOW</text>')
    out.extend(block_name("HELIOS"))
    out.append("</svg>")
    print("\n".join(out))


main()
