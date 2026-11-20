import copy
import json
from importlib.resources import files
from pathlib import Path

from jsonschema import Draft202012Validator

from .common import AnnotationError, read_json


def resource(name):
    return files("laparoscopy_5hz").joinpath("resources", name).read_text(encoding="utf-8")


def load_ontology(path=None):
    default = json.loads(resource("ontology.json"))
    if path is None:
        return default
    custom = read_json(path)
    if not isinstance(custom, dict) or set(custom) - set(default):
        raise AnnotationError("ontology 必须是对象；字段只能使用默认词表中的7个字段。")
    for name, values in custom.items():
        if (not isinstance(values, list) or not values
                or any(not isinstance(v, str) or not v.strip() for v in values)
                or len(set(values)) != len(values)):
            raise AnnotationError(f"ontology 的 {name} 必须为非空且不重复的代码数组。")
    return {**default, **custom}


def output_schema(ontology):
    schema = json.loads(resource("output.schema.json"))
    props = schema["properties"]["events"]["items"]["properties"]
    for name, values in ontology.items():
        if name == "key_events.kind":
            schema["properties"]["key_events"]["items"]["properties"]["kind"]["enum"] = values
        else:
            props[name]["enum"] = values
    return schema


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 含重复字段")
        result[key] = value
    return result


def parse_response(content):
    if not isinstance(content, str):
        raise AnnotationError("模型响应不是 JSON 文本。")
    try:
        def reject_constant(_):
            raise ValueError("JSON 不允许非有限数")
        return json.loads(content, object_pairs_hook=_no_duplicate_keys, parse_constant=reject_constant)
    except (ValueError, TypeError):
        # Never include response text in validation errors.
        raise AnnotationError("模型未返回单个严格合法的 JSON 对象。") from None


def validate_result(result, core, ontology, sampled_frames=None):
    errors = list(Draft202012Validator(output_schema(ontology)).iter_errors(result))
    if errors:
        locations = ["/".join(map(str, e.absolute_path)) or "<root>" for e in errors[:6]]
        raise AnnotationError("JSON 结构或词表校验失败，字段：" + "、".join(locations))
    start, end = core["start_frame"], core["end_frame"]
    if (result["reviewed_start_frame"], result["reviewed_end_frame"]) != (start, end):
        raise AnnotationError("reviewed 起止帧与本次 core 不一致。")
    allowed = None if sampled_frames is None else set(sampled_frames) | {start, end}
    seen = set()
    for list_name in ("events", "uncertain_ranges"):
        for index, row in enumerate(result[list_name]):
            a, b = row["start_frame"], row["end_frame"]
            if not start <= a < b <= end:
                raise AnnotationError(f"{list_name}[{index}] 越出 core 或起止顺序错误。")
            if allowed is not None and (a not in allowed or b not in allowed):
                raise AnnotationError(f"{list_name}[{index}] 使用了未提供的边界帧。")
            if list_name == "events":
                identity = (a, b, row["instrument_code"], row["tool_position"],
                            row["action_code"], row["target_code"], row["mediator"])
                if identity in seen:
                    raise AnnotationError("events 中存在完全重复的交互。")
                seen.add(identity)
                if row["target_code"] == "gauze" and row["mediator"] != "none":
                    raise AnnotationError("纱布为目标时 mediator 必须为 none。")
                if row["action_code"] == "wipe" and row["target_code"] == "gauze":
                    raise AnnotationError("wipe 的目标应为被擦组织；纱布是中介。")
    shown = None if sampled_frames is None else set(sampled_frames)
    for index, row in enumerate(result["key_events"]):
        if not start <= row["frame"] < end:
            raise AnnotationError(f"key_events[{index}] 越出 core。")
        if shown is not None and row["frame"] not in shown:
            raise AnnotationError(f"key_events[{index}] 使用了未提供的图像帧。")
    return copy.deepcopy(result)
