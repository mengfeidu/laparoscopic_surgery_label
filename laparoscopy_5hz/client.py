import json
import time

import requests

from .common import AnnotationError
from .cost import safe_usage, summarize
from .images import image_config, image_content
from .schema import parse_response, resource, validate_result


def window_instruction(core, selected, timeline, ontology):
    return (
        "本次输入参数如下。所有 frame 均为从0开始的原视频全片帧号，"
        "不是本窗口内序号，也不是5 Hz样本序号。"
        "标签时间为原视频PTS减去首帧PTS；变帧率源视频的实际时间可略偏离0.2秒网格。"
        "图像按全片帧号和时间升序提供。上下文仅用于判读，输出必须裁剪到core。"
        "事件跨越core边界时仅输出其在本core内的部分；不要补写未观察到的操作结果。"
        "起点使用已提供样本的帧号或core起点。终点不包含，可用最后支持帧加1，"
        "并截断到core终点；采样间隔不代表定位精度。"
        "key_events.frame必须是core内实际提供图像的帧号。\n"
        + json.dumps({
            "core": [core["start_frame"], core["end_frame"]],
            "core_time_seconds": [core["start_seconds"], core["end_seconds"]],
            "reviewed_start_frame": core["start_frame"],
            "reviewed_end_frame": core["end_frame"],
            "sampling_hz": 5,
            "original_video_frame_count": timeline["source_frame_count"],
            "shown_source_frames": selected,
            "ontology": ontology,
        }, ensure_ascii=False)
    )


def build_payload(model, core, selected, timeline, ontology, frame_dir, max_tokens, json_mode=False,
                  delivery=None, prompt=None):
    delivery = delivery or image_config()
    content = [{"type": "text", "text": window_instruction(core, selected, timeline, ontology)}]
    content += image_content(selected, timeline, frame_dir, delivery)
    payload = {
        "model": model, "temperature": 0, "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": prompt if prompt is not None else resource("prompt_zh.txt")},
            {"role": "user", "content": content},
        ],
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    return payload


class AnnotationClient:
    def __init__(self, base_url, key, redactor, attempts=3, read_timeout=300, session=None, sleeper=None):
        self.endpoint = base_url + "/chat/completions"
        self._key = key
        self.redactor = redactor
        self.attempts = attempts
        self.read_timeout = read_timeout
        self.session = session or requests.Session()
        self.sleep = sleeper or time.sleep
        self.last_metadata = {}

    def annotate(self, payload, core, ontology, selected):
        correction = None
        history = []
        self.last_metadata = {"attempt_records": history}
        for attempt in range(self.attempts):
            record = {"attempt": attempt+1, "category": "unknown", "usage": {}}
            history.append(record)
            request_payload = dict(payload)
            if correction:
                request_payload["messages"] = payload["messages"] + [{
                    "role": "user",
                    "content": "上次响应未通过程序校验：" + correction
                    + "。请依据相同图像重新检查，并只输出符合core、词表和格式的JSON。",
                }]
            try:
                response = self.session.post(
                    self.endpoint,
                    headers={"Authorization": "Bearer " + self._key, "Content-Type": "application/json"},
                    json=request_payload, timeout=(30, self.read_timeout), allow_redirects=False,
                )
            except requests.RequestException:
                record["category"] = "network_error"
                if attempt+1 == self.attempts:
                    raise AnnotationError("API 网络请求失败；未保存密钥或响应正文。") from None
                self.sleep(min(0.8 * 2**attempt, 8))
                continue
            if response.status_code != 200:
                status = response.status_code
                # Retry only transient statuses, never a policy/authentication rejection.
                record.update(category="http_error", status=status)
                if status not in {408, 429, 500, 502, 503, 504} or attempt+1 == self.attempts:
                    raise AnnotationError(f"API 返回 HTTP {status}；响应正文未写入日志。")
                self.sleep(min(0.8 * 2**attempt, 8))
                continue
            try:
                body = response.json()
                record["usage"] = safe_usage(body.get("usage"))
                choices = body.get("choices")
                if not isinstance(choices, list) or not choices:
                    raise AnnotationError("API 响应缺少 choices。")
                choice = choices[0]
                message = choice.get("message") or {}
                if message.get("refusal") or choice.get("finish_reason") == "content_filter":
                    raise AnnotationError("API 拒绝此图像请求；保持缺失，不自动重试。")
                if choice.get("finish_reason") == "length":
                    raise AnnotationError("模型输出被截断；请减小窗口或增大 --max-tokens。")
                content = message.get("content")
                if isinstance(content, list):
                    content = "".join(x.get("text", "") for x in content if isinstance(x, dict))
                if not content:
                    raise AnnotationError("API 返回空内容。")
                result, parse_meta = parse_response(self.redactor.text(content), with_metadata=True)
                result = validate_result(result, core, ontology, selected)
                # Persist only a narrow, safe metadata allowlist; never the whole API body.
                record["category"] = "validated"
                response_model = body.get("model")
                if not isinstance(response_model, str):
                    response_model = payload["model"]
                metadata = {
                    "requested_model": self.redactor.text(payload["model"]),
                    "response_model": self.redactor.text(response_model[:200]),
                    "usage": summarize(history), "attempts": attempt+1,
                    "prior_attempts": history[:-1], "attempt_records": history,
                    **parse_meta,
                }
                self.last_metadata = metadata
                return result, metadata
            except AnnotationError as error:
                correction = self.redactor.text(error)
                if "拒绝此图像" in correction:
                    record["category"] = "refusal"
                    raise
            except (ValueError, TypeError, KeyError, AttributeError):
                correction = "API 响应结构或JSON解析失败"
            record["category"] = "invalid_output"
            if attempt+1 < self.attempts:
                self.sleep(min(0.8 * 2**attempt, 8))
        raise AnnotationError("模型响应连续未通过校验：" + (correction or "未知格式错误"))
