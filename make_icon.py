# -*- coding: utf-8 -*-
"""
生成应用图标 app_icon.ico

设计：蓝色渐变圆角方块（呼应程序主题色 #0078d4），
     白色上行/下行双箭头表示文件传输。
高分辨率绘制后缩小抗锯齿，输出 16-256 全尺寸 ico。

用法：python make_icon.py
"""

import os

from PIL import Image, ImageDraw

S = 2048  # 超采样画布尺寸，最终缩小到 256 抗锯齿


def lerp_color(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def rounded_gradient(size, radius, top, bottom):
    """带垂直渐变的圆角方块"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    grad = Image.new("RGBA", (size, size))
    gd = ImageDraw.Draw(grad)
    for y in range(size):
        gd.line([(0, y), (size, y)], fill=lerp_color(top, bottom, y / size))

    mask = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    img.paste(grad, (0, 0), mask)
    return img


def draw_arrow(draw, cx, top, bottom, pointing_up, color):
    """画一个箭头：圆角矩形杆 + 三角头"""
    shaft_w = 190
    head_w = 470
    head_h = 420

    if pointing_up:
        head_top = top
        shaft_top = top + head_h - 30
        shaft_bottom = bottom
        head = [(cx, head_top), (cx + head_w // 2, head_top + head_h), (cx - head_w // 2, head_top + head_h)]
    else:
        head_bottom = bottom
        shaft_top = top
        shaft_bottom = bottom - head_h + 30
        head = [(cx, head_bottom), (cx + head_w // 2, head_bottom - head_h), (cx - head_w // 2, head_bottom - head_h)]

    # 杆（圆角矩形）
    draw.rounded_rectangle(
        [cx - shaft_w // 2, shaft_top, cx + shaft_w // 2, shaft_bottom],
        radius=60,
        fill=color,
    )
    # 头（三角）
    draw.polygon(head, fill=color)


def make_icon():
    top_color = (64, 148, 238)    # #4094EE
    bottom_color = (23, 92, 197)  # #175CC5

    img = rounded_gradient(S, radius=int(S * 0.22), top=top_color, bottom=bottom_color)

    # 顶部高光弧线（增加一点立体感，裁剪进圆角内）
    highlight = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(highlight).ellipse(
        [-S * 0.35, -S * 0.62, S * 1.35, S * 0.62],
        outline=(255, 255, 255, 46),
        width=int(S * 0.016),
    )
    clip = Image.new("L", (S, S), 0)
    ImageDraw.Draw(clip).rounded_rectangle(
        [0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=255
    )
    highlight.putalpha(
        Image.composite(highlight.split()[3], Image.new("L", (S, S), 0), clip)
    )
    img.alpha_composite(highlight)
    draw = ImageDraw.Draw(img)

    # 双箭头：上箭头（左）+ 下箭头（右），轻微投影
    arrow_top = int(S * 0.235)
    arrow_bottom = int(S * 0.765)
    left_cx = int(S * 0.365)
    right_cx = int(S * 0.635)

    for cx, up in ((left_cx, True), (right_cx, False)):
        # 投影
        shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shadow)
        draw_arrow(sd, cx, arrow_top + 26, arrow_bottom + 26, up, (10, 40, 90, 90))
        img.alpha_composite(shadow)

    for cx, up in ((left_cx, True), (right_cx, False)):
        draw_arrow(draw, cx, arrow_top, arrow_bottom, up, (255, 255, 255, 255))

    # 缩小抗锯齿
    base = img.resize((256, 256), Image.LANCZOS)

    out_ico = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_icon.ico")
    base.save(
        out_ico,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )

    out_png = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out", "icon_preview.png")
    base.save(out_png)

    print(f"已生成: {out_ico}")
    print(f"预览图: {out_png}")


if __name__ == "__main__":
    make_icon()
