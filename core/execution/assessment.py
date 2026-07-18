from __future__ import annotations

import os
import re
from collections import Counter
from pathlib import Path

from .models import (
    ExecutionStrategy,
    TaskAssessment,
    TaskComplexity,
    TaskIntent,
    TaskRisk,
    WorkspaceProfile,
)


IGNORED_DIRECTORIES = frozenset({
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".sca",
    ".venv",
    ".worktrees",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
    "tmp",
    "vendor",
})
SOURCE_EXTENSIONS = frozenset({
    ".c", ".cc", ".cpp", ".cs", ".css", ".go", ".h", ".hpp", ".html",
    ".java", ".js", ".jsx", ".kt", ".kts", ".php", ".py", ".rb", ".rs",
    ".scala", ".sh", ".sql", ".swift", ".ts", ".tsx", ".vue",
})
LANGUAGE_BY_EXTENSION = {
    ".c": "C", ".cc": "C++", ".cpp": "C++", ".cs": "C#", ".css": "CSS",
    ".go": "Go", ".h": "C/C++", ".hpp": "C++", ".html": "HTML",
    ".java": "Java", ".js": "JavaScript", ".jsx": "JavaScript", ".kt": "Kotlin",
    ".kts": "Kotlin", ".php": "PHP", ".py": "Python", ".rb": "Ruby",
    ".rs": "Rust", ".scala": "Scala", ".sh": "Shell", ".sql": "SQL",
    ".swift": "Swift", ".ts": "TypeScript", ".tsx": "TypeScript", ".vue": "Vue",
}
PATH_PATTERN = re.compile(
    r"(?<![\w])(?:[\w.@+-]+[\\/])+[\w.@+\-]+(?:\.[A-Za-z0-9]+)?"
    r"|(?<![\w])(?:[\w@+-]+\.)+(?:py|js|jsx|ts|tsx|go|rs|java|kt|cs|cpp|c|h|"
    r"rb|php|swift|scala|sql|sh|toml|yaml|yml|json|md|txt)(?![\w])",
    re.IGNORECASE,
)


def _contains_term(text: str, term: str) -> bool:
    if not term.isascii():
        return term in text
    return re.search(
        rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])",
        text,
    ) is not None


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(_contains_term(text, term) for term in terms)


def _is_small_talk(text: str) -> bool:
    compact = re.sub(r"[\s.!?,;:\uFF0C\u3002\uFF01\uFF1F\u3001~\uFF5E]+", "", text)
    greetings = {
        "hello",
        "hi",
        "hey",
        "thanks",
        "thankyou",
        "goodmorning",
        "goodafternoon",
        "goodevening",
        "\u4f60\u597d",
        "\u60a8\u597d",
        "\u55e8",
        "\u54c8\u55bd",
        "\u54c8\u7f57",
        "\u5728\u5417",
        "\u8c22\u8c22",
        "\u65e9\u4e0a\u597d",
    }
    return compact in greetings


class TaskAssessor:
    """Create a bounded, deterministic assessment before Planner execution."""

    def __init__(self, workspace_dir: str | Path, *, max_files: int = 2500) -> None:
        if max_files < 1:
            raise ValueError("max_files must be positive")
        self.workspace_dir = Path(workspace_dir).resolve()
        self.max_files = max_files

    def assess(self, prompt: str) -> TaskAssessment:
        normalized = " ".join(prompt.lower().split())
        profile = self.profile_workspace()
        explicit_paths = self._explicit_paths(prompt)
        intent = self._classify_intent(normalized)
        risk, risk_reasons = self._classify_risk(normalized, intent)
        complexity, complexity_reasons = self._classify_complexity(
            normalized,
            explicit_paths,
            profile,
        )
        strategy = self._select_strategy(
            intent=intent,
            complexity=complexity,
            risk=risk,
            has_explicit_paths=bool(explicit_paths),
            has_quality_gates=profile.has_quality_gates,
        )
        reasons = self._build_reasons(
            intent=intent,
            strategy=strategy,
            explicit_paths=explicit_paths,
            profile=profile,
            risk_reasons=risk_reasons,
            complexity_reasons=complexity_reasons,
        )
        max_actors = 1 if strategy is not ExecutionStrategy.SCOUT_THEN_DAG else 4
        verifier_recommended = risk is not TaskRisk.LOW or (
            complexity is not TaskComplexity.SMALL
            and not profile.has_quality_gates
        )
        return TaskAssessment(
            intent=intent,
            complexity=complexity,
            risk=risk,
            strategy=strategy,
            reasons=reasons,
            explicit_paths=explicit_paths,
            workspace=profile,
            max_actors=max_actors,
            verifier_recommended=verifier_recommended,
            requires_human_approval=risk is TaskRisk.HIGH,
        )

    def profile_workspace(self) -> WorkspaceProfile:
        file_count = 0
        source_file_count = 0
        test_file_count = 0
        scan_truncated = False
        language_counts: Counter[str] = Counter()

        if self.workspace_dir.is_dir():
            for root, dirs, files in os.walk(self.workspace_dir, followlinks=False):
                dirs[:] = sorted(
                    name for name in dirs
                    if name not in IGNORED_DIRECTORIES
                    and not (Path(root) / name).is_symlink()
                )
                for filename in sorted(files):
                    file_count += 1
                    suffix = Path(filename).suffix.lower()
                    if suffix in SOURCE_EXTENSIONS:
                        source_file_count += 1
                        language_counts[LANGUAGE_BY_EXTENSION[suffix]] += 1
                    lowered = filename.lower()
                    if (
                        lowered.startswith("test_")
                        or lowered.endswith("_test.py")
                        or ".test." in lowered
                        or ".spec." in lowered
                    ):
                        test_file_count += 1
                    if file_count >= self.max_files:
                        scan_truncated = True
                        break
                if scan_truncated:
                    break

        top_level_dirs: tuple[str, ...] = ()
        try:
            top_level_dirs = tuple(sorted(
                entry.name for entry in self.workspace_dir.iterdir()
                if entry.is_dir() and entry.name not in IGNORED_DIRECTORIES
            ))
        except OSError:
            pass

        languages = tuple(
            name for name, _ in sorted(
                language_counts.items(),
                key=lambda item: (-item[1], item[0]),
            )
        )
        return WorkspaceProfile(
            file_count=file_count,
            source_file_count=source_file_count,
            test_file_count=test_file_count,
            top_level_dirs=top_level_dirs,
            languages=languages,
            has_git=(self.workspace_dir / ".git").exists(),
            has_quality_gates=(self.workspace_dir / ".sca" / "quality-gates.toml").is_file(),
            scan_truncated=scan_truncated,
        )

    def _explicit_paths(self, prompt: str) -> tuple[str, ...]:
        paths = {
            match.group(0).strip("`'\".,:;()[]{}")
            for match in PATH_PATTERN.finditer(prompt)
        }
        return tuple(sorted(
            path for path in paths
            if path and self._looks_like_workspace_path(path)
        ))

    def _looks_like_workspace_path(self, raw_path: str) -> bool:
        path = raw_path.replace("\\", "/").lstrip("./")
        lowered = path.lower()
        if (
            "://" in lowered
            or lowered.startswith(("github.com/", "www.", "usr/", "lib/"))
            or "/site-packages/" in lowered
            or "/python3." in lowered
        ):
            return False
        if re.match(r"^[a-z]:/", lowered):
            return False
        candidate = (self.workspace_dir / path).resolve()
        try:
            if candidate.exists() and candidate.is_relative_to(self.workspace_dir):
                return True
        except OSError:
            return False
        first_part = path.split("/", 1)[0]
        try:
            top_level = {
                entry.name for entry in self.workspace_dir.iterdir()
                if entry.is_dir() and entry.name not in IGNORED_DIRECTORIES
            }
            root_files = {
                entry.name for entry in self.workspace_dir.iterdir()
                if entry.is_file()
            }
        except OSError:
            return "/" not in path
        return first_part in top_level or path in root_files

    @staticmethod
    def _classify_intent(text: str) -> TaskIntent:
        change_terms = (
            "add", "build", "change", "create", "delete", "edit", "fix", "implement",
            "refactor", "remove", "rename", "update", "write", "修复", "修改", "新增",
            "实现", "重构", "更新", "创建", "删除", "编写", "改造",
        )
        operations_terms = (
            "deploy", "install", "release", "rollback", "upgrade dependency", "migration",
            "部署", "安装", "发布", "回滚", "升级依赖", "迁移数据库",
        )
        documentation_terms = (
            "documentation", "readme", "changelog", "文档", "说明书",
        )
        test_change_terms = (
            "add test", "add tests", "write test", "write tests", "update test",
            "update tests", "add unit test", "add unit tests", "write unit test",
            "write unit tests", "test coverage", "新增测试", "编写测试", "更新测试", "测试覆盖率",
        )
        read_only_terms = (
            "analyze", "explain", "find", "inspect", "review", "summarize", "what is",
            "why", "分析", "解释", "查找", "检查", "审查", "总结", "是什么", "为什么",
            "给出建议", "制定计划",
        )
        has_change = _contains_any(text, change_terms)
        if not has_change and _is_small_talk(text):
            return TaskIntent.READ_ONLY
        if not has_change and _contains_any(text, read_only_terms):
            return TaskIntent.READ_ONLY
        if _contains_any(text, operations_terms):
            return TaskIntent.OPERATIONS
        if has_change and _contains_any(text, test_change_terms):
            return TaskIntent.TEST_CHANGE
        if has_change and _contains_any(text, documentation_terms):
            return TaskIntent.DOCUMENTATION
        return TaskIntent.CODE_CHANGE

    @staticmethod
    def _classify_risk(text: str, intent: TaskIntent) -> tuple[TaskRisk, tuple[str, ...]]:
        high_terms = (
            "production", "deploy", "release", "delete", "drop table", "migration",
            "credential", "secret", "permission", "authentication", "authorization",
            "git reset", "git clean", "rm -rf", "生产", "部署", "发布", "删除",
            "数据库迁移", "凭证", "密钥", "权限", "认证", "授权",
        )
        medium_terms = (
            "api", "config", "concurrency", "database", "dependency", "public interface",
            "schema", "security", "配置", "并发", "数据库", "依赖", "公共接口", "安全",
        )
        if intent is TaskIntent.READ_ONLY:
            return TaskRisk.LOW, ("read-only request has no planned repository side effect",)
        matched_high = tuple(term for term in high_terms if _contains_term(text, term))
        if matched_high or intent is TaskIntent.OPERATIONS:
            detail = matched_high[:3] or ("operations task",)
            return TaskRisk.HIGH, ("high-impact signal: " + ", ".join(detail),)
        matched_medium = tuple(term for term in medium_terms if _contains_term(text, term))
        if matched_medium:
            return TaskRisk.MEDIUM, ("cross-boundary signal: " + ", ".join(matched_medium[:3]),)
        return TaskRisk.LOW, ("no elevated-risk lexical signal detected",)

    @staticmethod
    def _classify_complexity(
        text: str,
        explicit_paths: tuple[str, ...],
        profile: WorkspaceProfile,
    ) -> tuple[TaskComplexity, tuple[str, ...]]:
        large_terms = (
            "architecture", "entire project", "project-wide", "rewrite", "multiple modules",
            "end-to-end", "架构", "整个项目", "全局", "重写", "多个模块", "端到端",
        )
        medium_terms = (
            "feature", "refactor", "workflow", "integration", "across", "功能", "重构",
            "工作流", "集成", "跨模块", "改造计划",
        )
        if len(explicit_paths) >= 4 or _contains_any(text, large_terms):
            return TaskComplexity.LARGE, ("broad scope or at least four explicit paths",)
        word_count = len(text.split())
        if (
            len(explicit_paths) >= 2
            or _contains_any(text, medium_terms)
            or word_count >= 60
            or (profile.source_file_count >= 100 and not explicit_paths)
        ):
            return TaskComplexity.MEDIUM, ("multi-part, cross-cutting, or unfamiliar scope",)
        return TaskComplexity.SMALL, ("bounded request with a narrow inferred scope",)

    @staticmethod
    def _select_strategy(
        *,
        intent: TaskIntent,
        complexity: TaskComplexity,
        risk: TaskRisk,
        has_explicit_paths: bool,
        has_quality_gates: bool,
    ) -> ExecutionStrategy:
        if intent is TaskIntent.READ_ONLY:
            return ExecutionStrategy.PLANNER_DIRECT
        if risk is TaskRisk.HIGH or complexity is TaskComplexity.LARGE:
            return ExecutionStrategy.SCOUT_THEN_DAG
        if has_quality_gates:
            return ExecutionStrategy.CODER_WITH_GATES
        return ExecutionStrategy.SINGLE_ACTOR

    @staticmethod
    def _build_reasons(
        *,
        intent: TaskIntent,
        strategy: ExecutionStrategy,
        explicit_paths: tuple[str, ...],
        profile: WorkspaceProfile,
        risk_reasons: tuple[str, ...],
        complexity_reasons: tuple[str, ...],
    ) -> tuple[str, ...]:
        scope_reason = (
            f"request names {len(explicit_paths)} explicit path(s)"
            if explicit_paths
            else "request does not name an explicit path"
        )
        gate_reason = (
            "repository defines deterministic quality gates"
            if profile.has_quality_gates
            else "repository does not define deterministic quality gates"
        )
        return (
            f"intent classified as {intent.value}",
            *complexity_reasons,
            *risk_reasons,
            scope_reason,
            gate_reason,
            f"selected {strategy.value}",
        )


__all__ = ["IGNORED_DIRECTORIES", "TaskAssessor"]
