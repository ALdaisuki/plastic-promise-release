"""ReviewEngine — 代码审查编排引擎

执行结构化代码审查的三阶段管线:
  1. Prepare  — 获取 git diff + 自动化预检 + 生成审查 prompt
  2. Evaluate — 解析 LLM 审查输出为结构化 ReviewReport
  3. Apply    — 信任分调整 + 发现入池 + fix 任务创建 + 六联闭环

审查八维度:
  correctness / security / principle_alignment / test_coverage /
  code_quality / maintainability / spec_compliance / performance

与已有基础设施的集成:
  - TrustManager.boost/decay  — 信任分驱动
  - ContextEngine.register_memory — 发现入池
  - SoulLoop.post_task — 六联闭环
  - StepAuditor 评分模式 — 设计参考
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

from plastic_promise.core.constants import (
    TRUST_REVIEW_BLOCKER_PENALTY,
    TRUST_REVIEW_FAIL_DECAY,
    TRUST_REVIEW_PASS_BOOST,
    TRUST_REVIEW_REVIEWER_BOOST,
)

# ═══════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════


@dataclass
class ReviewFinding:
    """单个审查发现。

    Attributes:
        severity: 严重度 — blocker | major | minor | nit
        category: 分类 — security | correctness | performance | maintainability | ...
        file: 文件路径
        line_range: 行号范围，如 "L42-L58"
        description: 具体问题描述
        principle_id: 关联的 12 原则 ID (1-12, 0=不关联)
        suggestion: 具体修复建议
        auto_fixable: 是否可自动修复
    """

    severity: str = "minor"
    category: str = "code_quality"
    file: str = ""
    line_range: str = ""
    description: str = ""
    principle_id: int = 0
    suggestion: str = ""
    auto_fixable: bool = False

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "category": self.category,
            "file": self.file,
            "line_range": self.line_range,
            "description": self.description,
            "principle_id": self.principle_id,
            "suggestion": self.suggestion,
            "auto_fixable": self.auto_fixable,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ReviewFinding:
        return cls(
            severity=d.get("severity", "minor"),
            category=d.get("category", "code_quality"),
            file=d.get("file", ""),
            line_range=d.get("line_range", ""),
            description=d.get("description", ""),
            principle_id=d.get("principle_id", 0),
            suggestion=d.get("suggestion", ""),
            auto_fixable=d.get("auto_fixable", False),
        )


@dataclass
class ReviewReport:
    """结构化审查报告 — 严格匹配 protocol JSON 格式。

    Attributes:
        status: pass | fail
        principle_observations: {"#1": "方案是否最简...", "#3": "...", ...}
        findings: 审查发现列表
        recommendation: approve | revise | block
        summary: 审查摘要
        trust_delta: 建议的信任分调整值 (正=boost, 负=decay)
        metadata: {commit_range, files_changed, pre_check_passed, reviewer, ...}
    """

    status: str = "pass"
    principle_observations: dict = field(default_factory=dict)
    findings: list[ReviewFinding] = field(default_factory=list)
    recommendation: str = "approve"
    summary: str = ""
    trust_delta: float = 0.0
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "principle_observations": self.principle_observations,
            "findings": [f.to_dict() for f in self.findings],
            "recommendation": self.recommendation,
            "summary": self.summary,
            "trust_delta": self.trust_delta,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ReviewReport:
        findings = [ReviewFinding.from_dict(f) for f in d.get("findings", [])]
        return cls(
            status=d.get("status", "pass"),
            principle_observations=d.get("principle_observations", {}),
            findings=findings,
            recommendation=d.get("recommendation", "approve"),
            summary=d.get("summary", ""),
            trust_delta=d.get("trust_delta", 0.0),
            metadata=d.get("metadata", {}),
        )


# ═══════════════════════════════════════════════════════════════
# ReviewEngine
# ═══════════════════════════════════════════════════════════════


class ReviewEngine:
    """代码审查编排引擎。

    三阶段管线:
      1. prepare()  — 获取 diff + 预检 + 生成审查 prompt
      2. evaluate() — 解析 LLM 审查输出 → ReviewReport
      3. apply()    — 信任分调整 + 发现入池 + fix 任务

    用法:
        engine = ReviewEngine(trust_manager, context_engine)
        prep = engine.prepare("HEAD~3..HEAD")
        # ... LLM 执行审查，输出 review_text ...
        report = engine.evaluate(prep["diff_text"], prep["changed_files"],
                                 prep["pre_check_results"], review_text)
        result = engine.apply(report, author_target="pi_builder")
    """

    # 审查 prompt 模板中的 12 原则检查清单
    PRINCIPLE_CHECKLIST = [
        ("#1", "奥卡姆剃刀", "方案是否最简？有无不必要的实体、抽象层或中间步骤？"),
        ("#2", "全过程可查可透明", "每步是否有 git commit？变更历史是否可追溯？日志是否完整？"),
        ("#3", "自我审计闭环", "代码/审查是否包含根因分析？是否提炼了可迁移的教训？"),
        ("#4", "上下文驱动决策", "是否基于足够上下文做出设计决策？不足处是否标注而非猜测？"),
        ("#5", "约定优于约束", "新增的检查/约束是否经得起反事实检验？是否真正有效？"),
        ("#6", "数据流驱动", "是否追踪了真实数据流？接口契约是否基于实际数据流而非假设？"),
        ("#7", "全局下棋", "变更是否影响下游模块？上下游保护是否完整？"),
        ("#8", "与工具共舞", "是否充分利用了可用工具/lint/测试框架？有无遗漏的检查手段？"),
        ("#9", "信任分反映质量", "代码质量是否足以提升信任分？信任分是否反映了真实的交付质量？"),
        ("#10", "自演化闭环", "是否从本次变更中提炼了经验？经验是否会存入记忆池供后续复用？"),
        ("#11", "信息距离最小化", "函数/模块间的信息距离是否最小？是否避免了深层嵌套和长程依赖？"),
        ("#12", "代码即文档", "命名是否自解释？类型标注是否完整？新增接口是否有 docstring？"),
    ]

    # 安全审查清单
    SECURITY_CHECKLIST = [
        ("注入攻击", "SQL/LDAP/命令注入面，用户输入是否经过参数化或清理？"),
        ("硬编码密钥", "是否有硬编码的密码/token/API密钥？"),
        ("输入验证", "外部输入是否有完整的类型检查和边界验证？"),
        ("权限检查", "敏感操作是否有权限检查？是否遵循最小权限原则？"),
        ("错误泄露", "错误信息是否可能泄露内部实现细节或敏感数据？"),
        ("依赖安全", "新增/升级的依赖是否有已知 CVE？"),
    ]

    def __init__(
        self,
        trust_manager: Any = None,
        context_engine: Any = None,
        project_root: str | None = None,
    ) -> None:
        """初始化审查引擎。

        Args:
            trust_manager: TrustManager 实例，用于信任分调整。
                          若为 None，apply() 中的信任分操作将被跳过。
            context_engine: ContextEngine 实例，用于记忆检索和存储。
                           若为 None，记忆操作将被跳过。
            project_root: 项目根目录，用于 git 操作。默认为当前工作目录。
        """
        self._trust = trust_manager
        self._engine = context_engine
        self._project_root = project_root or os.getcwd()
        self._review_history: list[ReviewReport] = []

    # ═══════════════════════════════════════════════════════════
    # Phase 1: Prepare
    # ═══════════════════════════════════════════════════════════

    def prepare(self, commit_range: str = "HEAD~1..HEAD", spec_path: str | None = None) -> dict:
        """准备审查上下文 — 获取 diff + 运行预检 + 生成审查 prompt。

        Args:
            commit_range: git commit 范围，如 "HEAD~3..HEAD" 或 "Dev..feature"
            spec_path: spec 文件路径 (可选)，用于 spec 合规检查

        Returns:
            dict with keys:
                diff_text: git diff 完整文本
                changed_files: 变更文件列表
                pre_check_results: 自动化预检结果
                context_memories: 关联的历史审查记忆
                structured_prompt: 生成的审查 prompt
                git_available: git 是否可用
        """
        result = {
            "diff_text": "",
            "changed_files": [],
            "pre_check_results": {"tests": "unknown", "lint": "unknown"},
            "context_memories": [],
            "structured_prompt": "",
            "git_available": False,
        }

        # 1. 获取 git diff
        try:
            diff_text = self._run_git_diff(commit_range)
            changed_files = self._get_changed_files(commit_range)
            result["diff_text"] = diff_text
            result["changed_files"] = changed_files
            result["git_available"] = True
        except Exception as e:
            result["diff_text"] = f"[git diff 不可用: {e}]"
            result["changed_files"] = []
            result["git_available"] = False

        # 2. 自动化预检
        result["pre_check_results"] = self._run_pre_checks()

        # 3. 检索关联记忆
        result["context_memories"] = self._recall_review_context(result["changed_files"])

        # 4. 生成结构化审查 prompt
        result["structured_prompt"] = self.generate_review_prompt(
            diff_text=result["diff_text"],
            changed_files=result["changed_files"],
            pre_check=result["pre_check_results"],
            context_memories=result["context_memories"],
            spec_path=spec_path,
        )

        return result

    def _run_git_diff(self, commit_range: str) -> str:
        """获取 git diff 文本。

        使用 subprocess 调用 git，捕获 stdout 和 stderr。
        若 git 不可用，返回降级信息。
        """
        try:
            result = subprocess.run(
                ["git", "-C", self._project_root, "diff", commit_range],
                capture_output=True,
                text=True,
                timeout=30,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode != 0:
                # 尝试将 commit_range 作为两个独立的 ref
                parts = commit_range.split("..")
                if len(parts) == 2:
                    result = subprocess.run(
                        ["git", "-C", self._project_root, "diff", f"{parts[0]}..{parts[1]}"],
                        capture_output=True,
                        text=True,
                        timeout=30,
                        encoding="utf-8",
                        errors="replace",
                    )
            return result.stdout or "(空 diff — 无变更或 commit 范围无效)"
        except FileNotFoundError as exc:
            raise RuntimeError("git 不可用 — 请确认 git 已安装且在 PATH 中") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("git diff 超时 — commit 范围可能过大") from exc

    def _get_changed_files(self, commit_range: str) -> list:
        """获取变更的文件列表。"""
        try:
            parts = commit_range.split("..")
            if len(parts) == 2:
                result = subprocess.run(
                    [
                        "git",
                        "-C",
                        self._project_root,
                        "diff",
                        "--name-only",
                        f"{parts[0]}..{parts[1]}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    encoding="utf-8",
                    errors="replace",
                )
            else:
                result = subprocess.run(
                    ["git", "-C", self._project_root, "diff", "--name-only", commit_range],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    encoding="utf-8",
                    errors="replace",
                )
            if result.returncode == 0:
                return [f.strip() for f in result.stdout.splitlines() if f.strip()]
            return []
        except Exception:
            return []

    def _run_pre_checks(self) -> dict:
        """运行自动化预检 (lint / tests)。

        Returns:
            dict with keys: tests (passed/failed/unknown),
                           lint (passed/failed/unknown)
        """
        pre_check = {"tests": "unknown", "lint": "unknown"}

        # 检查 pytest 是否可用
        try:
            test_result = subprocess.run(
                [sys.executable, "-m", "pytest", "--co", "-q"],
                capture_output=True,
                text=True,
                timeout=30,
                encoding="utf-8",
                errors="replace",
                cwd=self._project_root,
            )
            # --co = collect-only, 快速检查测试是否可发现
            if test_result.returncode == 0:
                pre_check["tests"] = "collect_ok"
            else:
                pre_check["tests"] = "collect_failed"
        except Exception:
            pass

        # 检查是否有 Python 语法错误
        try:
            changed = self._get_changed_files("HEAD~1..HEAD")
            py_files = [f for f in changed if f.endswith(".py")]
            if py_files:
                lint_result = subprocess.run(
                    [sys.executable, "-m", "py_compile"] + py_files,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    encoding="utf-8",
                    errors="replace",
                    cwd=self._project_root,
                )
                pre_check["lint"] = "passed" if lint_result.returncode == 0 else "failed"
            else:
                pre_check["lint"] = "no_py_files"
        except Exception:
            pass

        return pre_check

    def _recall_review_context(self, changed_files: list) -> list:
        """检索关联的历史审查记忆。

        从 ContextEngine 中检索与变更文件相关的历史审查记录，
        用于提示审查者关注历史问题区域。
        """
        memories = []
        if self._engine is None:
            return memories

        try:
            # 检索最近的审查记忆
            if self._engine.memory_count > 0:
                for mem in self._engine.iter_memories():
                    mem_id = mem.get("id", "")
                    tags = mem.get("tags", [])
                    if isinstance(tags, str):
                        try:
                            tags = json.loads(tags)
                        except json.JSONDecodeError:
                            tags = []
                    if "review" in tags or "finding" in tags:
                        memories.append(
                            {
                                "id": mem_id,
                                "content": str(mem.get("content", ""))[:200],
                                "tags": tags,
                            }
                        )
                        if len(memories) >= 5:
                            break
        except Exception:
            pass

        return memories

    # ═══════════════════════════════════════════════════════════
    # Phase 2: Evaluate
    # ═══════════════════════════════════════════════════════════

    def evaluate(
        self, diff_text: str, changed_files: list, pre_check: dict, review_output: str
    ) -> ReviewReport:
        """解析 LLM 审查输出，生成结构化 ReviewReport。

        尝试 JSON 解析，失败时降级为 regex 提取关键字段。
        自动补全缺失的 principle_observations。

        Args:
            diff_text: git diff 文本
            changed_files: 变更文件列表
            pre_check: 预检结果
            review_output: LLM 审查输出 (期望为 JSON)

        Returns:
            ReviewReport — 结构化审查报告
        """
        # 尝试解析 JSON
        parsed = self._parse_review_output(review_output)

        # 提取 findings
        findings = []
        raw_findings = parsed.get("findings", [])
        if isinstance(raw_findings, list):
            for f in raw_findings:
                if isinstance(f, dict):
                    findings.append(ReviewFinding.from_dict(f))
                elif isinstance(f, str):
                    findings.append(ReviewFinding(description=f, severity="minor"))

        # 提取 principle_observations — 自动补全缺失的原则
        observations = parsed.get("principle_observations", {})
        if isinstance(observations, dict):
            for pid, _, question in self.PRINCIPLE_CHECKLIST:
                if pid not in observations:
                    observations[pid] = f"未评估 — {question}"
        else:
            observations = {}

        # 确定 status 和 recommendation
        status = parsed.get("status", "pass")
        if status not in ("pass", "fail"):
            status = "fail" if findings else "pass"

        recommendation = parsed.get("recommendation", "approve")
        # 根据 findings 严重度自动修正 recommendation
        has_blocker = any(f.severity == "blocker" for f in findings)
        has_major = any(f.severity == "major" for f in findings)
        if has_blocker:
            recommendation = "block"
        elif has_major and recommendation == "approve":
            recommendation = "revise"

        # 计算信任分 delta
        trust_delta = self._calculate_trust_delta(status, findings)

        # 生成摘要
        summary = parsed.get("summary", "")
        if not summary:
            summary = self._generate_summary(status, findings, changed_files)

        metadata = {
            "commit_range": parsed.get("metadata", {}).get("commit_range", ""),
            "files_changed": changed_files,
            "files_count": len(changed_files),
            "pre_check_passed": (
                pre_check.get("tests") == "collect_ok"
                and pre_check.get("lint") in ("passed", "no_py_files")
            ),
            "findings_count": len(findings),
            "blocker_count": sum(1 for f in findings if f.severity == "blocker"),
            "major_count": sum(1 for f in findings if f.severity == "major"),
            "reviewer": parsed.get("metadata", {}).get("reviewer", "claude"),
            "timestamp": datetime.datetime.now().isoformat(),
        }

        report = ReviewReport(
            status=status,
            principle_observations=observations,
            findings=findings,
            recommendation=recommendation,
            summary=summary,
            trust_delta=trust_delta,
            metadata=metadata,
        )

        self._review_history.append(report)
        return report

    def _parse_review_output(self, review_output: str) -> dict:
        """解析审查输出 — JSON 优先，降级到 regex 提取。

        三层降级策略:
          1. 直接 JSON 解析
          2. 提取 ```json ... ``` 代码块
          3. regex 提取关键字段
        """
        # Level 1: 直接 JSON
        try:
            return json.loads(review_output.strip())
        except (json.JSONDecodeError, ValueError):
            pass

        # Level 2: JSON 代码块
        json_block_pattern = r"```(?:json)?\s*\n?(.*?)\n?```"
        matches = re.findall(json_block_pattern, review_output, re.DOTALL)
        for match in matches:
            try:
                return json.loads(match.strip())
            except (json.JSONDecodeError, ValueError):
                continue

        # Level 3: regex 提取
        parsed = {}

        # 提取 status
        status_match = re.search(
            r'["\']status["\']\s*:\s*["\'](pass|fail)["\']', review_output, re.IGNORECASE
        )
        if status_match:
            parsed["status"] = status_match.group(1).lower()

        # 提取 recommendation
        rec_match = re.search(
            r'["\']recommendation["\']\s*:\s*["\'](approve|revise|block)["\']',
            review_output,
            re.IGNORECASE,
        )
        if rec_match:
            parsed["recommendation"] = rec_match.group(1).lower()

        # 提取 findings (数组)
        findings_match = re.search(r'["\']findings["\']\s*:\s*\[(.*?)\]', review_output, re.DOTALL)
        if findings_match:
            findings_block = findings_match.group(1)
            # 尝试提取 finding 对象
            finding_objects = re.findall(r"\{(.*?)\}", findings_block, re.DOTALL)
            parsed["findings"] = []
            for fo in finding_objects:
                finding = {}
                for field in [
                    "severity",
                    "category",
                    "file",
                    "line_range",
                    "description",
                    "suggestion",
                ]:
                    fm = re.search(rf'["\']{field}["\']\s*:\s*["\']([^"\']*)["\']', fo)
                    if fm:
                        finding[field] = fm.group(1)
                if finding:
                    parsed["findings"].append(finding)

        # 提取 principle_observations
        po_block = re.search(
            r'["\']principle_observations["\']\s*:\s*(\{.*?\})', review_output, re.DOTALL
        )
        if po_block:
            try:
                parsed["principle_observations"] = json.loads(po_block.group(1))
            except (json.JSONDecodeError, ValueError):
                parsed["principle_observations"] = {}

        # 提取 summary
        summary_match = re.search(r'["\']summary["\']\s*:\s*["\']([^"\']{10,})["\']', review_output)
        if summary_match:
            parsed["summary"] = summary_match.group(1)

        return parsed

    def _calculate_trust_delta(self, status: str, findings: list[ReviewFinding]) -> float:
        """根据审查结果计算信任分 delta。

        - pass + 无 blocker → +TRUST_REVIEW_PASS_BOOST
        - pass + 有 minor → +TRUST_REVIEW_PASS_BOOST * 0.5 (减半)
        - fail + 无 blocker → -TRUST_REVIEW_FAIL_DECAY
        - fail + 有 blocker → -(TRUST_REVIEW_FAIL_DECAY + N*BLOCKER_PENALTY)
        """
        blocker_count = sum(1 for f in findings if f.severity == "blocker")
        major_count = sum(1 for f in findings if f.severity == "major")

        if status == "pass":
            if blocker_count > 0:
                # 矛盾: pass 但有 blocker — 以 findings 为准
                return -(TRUST_REVIEW_FAIL_DECAY + blocker_count * TRUST_REVIEW_BLOCKER_PENALTY)
            elif major_count > 0:
                return TRUST_REVIEW_PASS_BOOST * 0.5
            else:
                return TRUST_REVIEW_PASS_BOOST
        else:  # fail
            delta = TRUST_REVIEW_FAIL_DECAY
            delta += blocker_count * TRUST_REVIEW_BLOCKER_PENALTY
            delta += major_count * TRUST_REVIEW_BLOCKER_PENALTY * 0.5
            return -min(delta, 0.15)  # 硬上限: 单次审查最多 -0.15

    def _generate_summary(
        self, status: str, findings: list[ReviewFinding], changed_files: list
    ) -> str:
        """自动生成审查摘要。"""
        parts = []
        parts.append(f"审查状态: {status.upper()}")
        parts.append(f"变更文件: {len(changed_files)} 个")

        if findings:
            by_severity = {}
            for f in findings:
                by_severity[f.severity] = by_severity.get(f.severity, 0) + 1
            sev_parts = [f"{count} {sev}" for sev, count in by_severity.items()]
            parts.append(f"发现: {', '.join(sev_parts)}")

            by_category = {}
            for f in findings:
                by_category[f.category] = by_category.get(f.category, 0) + 1
            cat_parts = [f"{k}({v})" for k, v in by_category.items()]
            parts.append(f"分类: {', '.join(cat_parts)}")
        else:
            parts.append("发现: 无")

        return "; ".join(parts)

    # ═══════════════════════════════════════════════════════════
    # Phase 3: Apply
    # ═══════════════════════════════════════════════════════════

    def apply(
        self,
        report: ReviewReport,
        author_target: str = "pi_builder",
        reviewer_target: str = "pi_reviewer",
    ) -> dict:
        """应用审查结果 — 信任分调整 + 发现入池 + fix 任务 + 六联闭环。

        Args:
            report: ReviewReport 实例
            author_target: 被审查 Agent 的 trust target (如 pi_builder)
            reviewer_target: 审查 Agent 的 trust target (如 pi_reviewer)

        Returns:
            dict with keys:
                trust_changes: 信任分变更详情
                memory_ids: 入池的记忆 ID 列表
                fix_tasks: 创建的 fix 任务 ID 列表
                closure: post_task 六联闭环结果 (若可用)
        """
        result = {
            "trust_changes": [],
            "memory_ids": [],
            "fix_tasks": [],
            "closure": None,
        }

        # 1. 信任分调整
        trust_result = self._apply_trust_deltas(report, author_target, reviewer_target)
        result["trust_changes"] = trust_result

        # 2. 审查发现入池
        if self._engine is not None:
            memory_ids = self._store_review_memories(report)
            result["memory_ids"] = memory_ids

        # 3. 创建 fix 任务 (fail / revise 时)
        if report.recommendation in ("revise", "block"):
            fix_tasks = self._create_fix_tasks(report, author_target)
            result["fix_tasks"] = fix_tasks

        # 4. 调用 post_task 六联闭环
        try:
            from plastic_promise.loop.soul_loop import post_task

            lesson = self._extract_lesson(report)
            improvement = self._extract_improvement(report)
            closure = post_task(
                task_description=f"审查完成: {report.status} — {report.summary[:100]}",
                git_commit=report.metadata.get("commit_range", ""),
                mode="full",
                lesson=lesson,
                improvement=improvement,
            )
            result["closure"] = {
                "cei": closure.get("cei", {}).get("score", 0),
                "trust": closure.get("trust", {}).get("score", 0),
            }
        except Exception:
            pass  # post_task 失败不阻塞审查结果应用

        return result

    def _apply_trust_deltas(
        self, report: ReviewReport, author_target: str, reviewer_target: str
    ) -> list:
        """应用信任分调整。

        Returns:
            list of {target, old, new, delta, reason}
        """
        changes = []
        if self._trust is None:
            return changes

        try:
            # 被审查者 (author)
            author_delta = report.trust_delta
            if author_delta > 0:
                old = self._trust.get(author_target)
                new_val = self._trust.boost(
                    author_delta,
                    f"审查通过: {report.summary[:80]}",
                    target=author_target,
                )
                changes.append(
                    {
                        "target": author_target,
                        "delta": author_delta,
                        "old": old,
                        "new": new_val,
                        "reason": "review_pass",
                    }
                )
            elif author_delta < 0:
                old = self._trust.get(author_target)
                new_val = self._trust.decay(
                    abs(author_delta),
                    f"审查未通过: {report.summary[:80]}",
                    target=author_target,
                )
                changes.append(
                    {
                        "target": author_target,
                        "delta": author_delta,
                        "old": old,
                        "new": new_val,
                        "reason": "review_fail",
                    }
                )

            # 审查者 (reviewer) — 完成审查总是轻微 boost
            old = self._trust.get(reviewer_target)
            new_val = self._trust.boost(
                TRUST_REVIEW_REVIEWER_BOOST,
                f"审查完成: {report.status}",
                target=reviewer_target,
            )
            changes.append(
                {
                    "target": reviewer_target,
                    "delta": TRUST_REVIEW_REVIEWER_BOOST,
                    "old": old,
                    "new": new_val,
                    "reason": "review_completed",
                }
            )
        except Exception:
            pass

        return changes

    def _store_review_memories(self, report: ReviewReport) -> list:
        """将审查报告和发现存入记忆池。

        Returns:
            list of memory_id strings
        """
        memory_ids = []
        if self._engine is None:
            return memory_ids

        ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")

        try:
            # 主审查报告
            report_id = f"review_{ts}"
            self._engine.create_ordinary_if_absent(
                {
                    "id": report_id,
                    "content": json.dumps(report.to_dict(), ensure_ascii=False),
                    "memory_type": "reflection",
                    "source": "review_engine",
                    "tags": [
                        "review",
                        "domain:reflecting",
                        f"status:{report.status}",
                        f"recommendation:{report.recommendation}",
                    ],
                    "tier": "L2",
                }
            )
            memory_ids.append(report_id)

            # 每个 finding 单独入池 (仅 blocker 和 major)
            for i, finding in enumerate(report.findings):
                if finding.severity in ("blocker", "major"):
                    finding_id = f"review_finding_{ts}_{i}"
                    self._engine.create_ordinary_if_absent(
                        {
                            "id": finding_id,
                            "content": json.dumps(finding.to_dict(), ensure_ascii=False),
                            "memory_type": "experience",
                            "source": "review_engine",
                            "tags": [
                                "review",
                                "finding",
                                f"severity:{finding.severity}",
                                f"category:{finding.category}",
                                f"principle:{finding.principle_id}",
                            ],
                            "tier": "L1",
                        }
                    )
                    memory_ids.append(finding_id)

        except Exception:
            pass

        return memory_ids

    def _create_fix_tasks(self, report: ReviewReport, author_target: str) -> list:
        """为审查发现创建 fix 任务。

        Returns:
            list of fix task dicts {id, description, severity}
        """
        fix_tasks = []
        if self._engine is None:
            return fix_tasks

        ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")

        try:
            for i, finding in enumerate(report.findings):
                if finding.severity not in ("blocker", "major"):
                    continue

                task_id = f"fix_task_{ts}_{i}"
                task_content = (
                    f"审查发现 [{finding.severity}] {finding.category}: "
                    f"{finding.description}\n"
                    f"文件: {finding.file} {finding.line_range}\n"
                    f"建议: {finding.suggestion}"
                )

                self._engine.create_ordinary_if_absent(
                    {
                        "id": task_id,
                        "content": task_content,
                        "memory_type": "task",
                        "source": "review_engine",
                        "tags": [
                            "task:pending",
                            f"assignee:{author_target}",
                            "domain:fixing",
                            "type:fix_review_finding",
                            f"severity:{finding.severity}",
                            f"ts:{ts}",
                        ],
                        "tier": "L1",
                    }
                )
                fix_tasks.append(
                    {
                        "id": task_id,
                        "description": task_content[:100],
                        "severity": finding.severity,
                    }
                )
        except Exception:
            pass

        return fix_tasks

    def _extract_lesson(self, report: ReviewReport) -> str:
        """从审查报告中提取经验教训。"""
        lessons = []
        for finding in report.findings:
            if finding.severity in ("blocker", "major") and finding.description:
                lessons.append(f"[{finding.category}] {finding.description[:120]}")
        if lessons:
            return "审查教训: " + " | ".join(lessons[:3])
        if report.status == "pass":
            return f"审查通过 — 代码质量良好 ({report.summary[:100]})"
        return f"审查发现 {len(report.findings)} 个问题需修复"

    def _extract_improvement(self, report: ReviewReport) -> str:
        """从审查报告中提取改良措施。"""
        improvements = []
        for finding in report.findings:
            if finding.suggestion and finding.severity in ("blocker", "major"):
                improvements.append(f"[{finding.file}] {finding.suggestion[:120]}")
        if improvements:
            return "修复建议: " + " | ".join(improvements[:3])
        if report.status == "pass":
            return "无阻塞问题，可合入"
        return f"修复 {sum(1 for f in report.findings if f.severity in ('blocker', 'major'))} 个严重问题后重新审查"

    # ═══════════════════════════════════════════════════════════
    # 审查 Prompt 生成
    # ═══════════════════════════════════════════════════════════

    def generate_review_prompt(
        self,
        diff_text: str,
        changed_files: list,
        pre_check: dict,
        context_memories: list,
        spec_path: str | None = None,
    ) -> str:
        """生成结构化审查 prompt。

        这是 Pi worker 和 Claude Code 共享的审查 prompt 模板。
        包含: 审查方法论 + 12原则检查清单 + 安全审查 + 输出格式要求。

        Args:
            diff_text: git diff 文本
            changed_files: 变更文件列表
            pre_check: 预检结果
            context_memories: 关联的历史审查记忆
            spec_path: spec 文件路径 (可选)

        Returns:
            结构化审查 prompt 字符串
        """
        # 截断过大的 diff (保留前 2000 行)
        diff_lines = diff_text.splitlines()
        if len(diff_lines) > 2000:
            diff_text = "\n".join(diff_lines[:2000]) + (
                f"\n\n... (diff 过长，已截断。共 {len(diff_lines)} 行，"
                f"显示前 2000 行。请聚焦关键变更)"
            )

        parts = []

        # ── Header ──
        parts.append("""# 代码审查任务

你是 Plastic Promise 多 Agent 团队的代码审查员 (Pi Reviewer, domain=reflecting)。
请对以下变更执行结构化代码审查，输出严格的 JSON 格式报告。

## 审查方法论

1. **通读 diff** — 理解变更的整体意图和数据流
2. **逐文件审查** — 每个变更文件对照以下检查清单
3. **安全扫描** — 逐项检查安全清单
4. **原则对齐** — 对照 13 条原则逐条评估
5. **输出报告** — 严格按 JSON Schema 输出
""")

        # ── Pre-check results ──
        parts.append("## 自动化预检结果\n")
        parts.append(f"- 测试收集: {pre_check.get('tests', 'unknown')}")
        parts.append(f"- 语法检查: {pre_check.get('lint', 'unknown')}")
        if pre_check.get("tests") == "collect_failed":
            parts.append("!!! 测试收集失败 — 请检查测试代码是否有语法错误")
        if pre_check.get("lint") == "failed":
            parts.append("!!! 语法检查失败 — 存在无法通过编译的 .py 文件")

        # ── Changed files ──
        parts.append("\n## 变更文件\n")
        if changed_files:
            for f in changed_files:
                parts.append(f"- `{f}`")
        else:
            parts.append("(无可识别的变更文件 — git diff 可能不可用)")

        # ── Context memories ──
        if context_memories:
            parts.append("\n## 历史审查记录\n")
            parts.append(
                "以下是与变更文件相关的历史审查发现，请特别关注之前出现过的问题是否已修复:\n"
            )
            for mem in context_memories[:5]:
                content = str(mem.get("content", ""))[:150]
                tags = mem.get("tags", [])
                parts.append(f"- [{','.join(tags[:3])}] {content}")

        # ── Git Diff ──
        parts.append("\n## Git Diff\n")
        parts.append("```diff")
        parts.append(diff_text)
        parts.append("```")

        # ── 12 原则检查清单 ──
        parts.append("\n## 12 原则逐条检查清单\n")
        parts.append("对每条原则，在 `principle_observations` 中给出具体评估（非走形式）:\n")
        for pid, name, question in self.PRINCIPLE_CHECKLIST:
            parts.append(f"- **{pid} {name}**: {question}")

        # ── 安全审查清单 ──
        parts.append("\n## 安全审查清单\n")
        for title, question in self.SECURITY_CHECKLIST:
            parts.append(f"- **{title}**: {question}")

        # ── Output format ──
        parts.append("""
## 输出格式

严格输出以下 JSON 格式（不要输出任何非 JSON 内容）:

```json
{
  "status": "pass | fail",
  "principle_observations": {
    "#1": "<方案是否最简？有无不必要的实体或过度抽象？>",
    "#2": "<每步是否有 git commit？变更历史是否可追溯？>",
    "#3": "<审查报告本身是否包含了根因分析和改良建议？>",
    "#4": "<是否基于足够上下文做决策？不足处是否标注？>",
    "#5": "<新增检查/约束是否经得起反事实检验？>",
    "#6": "<是否追踪了真实数据流？接口契约是否基于实际数据流？>",
    "#7": "<变更是否影响下游模块？上下游保护是否完整？>",
    "#8": "<是否充分利用了可用工具/lint/测试？有无遗漏？>",
    "#9": "<代码质量是否足以提升信任分？>",
    "#10": "<是否从本次变更中提炼了可复用的经验？>",
    "#11": "<函数/模块间的信息距离是否最小化？>",
    "#12": "<命名是否自解释？类型标注是否完整？新增接口是否有 docstring？>"
  },
  "findings": [
    {
      "severity": "blocker | major | minor | nit",
      "category": "security | correctness | performance | maintainability | code_quality | test_coverage | spec_compliance | principle_alignment",
      "file": "<文件路径>",
      "line_range": "<L起始-L结束>",
      "description": "<具体问题描述 — 必须具体，不可泛泛而谈>",
      "principle_id": "<关联原则 ID 1-12, 0=不关联>",
      "suggestion": "<具体的、可执行的修复建议>",
      "auto_fixable": false
    }
  ],
  "recommendation": "approve | revise | block",
  "summary": "<一段话总结审查结果>"
}
```

### 严重度定义:
- **blocker**: 安全漏洞、数据丢失风险、生产环境崩溃风险 — 必须立即修复
- **major**: 逻辑错误、性能严重退化、测试缺失、原则严重违反 — 合并前必须修复
- **minor**: 代码风格、命名改进、小重构建议 — 可后续修复
- **nit**: 拼写错误、注释格式 — 非阻塞

### recommendation 定义:
- **approve**: 无 blocker 和 major 问题，可直接合入
- **revise**: 存在 major 问题，修复后重新审查
- **block**: 存在 blocker，严禁合入

审查必须诚实、具体。不确定的地方标注不确定，不要猜测。走形式的审查比不审查更有害。
""")

        return "\n".join(parts)

    # ═══════════════════════════════════════════════════════════
    # 查询接口
    # ═══════════════════════════════════════════════════════════

    def get_history(self, limit: int = 10) -> list:
        """获取最近的审查历史。"""
        return [
            {
                "status": r.status,
                "recommendation": r.recommendation,
                "summary": r.summary[:150],
                "findings_count": len(r.findings),
                "trust_delta": r.trust_delta,
                "timestamp": r.metadata.get("timestamp", ""),
            }
            for r in self._review_history[-limit:]
        ]

    def get_stats(self) -> dict:
        """获取审查统计摘要。"""
        if not self._review_history:
            return {"total": 0, "pass_rate": 0, "avg_findings": 0}

        total = len(self._review_history)
        passed = sum(1 for r in self._review_history if r.status == "pass")
        total_findings = sum(len(r.findings) for r in self._review_history)

        return {
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "pass_rate": round(passed / max(total, 1), 2),
            "avg_findings": round(total_findings / max(total, 1), 1),
            "total_findings": total_findings,
        }
