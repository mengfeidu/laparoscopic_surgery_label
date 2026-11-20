"""Ordered source-frame delivery, either individual images or labeled contact sheets."""
import base64
import io
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .common import AnnotationError


def image_config(layout="individual", detail="high", frames_per_image=16,
                 columns=4, tile_width=320, tile_height=256):
    if layout not in {"individual", "grid"} or detail not in {"high", "low", "auto"}:
        raise AnnotationError("未知送图方式或detail。")
    if not (1 <= frames_per_image <= 64 and 1 <= columns <= 8
            and tile_width >= 160 and tile_height >= 128):
        raise AnnotationError("拼图参数无效。")
    return {"layout": layout, "detail": detail, "frames_per_image": frames_per_image,
            "columns": columns, "tile_width": tile_width, "tile_height": tile_height}


def frame_groups(selected, config):
    size = 1 if config["layout"] == "individual" else config["frames_per_image"]
    return [selected[i:i+size] for i in range(0, len(selected), size)]


def contact_sheet(group, timeline, frame_dir, config):
    columns = min(config["columns"], len(group))
    width, height = config["tile_width"], config["tile_height"]
    sheet = Image.new("RGB", (columns*width, math.ceil(len(group)/columns)*(height+24)), "#101820")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default(size=15)
    except TypeError:
        font = ImageFont.load_default()
    for i, n in enumerate(group):
        with Image.open(Path(frame_dir)/f"{n:09d}.jpg") as im:
            # prepare() placed the source-frame label outside the original image.
            image = im.convert("RGB").crop((0, 28, im.width, im.height))
            image.thumbnail((width, height), Image.Resampling.LANCZOS)
            x, y = (i%columns)*width, (i//columns)*(height+24)
            sheet.paste(image, (x, y+24))
            draw.text((x+4, y+3), f"frame={n} t={timeline['times'][n]:.3f}s", fill="white", font=font)
    buffer = io.BytesIO()
    sheet.save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()


def image_content(selected, timeline, frame_dir, config):
    content = []
    for group in frame_groups(selected, config):
        if config["layout"] == "individual":
            data = (Path(frame_dir)/f"{group[0]:09d}.jpg").read_bytes()
        else:
            data = contact_sheet(group, timeline, frame_dir, config)
        content.append({"type": "text", "text": (
            "source_frames=" + ",".join(map(str, group))
            + ("；拼图按从左到右、从上到下逐格阅读。" if len(group)>1 else "")
        )})
        content.append({"type": "image_url", "image_url": {
            "url": "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii"),
            "detail": config["detail"],
        }})
    return content


def image_manifest(selected, config):
    groups = frame_groups(selected, config)
    return {"source_frame_count": len(selected), "uploaded_image_count": len(groups),
            "image_groups": groups, "image_delivery": config}
