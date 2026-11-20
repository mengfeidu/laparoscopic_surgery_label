"""Meter image delivery with identical source frames and text, without annotation."""
import argparse
import base64
import json
import math
import os
from pathlib import Path
import time

import requests

from .common import AnnotationError, fingerprint, read_json, save_json, hash_file
from .cost import safe_usage, summarize
from .images import contact_sheet, image_config
from .security import DEFAULT_MODEL, DEFAULT_URL, api_key, load_environment, safe_base_url
from .video import extract_frames, inspect_video, sample_source_frames


def prepare_modes(frames, timeline, frame_dir):
    singles = [(frame_dir/f"{n:09d}.jpg").read_bytes() for n in frames]
    grid16 = [contact_sheet(frames[i:i+16], timeline, frame_dir,
                           image_config("grid")) for i in range(0,len(frames),16)]
    grid4 = [contact_sheet(frames[i:i+4], timeline, frame_dir,
                          image_config("grid", frames_per_image=4, columns=2,
                                       tile_width=640, tile_height=512))
             for i in range(0,len(frames),4)]
    return [("text_baseline", [], "high"), ("individual_high", singles, "high"),
            ("individual_low", singles, "low"), ("montage_4x4_high", grid16, "high"),
            ("montage_4x4_low", grid16, "low"), ("montage_2x2_high", grid4, "high")]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--start", type=float, default=314)
    parser.add_argument("--frames", type=int, default=16)
    parser.add_argument("--model", default=None)
    parser.add_argument("--base-url")
    parser.add_argument("--env-file")
    args = parser.parse_args()
    redactor = load_environment(args.env_file)
    try:
        if not math.isfinite(args.start) or args.start < 0 or not 1 <= args.frames <= 60:
            raise AnnotationError("start须非负有限，frames须1–60。")
        model = args.model or os.environ.get("ANNOTATION_MODEL") or DEFAULT_MODEL
        if not model.strip() or redactor.text(model) != model:
            raise AnnotationError("模型名无效。")
        base = safe_base_url(args.base_url or os.environ.get("ANNOTATION_BASE_URL")
                             or os.environ.get("XHUB_BASE_URL") or DEFAULT_URL, redactor)
        key = api_key()
        timeline = inspect_video(args.video.resolve())
        frames = [n for n in sample_source_frames(timeline["times"], timeline["duration_seconds"])
                  if timeline["times"][n] >= args.start-1e-7][:args.frames]
        if len(frames) != args.frames:
            raise AnnotationError("真实采样帧不足。")
        config = {"source": str(args.video.resolve()), "model":model, "base_url":base,
                  "source_sha256":hash_file(args.video),
                  "frames":frames, "times":[timeline["times"][n] for n in frames]}
        config["fingerprint"] = fingerprint(config)
        args.output.mkdir(parents=True, exist_ok=True)
        cfg = args.output/"benchmark_config.json"
        if cfg.exists() and read_json(cfg) != config:
            raise AnnotationError("计量配置改变；请换输出目录。")
        save_json(cfg, redactor.value(config))
        frame_dir = args.output/"frames"
        extract_frames(args.video.resolve(), frames, timeline, frame_dir, 720)
        text = ('这是图像token计量实验。无需识别手术、器械或组织。只返回JSON：'
                '{"ok":true}。帧号及时间：'+json.dumps([
                    {"frame":n,"time":timeline["times"][n]} for n in frames]))
        results = []
        for name, images, detail in prepare_modes(frames, timeline, frame_dir):
            path = args.output/f"{name}.json"
            row = read_json(path) if path.exists() else {
                "mode":name, "source_frames":len(frames), "uploaded_images":len(images),
                "detail":detail, "attempt_records":[], "status":"pending"}
            if row["status"] != "measured":
                content = [{"type":"text","text":text}] + [
                    {"type":"image_url","image_url":{
                        "url":"data:image/jpeg;base64,"+base64.b64encode(im).decode("ascii"),
                        "detail":detail}} for im in images]
                for _ in range(3):
                    record = {"category":"network_error","usage":{}}
                    try:
                        response = requests.post(base+"/chat/completions",
                            headers={"Authorization":"Bearer "+key},
                            json={"model":model,"temperature":0,"max_tokens":64,
                                  "messages":[{"role":"user","content":content}]},
                            timeout=(30,300),allow_redirects=False)
                        record.update(category="http_error", status=response.status_code)
                        if response.status_code == 200:
                            record["usage"] = safe_usage(response.json().get("usage"))
                            record["category"] = "measured" if "prompt_tokens" in record["usage"] else "missing_usage"
                    except (requests.RequestException, ValueError, AttributeError):
                        pass
                    row["attempt_records"].append(record)
                    row["status"] = record["category"]
                    if row["status"] == "measured":
                        row["usage"] = record["usage"]
                    save_json(path, redactor.value(row))
                    if row["status"] == "measured" or record.get("status",503) not in {200,408,429,500,502,503,504}:
                        break
                    time.sleep(1)
            results.append(row)
            report = {"model":model, "results":results,
                      "usage":summarize([r for x in results for r in x["attempt_records"]]),
                      "note":"同帧同文本计量；不代表标注质量。成功模式续跑复用，失败用量单独审计。"}
            if results[0]["status"] == "measured":
                baseline = results[0]["usage"]["prompt_tokens"]
                for x in results:
                    if x["status"] == "measured":
                        x["net_image_prompt_tokens"] = x["usage"]["prompt_tokens"]-baseline
            save_json(args.output/"benchmark.json", redactor.value(report))
            print(name, row["status"], row.get("usage",{}), flush=True)
        if any(r["status"] != "measured" for r in results):
            raise AnnotationError("部分模式失败；已保留用量记录，续跑仅补测未成功模式。")
    except (AnnotationError, OSError, ValueError) as error:
        parser.exit(1, "未完成："+redactor.text(error)+"\n")


if __name__ == "__main__":
    main()
