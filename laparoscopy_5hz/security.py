import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv

from .common import AnnotationError

KEY_NAMES = ("ANNOTATION_API_KEY", "XHUB_API_KEY", "OPENAI_API_KEY")
DEFAULT_URL = "https://api3.xhub.chat/v1"
DEFAULT_MODEL = "gemini-4-argon"


class Redactor:
    def __init__(self, secrets=()):
        values = {variant for s in secrets if s for variant in (s, s.strip()) if variant}
        self.secrets = sorted(values, key=len, reverse=True)

    def text(self, text):
        text = str(text)
        for value in self.secrets:
            text = text.replace(value, "[REDACTED_API_KEY]")
        return text

    def value(self, value):
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(x) for x in value]
        if isinstance(value, dict):
            return {self.text(k): self.value(v) for k, v in value.items()}
        return value


def load_environment(env_file=None):
    # Use only a specifically located file; do not search parents for unrelated .env.
    path = Path(env_file) if env_file else Path.cwd() / ".env"
    if env_file and not path.is_file():
        raise AnnotationError("指定的 .env 文件不存在。")
    if path.is_file():
        load_dotenv(path, override=False)
    return Redactor(os.environ.get(k, "") for k in KEY_NAMES)


def api_key():
    for name in KEY_NAMES:
        value = os.environ.get(name, "").strip()
        if value:
            if "\r" in value or "\n" in value:
                raise AnnotationError("API key 包含换行，请检查本地配置。")
            return value
    raise AnnotationError("尚未设置 API key。请填写本地 .env 或设置 XHUB_API_KEY。")


def safe_base_url(url, redactor):
    parts = urlsplit(url.strip())
    if (not parts.hostname or parts.username or parts.password or parts.query
            or parts.fragment or redactor.text(url) != url):
        raise AnnotationError("API 地址不能含账号、密码、API key、查询参数或片段。")
    if parts.scheme != "https":
        if parts.scheme != "http" or parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise AnnotationError("API 必须使用 HTTPS；仅本地测试地址允许 HTTP。")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
