from __future__ import annotations

from dataclasses import dataclass


STATUS_DONE = "完成"
STATUS_NEXT = "下一步"
STATUS_WAITING = "待处理"
STATUS_RECOMMENDED = "建议"


@dataclass(frozen=True)
class OnboardingSnapshot:
    scan_root_count: int
    protected_root_count: int
    duplicate_group_count: int
    keep_selected_count: int
    organize_target_count: int
    organize_preview_count: int
    profile_count: int


@dataclass(frozen=True)
class OnboardingStep:
    key: str
    title: str
    status: str
    detail: str
    action: str

    @property
    def completed(self) -> bool:
        return self.status == STATUS_DONE


def build_onboarding_steps(snapshot: OnboardingSnapshot) -> list[OnboardingStep]:
    roots_ready = snapshot.scan_root_count > 0
    groups_ready = snapshot.duplicate_group_count > 0
    keep_ready = snapshot.keep_selected_count > 0
    targets_ready = snapshot.organize_target_count >= 4
    preview_ready = snapshot.organize_preview_count > 0

    return [
        OnboardingStep(
            key="scan_roots",
            title="选择扫描目录",
            status=STATUS_DONE if roots_ready else STATUS_NEXT,
            detail=f"已选择 {snapshot.scan_root_count} 个目录" if roots_ready else "还没有扫描目录",
            action="添加文件夹" if not roots_ready else "继续",
        ),
        OnboardingStep(
            key="protected_roots",
            title="标记保护目录",
            status=_protection_status(snapshot),
            detail=f"已保护 {snapshot.protected_root_count} 个目录" if snapshot.protected_root_count else "主资料库建议设为保护目录",
            action="设为保护目录" if roots_ready and not snapshot.protected_root_count else "继续",
        ),
        OnboardingStep(
            key="scan",
            title="扫描重复文件",
            status=STATUS_DONE if groups_ready else (STATUS_NEXT if roots_ready else STATUS_WAITING),
            detail=f"当前有 {snapshot.duplicate_group_count} 个重复/相似分组" if groups_ready else "等待扫描结果",
            action="开始扫描" if roots_ready and not groups_ready else "继续",
        ),
        OnboardingStep(
            key="keep_rule",
            title="应用保留规则",
            status=STATUS_DONE if keep_ready else (STATUS_NEXT if groups_ready else STATUS_WAITING),
            detail=f"已标记保留 {snapshot.keep_selected_count} 个文件" if keep_ready else "扫描后可批量标记保留文件",
            action="应用保留规则" if groups_ready and not keep_ready else "继续",
        ),
        OnboardingStep(
            key="targets",
            title="确认整理目标",
            status=STATUS_DONE if targets_ready else STATUS_NEXT,
            detail=f"已配置 {snapshot.organize_target_count}/4 类目标目录",
            action="填写目标目录" if not targets_ready else "继续",
        ),
        OnboardingStep(
            key="organize_preview",
            title="生成整理预览",
            status=STATUS_DONE if preview_ready else (STATUS_NEXT if roots_ready and targets_ready else STATUS_WAITING),
            detail=f"预览中有 {snapshot.organize_preview_count} 个文件" if preview_ready else "执行前先生成预览",
            action="预览整理结果" if roots_ready and targets_ready and not preview_ready else "继续",
        ),
        OnboardingStep(
            key="profile",
            title="保存整理方案",
            status=STATUS_DONE if snapshot.profile_count else (STATUS_RECOMMENDED if roots_ready else STATUS_WAITING),
            detail=f"已保存 {snapshot.profile_count} 个方案" if snapshot.profile_count else "常用目录和规则可保存为方案",
            action="保存方案" if roots_ready and not snapshot.profile_count else "继续",
        ),
    ]


def onboarding_summary(steps: list[OnboardingStep]) -> str:
    completed = sum(1 for step in steps if step.completed)
    next_step = next((step for step in steps if step.status in {STATUS_NEXT, STATUS_RECOMMENDED}), None)
    if next_step is None:
        return f"整理流程已完成 {completed}/{len(steps)} 步。"
    return f"整理流程已完成 {completed}/{len(steps)} 步，当前建议：{next_step.title}。"


def _protection_status(snapshot: OnboardingSnapshot) -> str:
    if snapshot.protected_root_count > 0:
        return STATUS_DONE
    if snapshot.scan_root_count > 0:
        return STATUS_RECOMMENDED
    return STATUS_WAITING
