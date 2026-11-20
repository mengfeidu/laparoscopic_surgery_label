import bisect
import math
from pathlib import Path
import shutil
import statistics
import subprocess
import tempfile
import json

from PIL import Image, ImageDraw, ImageFont

from .common import AnnotationError, save_json

SAMPLE_HZ = 5


def require_video_tools():
    missing = [name for name in ("ffmpeg", "ffprobe") if not shutil.which(name)]
    if missing:
        raise AnnotationError("缺少 " + "、".join(missing) + "；请先安装 ffmpeg。")


def _command(args):
    result = subprocess.run(args, capture_output=True)
    if result.returncode:
        # No response bodies, environment values, or full command lines are logged.
        raise AnnotationError(f"{Path(args[0]).name} 执行失败，退出码 {result.returncode}。")
    return result.stdout


def inspect_video(path):
    require_video_tools()
    data = json.loads(_command([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_frames",
        "-show_entries",
        "frame=best_effort_timestamp_time,pts_time,pkt_duration_time:"
        "stream=width,height,avg_frame_rate,duration:format=duration",
        "-of", "json", str(path),
    ]))
    frames, streams = data.get("frames", []), data.get("streams", [])
    if not frames or not streams:
        raise AnnotationError("视频没有可解码的视频帧。")
    pts = []
    for row in frames:
        value = row.get("best_effort_timestamp_time", row.get("pts_time"))
        if value is None:
            raise AnnotationError("存在没有时间戳的源帧，不能可靠对齐全片帧号。")
        pts.append(float(value))
    if any(not math.isfinite(t) for t in pts):
        raise AnnotationError("视频时间戳包含非有限值。")
    origin = pts[0]
    times = [t - origin for t in pts]
    deltas = [b-a for a, b in zip(times, times[1:])]
    if any(d <= 0 for d in deltas):
        raise AnnotationError("源帧时间戳重复或逆序，不能可靠使用全片帧号。")
    interval = statistics.median(deltas) if deltas else 0.2
    if interval > 0.2 + 1e-6:
        raise AnnotationError("源视频帧率低于5 Hz，无法提供每秒5张不同的真实帧。")
    last_duration = float(frames[-1].get("pkt_duration_time") or interval)
    if last_duration <= 0 or not math.isfinite(last_duration):
        last_duration = interval
    # Use video-frame duration, not a possibly longer audio-track duration.
    duration = times[-1] + last_duration
    return {
        "source_frame_count": len(times), "duration_seconds": duration,
        "width": streams[0].get("width"), "height": streams[0].get("height"),
        "source_avg_frame_rate": streams[0].get("avg_frame_rate"),
        "timestamp_origin_seconds": origin,
        "frame_numbering": "zero_based_original_video_presentation_frame",
        "end_frame_is_exclusive": True,
        "times": times,
    }


def sample_source_frames(times, duration):
    """First source frame at/after each global 0.2 s grid point; never duplicate."""
    chosen = []
    for sample_index in range(math.ceil(duration * SAMPLE_HZ)):
        frame = bisect.bisect_left(times, sample_index / SAMPLE_HZ - 1e-7)
        if frame < len(times) and (not chosen or chosen[-1] != frame):
            chosen.append(frame)
    return chosen


def frame_at_time(timeline, seconds):
    return bisect.bisect_left(timeline["times"], seconds - 1e-7)


def frame_time(timeline, frame):
    if frame == timeline["source_frame_count"]:
        return timeline["duration_seconds"]
    return timeline["times"][frame]


def make_cores(timeline, start_seconds, end_seconds, window_seconds):
    duration = timeline["duration_seconds"]
    end_seconds = duration if end_seconds is None else end_seconds
    if not (0 <= start_seconds < end_seconds <= duration + 1e-6):
        raise AnnotationError(f"时间范围必须在视频的 [0,{duration:.6f}] 秒内。")
    if window_seconds <= 0:
        raise AnnotationError("核心窗口时长必须大于0。")
    boundaries = [start_seconds]
    while boundaries[-1] + window_seconds < end_seconds - 1e-7:
        boundaries.append(boundaries[-1] + window_seconds)
    boundaries.append(end_seconds)
    cores = []
    for a, b in zip(boundaries, boundaries[1:]):
        first, last = frame_at_time(timeline, a), frame_at_time(timeline, b)
        if first < last:
            cores.append({"start_frame": first, "end_frame": last,
                          "start_seconds": frame_time(timeline, first),
                          "end_seconds": frame_time(timeline, last)})
    if not cores:
        raise AnnotationError("请求的区间内没有视频帧。")
    return cores


def with_context(core, timeline, sampled_frames, context_seconds):
    left = max(0.0, core["start_seconds"] - context_seconds)
    right = min(timeline["duration_seconds"], core["end_seconds"] + context_seconds)
    times = timeline["times"]
    selected = [n for n in sampled_frames if left - 1e-7 <= times[n] < right - 1e-7]
    if not any(core["start_frame"] <= n < core["end_frame"] for n in selected):
        raise AnnotationError("核心区间内没有5 Hz采样帧，请增大窗口。")
    return selected


def extract_frames(video, selected, timeline, frame_dir, width):
    """Select by ffmpeg n so the saved image and original global frame agree."""
    frame_dir = Path(frame_dir)
    frame_dir.mkdir(parents=True, exist_ok=True)
    needed = [n for n in selected if not (frame_dir / f"{n:09d}.jpg").is_file()]
    if not needed:
        return
    with tempfile.TemporaryDirectory(prefix=".extract-", dir=frame_dir.parent) as tmp:
        temp = Path(tmp)
        if len(needed) >= 2 and len(set(b-a for a,b in zip(needed,needed[1:]))) == 1:
            stride = needed[1] - needed[0]
            expression = f"between(n,{needed[0]},{needed[-1]})*not(mod(n-{needed[0]},{stride}))"
        else:
            expression = "+".join(f"eq(n,{n})" for n in needed)
        script = temp / "select.ffilter"
        script.write_text(f"select='{expression}',scale='min({width},iw)':-2", encoding="utf-8")
        _command([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(video),
            "-map", "0:v:0", "-filter_script:v", str(script), "-fps_mode", "vfr",
            "-q:v", "2", "-start_number", "0", str(temp / "%09d.jpg"),
        ])
        generated = sorted(temp.glob("*.jpg"))
        if len(generated) != len(needed):
            raise AnnotationError("实际抽帧数量与原视频帧号映射不一致；未调用API。")
        for n, path in zip(needed, generated):
            with Image.open(path) as image:
                image = image.convert("RGB")
                # Labels are outside the original surgical image.
                font = ImageFont.load_default()
                label = f"source_frame={n} | original_time={timeline['times'][n]:.6f}s"
                canvas = Image.new("RGB", (image.width, image.height + 28), "#101820")
                canvas.paste(image, (0, 28))
                ImageDraw.Draw(canvas).text((6, 7), label, font=font, fill="white")
                dest = frame_dir / f"{n:09d}.jpg"
                pending = temp / f"labeled-{n:09d}.jpg"
                canvas.save(pending, quality=92)
                pending.replace(dest)
    save_json(frame_dir.parent / "sampled_frames.json", {
        "sampling_hz": SAMPLE_HZ,
        "source_frame_numbering": timeline["frame_numbering"],
        "frames": [{"frame": n, "time_seconds": timeline["times"][n],
                    "file": f"frames/{n:09d}.jpg"} for n in selected],
        "note": "5 Hz全片网格选取真实源帧；变帧率视频的实际PTS可能略偏离0.2秒网格。",
    })
