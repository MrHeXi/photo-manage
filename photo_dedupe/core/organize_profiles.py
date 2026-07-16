from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROFILE_SETTINGS_KEY = "organize_profiles"


@dataclass(frozen=True)
class OrganizeProfile:
    name: str
    scan_roots: tuple[Path, ...]
    protected_roots: tuple[Path, ...]
    duplicate_destination: Path
    keep_strategy: str
    bulk_keep_rule: str
    bulk_keep_keyword: str
    processing_action: str
    similarity_threshold: int
    same_ratio_only: bool
    same_resolution_only: bool
    rotate_hint: bool
    organize_rule: str
    organize_keep_only: bool
    photo_target: Path
    video_target: Path
    document_target: Path
    other_target: Path


@dataclass(frozen=True)
class OrganizeTemplate:
    key: str
    name: str
    description: str
    duplicate_folder: str
    photo_folder: str
    video_folder: str
    document_folder: str
    other_folder: str
    keep_strategy: str
    bulk_keep_rule: str
    bulk_keep_keyword: str
    processing_action: str
    similarity_threshold: int
    same_ratio_only: bool
    same_resolution_only: bool
    rotate_hint: bool
    organize_rule: str
    organize_keep_only: bool


BUILTIN_TEMPLATES = (
    OrganizeTemplate(
        key="family_photo_library",
        name="家庭照片整理",
        description="适合把多盘照片和视频汇总到一个主资料库，优先保留保护目录、原图和最高质量版本。",
        duplicate_folder="重复待复核",
        photo_folder="照片",
        video_folder="视频",
        document_folder="文档",
        other_folder="其他",
        keep_strategy="保留最高质量",
        bulk_keep_rule="保留保护目录内文件",
        bulk_keep_keyword="原图",
        processing_action="移动到目标目录",
        similarity_threshold=8,
        same_ratio_only=True,
        same_resolution_only=False,
        rotate_hint=True,
        organize_rule="按拍摄年月",
        organize_keep_only=True,
    ),
    OrganizeTemplate(
        key="multi_disk_collect",
        name="多盘照片汇总",
        description="适合照片散落在多个磁盘时先按拍摄日期归档，再把重复副本移动到待复核目录。",
        duplicate_folder="多盘重复待复核",
        photo_folder="按日期照片",
        video_folder="按日期视频",
        document_folder="随附文档",
        other_folder="其他素材",
        keep_strategy="保留最高质量",
        bulk_keep_rule="保留最高分辨率",
        bulk_keep_keyword="",
        processing_action="移动到目标目录",
        similarity_threshold=8,
        same_ratio_only=True,
        same_resolution_only=False,
        rotate_hint=True,
        organize_rule="按拍摄日期",
        organize_keep_only=True,
    ),
    OrganizeTemplate(
        key="work_documents",
        name="工作文档归档",
        description="适合整理项目资料、合同、表格和截图，优先保留最新修改和工作目录中的文件。",
        duplicate_folder="文档重复待复核",
        photo_folder="参考图片",
        video_folder="视频资料",
        document_folder="工作文档",
        other_folder="其他资料",
        keep_strategy="保留最新修改",
        bulk_keep_rule="保留最新修改",
        bulk_keep_keyword="工作",
        processing_action="移动到目标目录",
        similarity_threshold=6,
        same_ratio_only=False,
        same_resolution_only=False,
        rotate_hint=False,
        organize_rule="按文件类型",
        organize_keep_only=False,
    ),
    OrganizeTemplate(
        key="chat_cache_review",
        name="聊天缓存复核",
        description="适合微信、QQ 等缓存图片和文件复核，优先保留非缓存目录里的版本。",
        duplicate_folder="缓存重复待复核",
        photo_folder="缓存图片",
        video_folder="缓存视频",
        document_folder="缓存文档",
        other_folder="缓存其他",
        keep_strategy="保留最高质量",
        bulk_keep_rule="保留非缓存目录",
        bulk_keep_keyword="Cache",
        processing_action="移动到目标目录",
        similarity_threshold=10,
        same_ratio_only=True,
        same_resolution_only=False,
        rotate_hint=True,
        organize_rule="按文件类型",
        organize_keep_only=False,
    ),
)


def list_builtin_templates() -> list[OrganizeTemplate]:
    return list(BUILTIN_TEMPLATES)


def get_builtin_template(key: str) -> OrganizeTemplate:
    for template in BUILTIN_TEMPLATES:
        if template.key == key:
            return template
    raise ValueError(f"未知整理模板：{key}")


def build_template_profile(
    template_key: str,
    base_root: Path,
    *,
    scan_roots: tuple[Path, ...] = (),
    protected_roots: tuple[Path, ...] = (),
) -> OrganizeProfile:
    template = get_builtin_template(template_key)
    root = Path(base_root)
    return OrganizeProfile(
        name=template.name,
        scan_roots=scan_roots,
        protected_roots=protected_roots,
        duplicate_destination=root / template.duplicate_folder,
        keep_strategy=template.keep_strategy,
        bulk_keep_rule=template.bulk_keep_rule,
        bulk_keep_keyword=template.bulk_keep_keyword,
        processing_action=template.processing_action,
        similarity_threshold=template.similarity_threshold,
        same_ratio_only=template.same_ratio_only,
        same_resolution_only=template.same_resolution_only,
        rotate_hint=template.rotate_hint,
        organize_rule=template.organize_rule,
        organize_keep_only=template.organize_keep_only,
        photo_target=root / template.photo_folder,
        video_target=root / template.video_folder,
        document_target=root / template.document_folder,
        other_target=root / template.other_folder,
    )


def clean_profile_name(name: str) -> str:
    cleaned = " ".join(name.strip().split())
    if not cleaned:
        raise ValueError("方案名称不能为空。")
    return cleaned


def encode_profiles(profiles: list[OrganizeProfile]) -> str:
    return json.dumps([profile_to_dict(profile) for profile in profiles], ensure_ascii=False, separators=(",", ":"))


def decode_profiles(text: str) -> list[OrganizeProfile]:
    if not text.strip():
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []

    profiles: list[OrganizeProfile] = []
    for item in payload:
        try:
            profiles.append(profile_from_dict(item))
        except (TypeError, ValueError):
            continue
    return profiles


def upsert_profile(profiles: list[OrganizeProfile], profile: OrganizeProfile) -> list[OrganizeProfile]:
    target = profile.name.casefold()
    result: list[OrganizeProfile] = []
    replaced = False
    for existing in profiles:
        if existing.name.casefold() == target:
            result.append(profile)
            replaced = True
        else:
            result.append(existing)
    if not replaced:
        result.append(profile)
    return result


def remove_profile(profiles: list[OrganizeProfile], name: str) -> list[OrganizeProfile]:
    target = clean_profile_name(name).casefold()
    return [profile for profile in profiles if profile.name.casefold() != target]


def profile_summary(profile: OrganizeProfile) -> str:
    return (
        f"扫描目录 {len(profile.scan_roots)} 个，保护目录 {len(profile.protected_roots)} 个，"
        f"保留策略：{profile.keep_strategy}，批量规则：{profile.bulk_keep_rule}，"
        f"归档方式：{profile.organize_rule}"
    )


def profile_to_dict(profile: OrganizeProfile) -> dict[str, Any]:
    return {
        "name": profile.name,
        "scan_roots": _paths_to_text(profile.scan_roots),
        "protected_roots": _paths_to_text(profile.protected_roots),
        "duplicate_destination": str(profile.duplicate_destination),
        "keep_strategy": profile.keep_strategy,
        "bulk_keep_rule": profile.bulk_keep_rule,
        "bulk_keep_keyword": profile.bulk_keep_keyword,
        "processing_action": profile.processing_action,
        "similarity_threshold": profile.similarity_threshold,
        "same_ratio_only": profile.same_ratio_only,
        "same_resolution_only": profile.same_resolution_only,
        "rotate_hint": profile.rotate_hint,
        "organize_rule": profile.organize_rule,
        "organize_keep_only": profile.organize_keep_only,
        "photo_target": str(profile.photo_target),
        "video_target": str(profile.video_target),
        "document_target": str(profile.document_target),
        "other_target": str(profile.other_target),
    }


def profile_from_dict(data: Any) -> OrganizeProfile:
    if not isinstance(data, dict):
        raise TypeError("profile data must be a dictionary")
    name = clean_profile_name(str(data.get("name", "")))
    return OrganizeProfile(
        name=name,
        scan_roots=_paths_from_text(data.get("scan_roots", [])),
        protected_roots=_paths_from_text(data.get("protected_roots", [])),
        duplicate_destination=Path(str(data.get("duplicate_destination", ""))),
        keep_strategy=str(data.get("keep_strategy", "")),
        bulk_keep_rule=str(data.get("bulk_keep_rule", "")),
        bulk_keep_keyword=str(data.get("bulk_keep_keyword", "")),
        processing_action=str(data.get("processing_action", "")),
        similarity_threshold=_int_in_range(data.get("similarity_threshold", 8), 1, 24),
        same_ratio_only=_bool(data.get("same_ratio_only", False)),
        same_resolution_only=_bool(data.get("same_resolution_only", False)),
        rotate_hint=_bool(data.get("rotate_hint", True)),
        organize_rule=str(data.get("organize_rule", "")),
        organize_keep_only=_bool(data.get("organize_keep_only", False)),
        photo_target=Path(str(data.get("photo_target", ""))),
        video_target=Path(str(data.get("video_target", ""))),
        document_target=Path(str(data.get("document_target", ""))),
        other_target=Path(str(data.get("other_target", ""))),
    )


def _paths_to_text(paths: tuple[Path, ...]) -> list[str]:
    return [str(path) for path in paths]


def _paths_from_text(value: Any) -> tuple[Path, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(Path(str(item)) for item in value if str(item).strip())


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _int_in_range(value: Any, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = minimum
    return max(minimum, min(number, maximum))
