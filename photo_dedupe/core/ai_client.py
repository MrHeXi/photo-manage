from __future__ import annotations

import base64
import json
import mimetypes
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class AiConfig:
    base_url: str
    api_key: str
    model: str
    timeout: int = 60

    @property
    def endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"


@dataclass(slots=True)
class AiAnalysisResult:
    summary: str
    score: float | None
    raw_response: str


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

