"""Generates simple cartoon-style illustrations for the training scenario's
deliberate "signal" events (the recurring grey van, the person in dark
clothing, the two sabotage signs, the two armed sightings) -- standing in
for a phone photo a guard might attach when filing one of these reports.
Deliberately flat/iconographic, not photorealistic: this is synthetic
training material, and the armed-sighting images in particular are drawn
as plain dark silhouettes, the same way a safety poster would depict one,
not as a realistic weapon.

Pure Pillow drawing, no external assets or network calls -- consistent
with the rest of this project being local-only. Re-run to regenerate:

    python demo/generate_training_images.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT_DIR = Path(__file__).resolve().parent / "training_days" / "images"
SIZE = (480, 320)

DAY_SKY_TOP = (176, 219, 245)
DAY_SKY_BOTTOM = (219, 240, 250)
DUSK_SKY_TOP = (32, 40, 66)
DUSK_SKY_BOTTOM = (70, 82, 110)
DAY_GROUND = (146, 199, 118)
DUSK_GROUND = (46, 58, 48)
ROAD = (162, 162, 168)
SILHOUETTE = (26, 31, 43)


def _vertical_gradient(draw: ImageDraw.ImageDraw, w: int, h: int, top: tuple, bottom: tuple) -> None:
    for y in range(h):
        t = y / max(h - 1, 1)
        color = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        draw.line([(0, y), (w, y)], fill=color)


def _base_scene(sky_top, sky_bottom, ground_color, horizon: int = 210) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", SIZE, sky_top)
    draw = ImageDraw.Draw(img)
    _vertical_gradient(draw, SIZE[0], horizon, sky_top, sky_bottom)
    draw.rectangle([0, horizon, SIZE[0], SIZE[1]], fill=ground_color)
    return img, draw


def _cloud(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int) -> None:
    color = (255, 255, 255)
    for dx, dy, rr in [(-r, 0, r * 0.7), (0, -r * 0.3, r), (r, 0, r * 0.7)]:
        draw.ellipse([cx + dx - rr, cy + dy - rr, cx + dx + rr, cy + dy + rr], fill=color)


def _tree(draw: ImageDraw.ImageDraw, x: int, base_y: int, scale: float = 1.0, dusk: bool = False) -> None:
    trunk_color = (90, 60, 40) if not dusk else (40, 30, 24)
    leaf_color = (60, 120, 60) if not dusk else (24, 40, 28)
    trunk_h = int(30 * scale)
    draw.rectangle([x - 4 * scale, base_y - trunk_h, x + 4 * scale, base_y], fill=trunk_color)
    r = 26 * scale
    for dx, dy in [(-10, -6), (10, -6), (0, -22)]:
        draw.ellipse(
            [x + dx * scale - r, base_y - trunk_h + dy * scale - r,
             x + dx * scale + r, base_y - trunk_h + dy * scale + r],
            fill=leaf_color,
        )


def _fence(
    draw: ImageDraw.ImageDraw, x0: int, x1: int, top_y: int, bottom_y: int,
    gap: tuple | None = None, behind_color: tuple = DAY_GROUND,
) -> None:
    post_color = (110, 90, 70)
    wire_color = (150, 150, 155)
    for x in range(x0, x1 + 1, 60):
        draw.rectangle([x - 3, top_y, x + 3, bottom_y], fill=post_color)
    diamond = 16
    y = top_y
    row = 0
    while y < bottom_y:
        offset = diamond if row % 2 else 0
        x = x0 + offset
        while x < x1:
            if gap and gap[0] <= x <= gap[1] and gap[2] <= y <= gap[3]:
                x += diamond * 2
                continue
            draw.line([(x, y), (x + diamond, y - diamond)], fill=wire_color, width=1)
            draw.line([(x, y), (x + diamond, y + diamond)], fill=wire_color, width=1)
            x += diamond * 2
        y += diamond
        row += 1
    if gap:
        gx0, gx1, gy0, gy1 = gap
        # the hole shows whatever's behind the fence (sky/grass), not a
        # solid patch, so it actually reads as an opening rather than a
        # dark blob
        draw.rectangle([gx0, gy0, gx1, gy1], fill=behind_color)
        # bent, snipped wire ends curling into the opening from several
        # directions, the clearest visual cue that this was cut open
        cut_points = [
            ((gx0, gy0), (gx0 + 14, gy0 + 18)),
            ((gx1, gy0), (gx1 - 12, gy0 + 20)),
            ((gx0 + 6, gy1), (gx0 + 22, gy1 - 16)),
            ((gx1 - 4, gy1), (gx1 - 20, gy1 - 14)),
            ((gx0 + (gx1 - gx0) // 2, gy0), (gx0 + (gx1 - gx0) // 2 + 8, gy0 + 22)),
        ]
        for start, end in cut_points:
            draw.line([start, end], fill=wire_color, width=2)


def _van(draw: ImageDraw.ImageDraw, x: int, y: int, scale: float, plate: str) -> None:
    body = (150, 156, 165)
    body_dark = (120, 126, 136)
    window = (70, 90, 105)
    w, h = int(150 * scale), int(58 * scale)
    draw.rounded_rectangle([x, y - h, x + w, y], radius=10, fill=body, outline=body_dark, width=2)
    cab_w = int(w * 0.32)
    draw.rounded_rectangle([x + w - cab_w, y - h - int(20 * scale), x + w, y - h + 6], radius=8, fill=body)
    draw.rounded_rectangle(
        [x + w - cab_w + 6, y - h - int(16 * scale), x + w - 8, y - h - 6], radius=4, fill=window,
    )
    for wx in (x + int(w * 0.15), x + int(w * 0.45)):
        draw.rectangle([wx, y - h + 10, wx + int(w * 0.18), y - h + int(h * 0.5)], fill=body_dark)
    wheel_r = int(14 * scale)
    for wx in (x + int(w * 0.22), x + int(w * 0.78)):
        draw.ellipse([wx - wheel_r, y - wheel_r, wx + wheel_r, y + wheel_r], fill=(30, 30, 32))
        draw.ellipse([wx - wheel_r // 2, y - wheel_r // 2, wx + wheel_r // 2, y + wheel_r // 2], fill=(90, 90, 92))
    plate_w, plate_h = int(46 * scale), int(16 * scale)
    px, py = x + int(w * 0.35), y - int(4 * scale)
    draw.rectangle([px, py, px + plate_w, py + plate_h], fill=(235, 225, 60), outline=(40, 40, 40))


def _person(draw: ImageDraw.ImageDraw, x: int, base_y: int, scale: float = 1.0, armed: bool = False) -> None:
    head_r = int(12 * scale)
    draw.ellipse([x - head_r, base_y - int(95 * scale) - head_r, x + head_r, base_y - int(95 * scale) + head_r],
                 fill=SILHOUETTE)
    # cap brim
    draw.ellipse([x - head_r - 2, base_y - int(95 * scale) - head_r // 2,
                  x + head_r + 4, base_y - int(95 * scale) + head_r // 2], fill=SILHOUETTE)
    body_top = base_y - int(88 * scale)
    body_w = int(24 * scale)
    draw.polygon(
        [(x - body_w, base_y - int(20 * scale)), (x + body_w, base_y - int(20 * scale)),
         (x + body_w - 6, body_top), (x - body_w + 6, body_top)],
        fill=SILHOUETTE,
    )
    # backpack hint
    draw.rounded_rectangle(
        [x - body_w - 6, body_top + int(10 * scale), x - body_w + 8, base_y - int(30 * scale)],
        radius=4, fill=SILHOUETTE,
    )
    leg_w = int(9 * scale)
    for dx in (-leg_w, leg_w // 2):
        draw.rectangle([x + dx, base_y - int(20 * scale), x + dx + leg_w, base_y], fill=SILHOUETTE)
    if armed:
        draw.line(
            [(x + body_w - 4, body_top + int(6 * scale)), (x + body_w + int(34 * scale), body_top - int(14 * scale))],
            fill=SILHOUETTE, width=max(3, int(4 * scale)),
        )


def _damaged_lock(draw: ImageDraw.ImageDraw) -> None:
    door_color = (96, 68, 46)
    frame_color = (60, 42, 30)
    draw.rectangle([60, 20, 420, 300], fill=door_color, outline=frame_color, width=6)
    draw.rectangle([0, 20, 60, 300], fill=frame_color)
    # plank lines on the door for a bit of texture/context
    for x in range(90, 420, 60):
        draw.line([(x, 26), (x, 294)], fill=frame_color, width=2)

    hasp_x, hasp_y = 250, 160
    # hasp plate, screwed to the door
    draw.rectangle([hasp_x - 50, hasp_y - 14, hasp_x + 16, hasp_y + 46], fill=(150, 150, 155),
                    outline=(90, 90, 95), width=3)
    # padlock body still hanging, but the shackle is bent open/torn free on
    # one side -- the clearest "forced entry" cue, rather than an intact lock
    draw.rounded_rectangle([hasp_x - 20, hasp_y + 10, hasp_x + 30, hasp_y + 50], radius=6,
                            fill=(190, 160, 70), outline=(90, 70, 20), width=3)
    draw.arc([hasp_x - 16, hasp_y - 26, hasp_x + 26, hasp_y + 18], start=180, end=360,
              fill=(215, 215, 220), width=6)
    # the torn-open end of the shackle, flung outward and up
    draw.line([(hasp_x + 24, hasp_y - 6), (hasp_x + 60, hasp_y - 46)], fill=(215, 215, 220), width=6)

    # a few crowbar gouges in the door beside the hasp, not overlapping it
    for i in range(3):
        sx = hasp_x - 130 + i * 22
        draw.line([(sx, hasp_y - 50), (sx + 34, hasp_y + 10)], fill=(60, 42, 30), width=4)


def _car(draw: ImageDraw.ImageDraw, x: int, y: int, scale: float) -> None:
    """A low sedan, deliberately distinct in shape and color from _van's
    grey box-van (that one is a specific recurring plot vehicle; this is
    just whatever the camera sensor happened to catch passing by)."""
    body = (150, 60, 55)
    body_dark = (115, 40, 38)
    window = (70, 90, 105)
    w, h = int(130 * scale), int(34 * scale)
    draw.rounded_rectangle([x, y - h, x + w, y], radius=8, fill=body, outline=body_dark, width=2)
    cabin_w = int(w * 0.55)
    cabin_x = x + int(w * 0.22)
    draw.rounded_rectangle(
        [cabin_x, y - h - int(22 * scale), cabin_x + cabin_w, y - h + 6], radius=10, fill=body,
    )
    draw.rounded_rectangle(
        [cabin_x + 8, y - h - int(18 * scale), cabin_x + cabin_w - 8, y - h - 4], radius=4, fill=window,
    )
    wheel_r = int(13 * scale)
    for wx in (x + int(w * 0.2), x + int(w * 0.8)):
        draw.ellipse([wx - wheel_r, y - wheel_r, wx + wheel_r, y + wheel_r], fill=(30, 30, 32))
        draw.ellipse([wx - wheel_r // 2, y - wheel_r // 2, wx + wheel_r // 2, y + wheel_r // 2], fill=(90, 90, 92))
    draw.ellipse([x + int(w * 0.85), y - h + int(6 * scale), x + w, y - h + int(14 * scale)], fill=(240, 220, 150))


def _pedestrian(draw: ImageDraw.ImageDraw, x: int, base_y: int, scale: float = 1.0) -> None:
    """An ordinary daytime passerby in plain colored clothing -- unlike
    _person (a stark dark silhouette used for the story's dark-clothed
    figure), this one is meant to read as unremarkable, since a camera
    sensor catching a random pedestrian isn't itself a "signal" event."""
    skin = (225, 185, 150)
    jacket = (60, 95, 150)
    pants = (70, 70, 80)
    head_r = int(11 * scale)
    head_y = base_y - int(92 * scale)
    draw.ellipse([x - head_r, head_y - head_r, x + head_r, head_y + head_r], fill=skin)
    body_top = head_y + head_r
    body_w = int(20 * scale)
    draw.rounded_rectangle(
        [x - body_w, body_top, x + body_w, base_y - int(22 * scale)], radius=6, fill=jacket,
    )
    leg_w = int(8 * scale)
    for dx in (-leg_w, leg_w // 2):
        draw.rectangle([x + dx, base_y - int(22 * scale), x + dx + leg_w, base_y], fill=pants)


def _deer(draw: ImageDraw.ImageDraw, x: int, base_y: int, scale: float = 1.0) -> None:
    """Side-on view, facing right: body -> neck -> head all overlap
    generously so the joints don't read as separate floating shapes."""
    body_color = (150, 110, 75)
    dark = (100, 70, 50)
    body_w, body_h = int(55 * scale), int(26 * scale)
    body_cy = base_y - int(50 * scale)
    body_box = [x - body_w, body_cy - body_h, x + body_w, body_cy + body_h]
    draw.ellipse(body_box, fill=body_color)

    neck_base = (x + int(body_w * 0.6), body_cy - int(body_h * 0.3))
    head_cx, head_cy = x + int(body_w * 0.95), body_cy - int(65 * scale)
    draw.polygon(
        [(neck_base[0] - int(10 * scale), neck_base[1] + int(14 * scale)),
         (neck_base[0] + int(18 * scale), neck_base[1] - int(6 * scale)),
         (head_cx + int(4 * scale), head_cy + int(10 * scale)),
         (head_cx - int(14 * scale), head_cy + int(16 * scale))],
        fill=body_color,
    )
    head_r = int(15 * scale)
    draw.ellipse([head_cx - head_r, head_cy - head_r, head_cx + head_r, head_cy + head_r], fill=body_color)
    # snout, so the head end is clearly the front, not just a round blob
    draw.ellipse(
        [head_cx + int(6 * scale), head_cy, head_cx + head_r + int(10 * scale), head_cy + int(10 * scale)],
        fill=body_color,
    )
    # ears
    for side in (-1, 1):
        ex = head_cx + side * int(6 * scale)
        draw.polygon(
            [(ex, head_cy - head_r + 2), (ex + side * int(10 * scale), head_cy - head_r - int(14 * scale)),
             (ex + side * int(4 * scale), head_cy - head_r + 4)],
            fill=body_color,
        )
    # antlers, rooted at the top of the head
    for side in (-1, 1):
        root = (head_cx + side * int(4 * scale), head_cy - head_r + int(4 * scale))
        tip = (root[0] + side * int(14 * scale), root[1] - int(26 * scale))
        draw.line([root, tip], fill=dark, width=max(2, int(3 * scale)))
        draw.line([tip, (tip[0] + side * int(6 * scale), tip[1] + int(6 * scale))], fill=dark, width=2)
    leg_w = int(7 * scale)
    for dx in (-body_w + int(8 * scale), -int(body_w * 0.15), int(body_w * 0.25), body_w - int(16 * scale)):
        draw.rectangle([x + dx, body_cy + int(body_h * 0.6), x + dx + leg_w, base_y], fill=dark)
    # small tail at the back
    draw.ellipse(
        [x - body_w - int(6 * scale), body_cy - int(6 * scale), x - body_w + int(8 * scale), body_cy + int(8 * scale)],
        fill=(230, 225, 210),
    )


def make_van() -> Image.Image:
    img, draw = _base_scene(DAY_SKY_TOP, DAY_SKY_BOTTOM, DAY_GROUND)
    _cloud(draw, 100, 50, 22)
    _cloud(draw, 360, 35, 16)
    draw.rectangle([0, 205, SIZE[0], 260], fill=ROAD)
    for x in range(-20, SIZE[0], 50):
        draw.rectangle([x, 230, x + 26, 236], fill=(230, 230, 210))
    _tree(draw, 40, 210, 1.1)
    _tree(draw, 440, 205, 0.9)
    _van(draw, 150, 235, 1.5, "QAB456")
    return img


def make_dark_figure() -> Image.Image:
    img, draw = _base_scene(DUSK_SKY_TOP, DUSK_SKY_BOTTOM, DUSK_GROUND, horizon=230)
    _fence(draw, 20, 460, 150, 230)
    _tree(draw, 60, 235, 0.8, dusk=True)
    _tree(draw, 430, 232, 0.7, dusk=True)
    _person(draw, 240, 250, 1.5, armed=False)
    return img


def make_cut_fence() -> Image.Image:
    img, draw = _base_scene(DAY_SKY_TOP, DAY_SKY_BOTTOM, DAY_GROUND, horizon=190)
    _fence(draw, 10, 470, 90, 220, gap=(200, 270, 196, 220), behind_color=DAY_GROUND)
    return img


def make_armed_single() -> Image.Image:
    img, draw = _base_scene(DUSK_SKY_TOP, DUSK_SKY_BOTTOM, DUSK_GROUND, horizon=230)
    _tree(draw, 70, 236, 1.2, dusk=True)
    _tree(draw, 400, 232, 1.0, dusk=True)
    _tree(draw, 330, 238, 0.8, dusk=True)
    _person(draw, 240, 255, 1.6, armed=True)
    return img


def make_armed_pair() -> Image.Image:
    img, draw = _base_scene(DUSK_SKY_TOP, DUSK_SKY_BOTTOM, DUSK_GROUND, horizon=230)
    _tree(draw, 55, 236, 1.0, dusk=True)
    _tree(draw, 425, 232, 0.9, dusk=True)
    _person(draw, 150, 258, 1.3, armed=False)
    _person(draw, 320, 255, 1.3, armed=True)
    return img


def make_broken_lock() -> Image.Image:
    img = Image.new("RGB", SIZE, (30, 30, 32))
    draw = ImageDraw.Draw(img)
    _damaged_lock(draw)
    return img


def make_camera_car() -> Image.Image:
    img, draw = _base_scene(DAY_SKY_TOP, DAY_SKY_BOTTOM, DAY_GROUND)
    _cloud(draw, 90, 45, 18)
    _cloud(draw, 380, 55, 20)
    draw.rectangle([0, 205, SIZE[0], 260], fill=ROAD)
    for x in range(-20, SIZE[0], 50):
        draw.rectangle([x, 230, x + 26, 236], fill=(230, 230, 210))
    _tree(draw, 50, 208, 1.0)
    _tree(draw, 420, 206, 0.8)
    _car(draw, 170, 235, 1.4)
    return img


def make_camera_person() -> Image.Image:
    img, draw = _base_scene(DAY_SKY_TOP, DAY_SKY_BOTTOM, DAY_GROUND, horizon=220)
    _cloud(draw, 110, 40, 16)
    _fence(draw, 10, 470, 160, 220)
    _tree(draw, 50, 222, 0.9)
    _tree(draw, 430, 218, 0.8)
    _pedestrian(draw, 240, 245, 1.5)
    return img


def make_camera_deer() -> Image.Image:
    img, draw = _base_scene(DAY_SKY_TOP, DAY_SKY_BOTTOM, DAY_GROUND, horizon=225)
    _cloud(draw, 380, 40, 18)
    _tree(draw, 60, 228, 1.1)
    _tree(draw, 400, 224, 0.9)
    _tree(draw, 350, 232, 0.7)
    _deer(draw, 230, 248, 1.6)
    return img


IMAGES = {
    "van.png": make_van,
    "dark_figure.png": make_dark_figure,
    "cut_fence.png": make_cut_fence,
    "armed_single.png": make_armed_single,
    "armed_pair.png": make_armed_pair,
    "broken_lock.png": make_broken_lock,
    "camera_car.png": make_camera_car,
    "camera_person.png": make_camera_person,
    "camera_deer.png": make_camera_deer,
}


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for filename, builder in IMAGES.items():
        path = OUT_DIR / filename
        builder().save(path, format="PNG")
        print(f"Wrote {path}")


if __name__ == "__main__":
    main()
