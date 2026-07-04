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
        f"整理规则：{profile.organize_rule}"
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
