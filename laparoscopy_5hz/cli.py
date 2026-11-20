import argparse
from contextlib import contextmanager
import csv
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import shutil
import sys

from . import __version__
from .client import AnnotationClient, build_payload, window_instruction
from .common import AnnotationError, fingerprint, hash_file, read_json, save_json
from .schema import load_ontology, output_schema, resource, validate_result
from .security import DEFAULT_MODEL, DEFAULT_URL, Redactor, api_key, load_environment, safe_base_url
from .video import (
    SAMPLE_HZ, extract_frames, frame_time, inspect_video, make_cores,
    sample_source_frames, with_context,
)


@contextmanager
def output_lock(output):
    # macOS/Linux advisory lock; the stable file must not be unlinked during a run.
    import fcntl
    output.mkdir(parents=True, exist_ok=True)
    with (output / ".run.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AnnotationError("此输出目录已有标注进程运行，请使用其他目录。") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _env_options(parser):
    parser.add_argument("--env-file", help="指定本地 .env；默认只读取当前目录的 .env")
    parser.add_argument("--model", help=f"默认 {DEFAULT_MODEL}，可用 ANNOTATION_MODEL 覆盖")
    parser.add_argument("--base-url", help="OpenAI兼容接口的 /v1 基地址")


def parser():
    root = argparse.ArgumentParser(description="腹腔镜视频5 Hz研究标注；全片源帧号；默认Gemini 4。")
    root.add_argument("--version", action="version", version=__version__)
    sub = root.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor", help="检查依赖和配置；不调用模型，不显示密钥")
    _env_options(doctor)
    annotate = sub.add_parser("annotate", help="抽帧并标注；--dry-run只准备输入")
    _env_options(annotate)
    annotate.add_argument("--video", required=True, type=Path)
    annotate.add_argument("--output", type=Path, default=Path("outputs/run"))
    annotate.add_argument("--start", type=float, help="原视频开始秒，默认0")
    annotate.add_argument("--end", type=float, help="原视频结束秒，不包含；默认视频末尾")
    annotate.add_argument("--start-frame", type=int, help="从0开始的原视频全片起始帧")
    annotate.add_argument("--end-frame", type=int, help="原视频全片结束帧，不包含")
    annotate.add_argument("--window-seconds", type=float, default=10)
    annotate.add_argument("--context-seconds", type=float, default=1)
    annotate.add_argument("--width", type=int, default=720, help="上传图像最大宽度，不放大原图")
    annotate.add_argument("--max-images", type=int, default=100)
    annotate.add_argument("--max-tokens", type=int, default=8000)
    annotate.add_argument("--attempts", type=int, default=3, help="瞬时失败／格式失败最多尝试次数")
    annotate.add_argument("--timeout", type=float, default=300, help="单次响应读取超时秒数")
    annotate.add_argument("--ontology", type=Path, help="JSON词表；未提供字段沿用默认词表")
    annotate.add_argument("--json-mode", action="store_true", help="仅兼容接口支持时开启response_format")
    annotate.add_argument("--dry-run", action="store_true", help="准备真实帧和输入清单；无需密钥，不调用API")
    validate = sub.add_parser("validate", help="校验一个core的JSON，不调用模型")
    validate.add_argument("file", type=Path)
    validate.add_argument("--ontology", type=Path)
    validate.add_argument("--input-manifest", type=Path, help="window_inputs中的清单；同时检查core和采样帧")
    schema = sub.add_parser("schema", help="导出含实际词表枚举的JSON Schema")
    schema.add_argument("--ontology", type=Path)
    schema.add_argument("--output", type=Path)
    return root


def _configuration(args, redactor):
    model = args.model or os.environ.get("ANNOTATION_MODEL") or DEFAULT_MODEL
    base = args.base_url or os.environ.get("ANNOTATION_BASE_URL") or os.environ.get("XHUB_BASE_URL") or DEFAULT_URL
    base = safe_base_url(base, redactor)
    if not model.strip() or redactor.text(model) != model:
        raise AnnotationError("模型名称为空或包含密钥内容。")
    return model, base


def doctor(args, redactor):
    model, base = _configuration(args, redactor)
    deps = {}
    for package in ("requests", "Pillow", "jsonschema", "python-dotenv"):
        try:
            deps[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            deps[package] = "missing"
    report = {
        "python": sys.version.split()[0], "version": __version__,
        "model": model, "base_url": base,
        "api_key_configured": any(os.environ.get(name, "").strip() for name in (
            "ANNOTATION_API_KEY", "XHUB_API_KEY", "OPENAI_API_KEY")),
        "tools": {name: bool(shutil.which(name)) for name in ("ffmpeg", "ffprobe")},
        "dependencies": deps, "sampling_hz": SAMPLE_HZ,
    }
    print(json.dumps(redactor.value(report), ensure_ascii=False, indent=2))
    return 0 if all(report["tools"].values()) and "missing" not in deps.values() else 1


def export_results(output, results, cores, failures, config, timeline, dry_run, redactor):
    events, uncertain, key_events, scenes = [], [], [], []
    for window_id, result in results:
        scenes.append({"window_id": window_id, "core": cores[window_id],
                       "scene_summary_zh": result["scene_summary_zh"]})
        for index, row in enumerate(result["events"]):
            events.append({
                "event_id": f"W{window_id:05d}:E{index+1:04d}",
                "window_id": window_id, **row,
                "start_seconds": frame_time(timeline, row["start_frame"]),
                "end_seconds": frame_time(timeline, row["end_frame"]),
                "review_required": True,
            })
        uncertain.extend({"window_id": window_id, **row} for row in result["uncertain_ranges"])
        key_events.extend({"window_id": window_id, **row} for row in result["key_events"])
    aggregate = {
        "format": "aggregate_with_window_provenance", "sampling_hz": SAMPLE_HZ,
        "frame_numbering": timeline["frame_numbering"],
        "model": config["model"],
        "status": "dry_run" if dry_run else "partial" if failures else "complete_candidate",
        "review_required": True,
        "coverage": {
            "requested_cores": cores,
            "reviewed_cores": [cores[i] for i, _ in results],
            "failed_cores": failures,
        },
        "scene_summaries": scenes, "events": events,
        "uncertain_ranges": uncertain, "key_events": key_events,
        "note": "模型候选标注。跨core片段不自动合并；未调用／失败的core不伪装成已复核。",
    }
    save_json(output / "annotations.json", redactor.value(aggregate))
    fields = [
        "event_id", "window_id", "start_frame", "end_frame", "start_seconds", "end_seconds",
        "instrument_code", "tool_position", "tool_description_zh", "action_code",
        "target_code", "target_detail_zh", "target_certainty", "mediator",
        "result_zh", "evidence_zh", "confidence", "review_required",
    ]
    with (output / "events.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(redactor.value(events))
    return aggregate


def annotate(args, redactor):
    if not args.video.is_file():
        raise AnnotationError("输入视频不存在。")
    numeric = (args.window_seconds, args.context_seconds, args.timeout)
    if any(not math.isfinite(n) for n in numeric):
        raise AnnotationError("窗口、上下文和超时参数必须是有限数。")
    if args.context_seconds < 0 or args.width < 160 or args.max_images < 1:
        raise AnnotationError("上下文不能为负；图像宽度至少160；max-images至少1。")
    if args.max_tokens < 128 or args.attempts < 1 or args.timeout <= 0:
        raise AnnotationError("max-tokens至少128；attempts至少1；timeout必须大于0。")
    if any(v is not None and not math.isfinite(v) for v in (args.start, args.end)):
        raise AnnotationError("时间范围必须是有限数。")
    use_frames = args.start_frame is not None or args.end_frame is not None
    if use_frames and (args.start_frame is None or args.end_frame is None
                       or args.start is not None or args.end is not None):
        raise AnnotationError("起止帧需同时提供，且不能与秒数范围混用。")
    model, base = _configuration(args, redactor)
    key = None if args.dry_run else api_key()
    ontology = load_ontology(args.ontology)
    source = args.video.resolve()
    output = args.output.resolve()
    config = {
        "version": __version__, "source_video_name": source.name, "source_sha256": hash_file(source),
        "sampling_hz": SAMPLE_HZ, "frame_numbering": "zero_based_original_video_presentation_frame",
        "requested_seconds": [args.start or 0, args.end],
        "requested_frames": [args.start_frame, args.end_frame],
        "window_seconds": args.window_seconds, "context_seconds": args.context_seconds,
        "width": args.width, "model": model, "base_url": base,
        "ontology": ontology,
        "prompt_sha256": hashlib.sha256(resource("prompt_zh.txt").encode("utf-8")).hexdigest(),
        "schema_sha256": fingerprint(output_schema(ontology)),
        "max_images": args.max_images, "max_tokens": args.max_tokens, "json_mode": args.json_mode,
    }
    if redactor.value(config) != config:
        raise AnnotationError("运行参数包含密钥内容，不能写入输出目录。")
    config["fingerprint"] = fingerprint(config)
    with output_lock(output):
        config_path = output / "run_config.json"
        if config_path.exists():
            old = read_json(config_path)
            if old.get("fingerprint") != config["fingerprint"]:
                raise AnnotationError("输出目录的配置与本次不同；请指定新的 --output。")
        elif any(p.name != ".run.lock" for p in output.iterdir()):
            raise AnnotationError("输出目录非空且没有匹配的run_config；请使用新的输出目录。")
        save_json(config_path, config)
        timeline_path = output / "video_timeline.json"
        timeline = read_json(timeline_path) if timeline_path.exists() else inspect_video(source)
        save_json(timeline_path, timeline)
        if use_frames:
            if not 0 <= args.start_frame < args.end_frame <= timeline["source_frame_count"]:
                raise AnnotationError("全片帧号范围无效；end-frame可等于总源帧数。")
            a, b = frame_time(timeline, args.start_frame), frame_time(timeline, args.end_frame)
        else:
            a, b = args.start or 0.0, args.end
        cores = make_cores(timeline, a, b, args.window_seconds)
        samples = sample_source_frames(timeline["times"], timeline["duration_seconds"])
        inputs = [with_context(c, timeline, samples, args.context_seconds) for c in cores]
        if max(map(len, inputs)) > args.max_images:
            raise AnnotationError("单窗口图片数超过max-images；请减小窗口或上下文时长。")
        selected = sorted({n for group in inputs for n in group})
        print(f"5 Hz真实帧 {len(selected)} 张；核心窗口 {len(cores)} 个；模型 {model}。")
        print("正在准备带全片帧号和原视频时间的逐张图像。")
        extract_frames(source, selected, timeline, output / "frames", args.width)
        save_json(output / "sampled_frames.json", {
            "sampling_hz": SAMPLE_HZ, "frame_numbering": timeline["frame_numbering"],
            "frames": [{"frame": n, "time_seconds": timeline["times"][n],
                        "file": f"frames/{n:09d}.jpg"} for n in selected],
        })
        save_json(output / "output.schema.json", output_schema(ontology))
        client = None if args.dry_run else AnnotationClient(
            base, key, redactor, args.attempts, args.timeout)
        results, failures, reused = [], [], 0
        for window_id, (core, frames) in enumerate(zip(cores, inputs)):
            stem = f"window_{window_id:05d}"
            input_manifest = {
                "window_id": window_id, "core": core, "source_frames": frames,
                "input_instruction": window_instruction(core, frames, timeline, ontology),
                "image_files": [f"frames/{n:09d}.jpg" for n in frames],
                "prompt_sha256": config["prompt_sha256"],
            }
            save_json(output / "window_inputs" / f"{stem}.json", input_manifest)
            result_path = output / "windows" / f"{stem}.json"
            if result_path.exists():
                try:
                    cached = validate_result(read_json(result_path), core, ontology, frames)
                    results.append((window_id, cached))
                    reused += 1
                    print(f"[{window_id+1}/{len(cores)}] 复用已校验结果。")
                    continue
                except (AnnotationError, ValueError, TypeError):
                    pass
            if args.dry_run:
                continue
            print(f"[{window_id+1}/{len(cores)}] core=[{core['start_frame']},{core['end_frame']})，{len(frames)}张图。")
            try:
                payload = build_payload(model, core, frames, timeline, ontology, output / "frames",
                                        args.max_tokens, args.json_mode)
                result, meta = client.annotate(payload, core, ontology, frames)
                save_json(result_path, result)
                save_json(output / "call_metadata" / f"{stem}.json", redactor.value(meta))
                failure_path = output / "failures" / f"{stem}.json"
                if failure_path.exists():
                    failure_path.unlink()
                results.append((window_id, result))
            except AnnotationError as error:
                failure = {"window_id": window_id, "core": core, "reason": redactor.text(error)}
                failures.append(failure)
                save_json(output / "failures" / f"{stem}.json", failure)
                print(f"[{window_id+1}/{len(cores)}] 此core未完成：" + failure["reason"], file=sys.stderr)
                if "HTTP 401" in failure["reason"] or "HTTP 403" in failure["reason"]:
                    # Remaining cores have not been sent; keep their pending state explicit.
                    for pending_id in range(window_id+1, len(cores)):
                        failures.append({"window_id": pending_id, "core": cores[pending_id],
                                         "reason": "认证／权限错误后未发起请求"})
                    break
        aggregate = export_results(output, results, cores, failures, config, timeline, args.dry_run, redactor)
        print(f"已校验 {len(results)}/{len(cores)} core；复用 {reused}；事件 {len(aggregate['events'])} 条。")
        if args.dry_run:
            print("dry-run完成：未调用API，未生成模型标注。去掉 --dry-run 可继续使用相同目录。")
        print("输出：" + redactor.text(output))
        return 1 if failures else 0


def main():
    args = parser().parse_args()
    redactor = Redactor(os.environ.get(k, "") for k in (
        "ANNOTATION_API_KEY", "XHUB_API_KEY", "OPENAI_API_KEY"))
    try:
        redactor = load_environment(getattr(args, "env_file", None))
        if args.command == "doctor":
            code = doctor(args, redactor)
        elif args.command == "annotate":
            code = annotate(args, redactor)
        elif args.command == "schema":
            schema = output_schema(load_ontology(args.ontology))
            if args.output:
                save_json(args.output, schema)
            else:
                print(json.dumps(schema, ensure_ascii=False, indent=2))
            code = 0
        else:
            result = read_json(args.file)
            if not isinstance(result, dict):
                raise AnnotationError("待校验文件必须是一个 JSON 对象。")
            manifest = read_json(args.input_manifest) if args.input_manifest else None
            core = manifest["core"] if manifest else {
                "start_frame": result.get("reviewed_start_frame"),
                "end_frame": result.get("reviewed_end_frame"),
            }
            validate_result(result, core, load_ontology(args.ontology),
                            manifest["source_frames"] if manifest else None)
            print("JSON、词表、半开区间" + ("和实际输入帧" if manifest else "") + "校验通过。")
            code = 0
    except (AnnotationError, OSError, ValueError, KeyError, TypeError) as error:
        print("未完成：" + redactor.text(error), file=sys.stderr)
        code = 1
    except KeyboardInterrupt:
        print("已停止；下次同配置启动会复用已完成的core。", file=sys.stderr)
        code = 130
    raise SystemExit(code)
