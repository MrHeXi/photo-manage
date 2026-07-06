from __future__ import annotations

import base64
import json
import mimetypes
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


@dataclass(slots=True)
class AiConfig:
    base_url: str
    api_key: str
    model: str
    timeout: int = 60

    @property
    def endpoint(self) -> str:
        base_url = self.base_url.strip().rstrip("/")
        if base_url.endswith("/chat/completions"):
            return base_url
        return base_url + "/chat/completions"


@dataclass(slots=True)
class AiAnalysisResult:
    summary: str
    score: float | None
    raw_response: str


@dataclass(frozen=True, slots=True)
class AiConfigValidation:
    ok: bool
    normalized_base_url: str
    endpoint: str
    errors: tuple[str, ...]
    warnings: tuple[str, ...]


def validate_ai_config(base_url: str, model: str, api_key: str) -> AiConfigValidation:
    normalized_base_url = base_url.strip().rstrip("/")
    model = model.strip()
    api_key = api_key.strip()
    endpoint = AiConfig(normalized_base_url, api_key, model).endpoint if normalized_base_url else ""
    errors: list[str] = []
    warnings: list[str] = []

    if not normalized_base_url:
        errors.append("缺少 API 地址。")
    else:
        parsed = urlparse(normalized_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            errors.append("API 地址需要是完整的 http:// 或 https:// URL。")
        if parsed.scheme == "http":
            warnings.append("当前使用 HTTP，API Key 可能以明文传输；建议优先使用 HTTPS。")
        if not parsed.path or parsed.path == "/":
            warnings.append("API 地址通常包含版本路径，例如 /v1；如果模型厂商文档另有要求可忽略。")
        if parsed.path.rstrip("/").endswith("/chat/completions"):
            warnings.append("检测到已填写完整 chat/completions 地址，程序会直接使用该地址。")

    if not model:
        errors.append("缺少模型名。")
    if not api_key:
        errors.append("缺少 API Key。")
    elif len(api_key) < 8:
        warnings.append("API Key 看起来偏短，请确认是否复制完整。")

    return AiConfigValidation(
        ok=not errors,
        normalized_base_url=normalized_base_url,
        endpoint=endpoint,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


def format_ai_config_validation(validation: AiConfigValidation) -> str:
    lines = [
        "AI 配置本地检查",
        "",
        f"结果：{'可用于发起请求' if validation.ok else '需要补充配置'}",
    ]
    if validation.normalized_base_url:
        lines.append(f"API 地址：{validation.normalized_base_url}")
    if validation.endpoint:
        lines.append(f"请求端点：{validation.endpoint}")
    if validation.errors:
        lines.extend(["", "需要修复："])
        lines.extend(f"- {error}" for error in validation.errors)
    if validation.warnings:
        lines.extend(["", "提醒："])
        lines.extend(f"- {warning}" for warning in validation.warnings)
    if validation.ok and not validation.warnings:
        lines.extend(["", "未发现明显配置问题。"])
    lines.extend(["", "这只是本地格式检查，不会联网验证 API Key 是否有效。"])
    return "\n".join(lines)


def image_data_url(path: Path) -> str:
    mime_type, _ = mimetypes.guess_type(path.name)
    if not mime_type:
        mime_type = "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def build_quality_prompt() -> str:
    return (
        "请分析这张照片是否值得保留。重点判断清晰度、曝光、构图、主体、重复/连拍价值、"
        "是否像截图或无意义图片。请用中文回答，并给出 0-100 的保留分数。"
    )


def analyze_image(path: Path, config: AiConfig, prompt: str | None = None) -> AiAnalysisResult:
    body = {
        "model": config.model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt or build_quality_prompt()},
                    {"type": "image_url", "image_url": {"url": image_data_url(path)}},
                ],
            }
        ],
        "temperature": 0.2,
    }
    request = urllib.request.Request(
        config.endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=config.timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"AI API HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"AI API request failed: {exc.reason}") from exc

    summary = parse_chat_content(raw)
    return AiAnalysisResult(summary=summary, score=parse_score(summary), raw_response=raw)


def parse_chat_content(raw_response: str) -> str:
    data = json.loads(raw_response)
    choices = data.get("choices") or []
    if not choices:
        return raw_response
    message = choices[0].get("message") or {}
    content = message.get("content", "")
    if isinstance(content, str):
        return content.strip()
    return json.dumps(content, ensure_ascii=False)


def parse_score(text: str) -> float | None:
    import re

    patterns = [
        r"(?:分数|评分|保留分数|score)\D{0,8}(\d{1,3})(?:\s*/\s*100)?",
        r"(\d{1,3})\s*/\s*100",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        value = int(match.group(1))
        if 0 <= value <= 100:
            return float(value)
    return None
