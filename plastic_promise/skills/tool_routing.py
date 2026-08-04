"""Project-aware routing across Matt Pocock skills and Plastic Promise MCP tools."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html import escape
from typing import Any

from plastic_promise.core.official_workflow import (
    OFFICIAL_ROUTES,
    OFFICIAL_SKILLS,
    UPSTREAM_SKILLS_REPOSITORY,
    UPSTREAM_SKILLS_REVISION,
)

USER_INVOKED_SKILLS = frozenset(
    name for name, skill in OFFICIAL_SKILLS.items() if skill.authority == "user"
)
MODEL_INVOKED_SKILLS = frozenset(
    name for name, skill in OFFICIAL_SKILLS.items() if skill.authority == "model"
)
ENGINEERING_SKILLS = USER_INVOKED_SKILLS | MODEL_INVOKED_SKILLS

OFFICIAL_WORKFLOW_ROUTES: dict[str, dict[str, Any]] = {
    route_id: {
        "label": route.label,
        "summary": route.summary,
        "stages": list(route.stages),
        "branches": route.branch_map(),
    }
    for route_id, route in OFFICIAL_ROUTES.items()
}

_EXPLICIT_SKILL_ROUTES = {
    "setup-matt-pocock-skills": "setup",
    "ask-matt": "routing",
    "grill-with-docs": "idea-to-ship",
    "grill-me": "grill-me",
    "grilling": "grilling",
    "to-spec": "spec-to-ship",
    "to-tickets": "tickets-to-ship",
    "implement": "implement-to-review",
    "tdd": "tdd-to-review",
    "code-review": "review",
    "diagnosing-bugs": "bug-onramp",
    "prototype": "prototype",
    "research": "research-feed",
    "triage": "triage-to-ship",
    "wayfinder": "wayfinder-to-ship",
    "improve-codebase-architecture": "architecture-feed",
    "domain-modeling": "domain-modeling",
    "codebase-design": "codebase-design",
    "resolving-merge-conflicts": "merge-conflict",
    "handoff": "handoff",
    "teach": "teach",
    "writing-great-skills": "writing-great-skills",
}


def _canonical_skill_pattern(skill_name: str) -> re.Pattern[str]:
    canonical_name = r"(?:-|\s+)".join(re.escape(part) for part in skill_name.split("-"))
    return re.compile(
        rf"^\s*(?:(?:please)\s+|请\s*)?/{canonical_name}(?![A-Za-z0-9_-])",
        re.IGNORECASE,
    )


def _plain_command_pattern(command: str) -> re.Pattern[str]:
    return re.compile(
        rf"^\s*(?:(?:please)\s+|请\s*)?{command}(?![A-Za-z0-9_-])",
        re.IGNORECASE,
    )


_SLASH_ROUTE_PATTERNS = tuple(
    (_EXPLICIT_SKILL_ROUTES[skill_name], _canonical_skill_pattern(skill_name))
    for skill_name in sorted(_EXPLICIT_SKILL_ROUTES, key=len, reverse=True)
)
_PLAIN_ROUTE_PATTERNS = (
    (
        "setup",
        _plain_command_pattern(r"setup(?:-|\s+)matt(?:-|\s+)pocock(?:-|\s+)skills"),
    ),
    ("routing", _plain_command_pattern(r"ask(?:-|\s+)matt")),
    (
        "idea-to-ship",
        _plain_command_pattern(r"grill(?:-|\s+)with(?:-|\s+)docs"),
    ),
    (
        "idea-to-ship",
        _plain_command_pattern(
            r"(?:可以\s*)?(?:使用|采用|启动|按|走)(?:这套|一套)?"
            r"完整(?:的)?(?:开发|工程)(?:链|流程)(?:进行)?"
        ),
    ),
    (
        "idea-to-ship",
        _plain_command_pattern(
            r"(?:use|start|follow)\s+(?:the\s+)?(?:complete|full)\s+"
            r"(?:development|engineering)\s+(?:chain|workflow)"
        ),
    ),
    ("grill-me", _plain_command_pattern(r"grill(?:-|\s+)me")),
    (
        "writing-great-skills",
        _plain_command_pattern(r"writing(?:-|\s+)great(?:-|\s+)skills"),
    ),
    (
        "architecture-feed",
        _plain_command_pattern(r"improve(?:-|\s+)codebase(?:-|\s+)architecture"),
    ),
    ("handoff", _plain_command_pattern("handoff")),
    ("teach", _plain_command_pattern("teach")),
    ("triage-to-ship", _plain_command_pattern("triage")),
    ("wayfinder-to-ship", _plain_command_pattern("wayfinder")),
    ("domain-modeling", _plain_command_pattern(r"domain(?:-|\s+)model(?:ing)?")),
    ("domain-modeling", _plain_command_pattern(r"领域(?:建模|模型)")),
    ("codebase-design", _plain_command_pattern("代码库设计")),
    ("handoff", _plain_command_pattern("交接文档")),
    ("teach", _plain_command_pattern("教学工作区")),
    ("triage-to-ship", _plain_command_pattern("问题分诊")),
    ("prototype", _plain_command_pattern("原型")),
)

_COMMAND_DIRECT_NEGATION_RE = re.compile(
    r"^[\s:：,，-]*(?:do\s+not|don['’]?t|should\s+not|shouldn['’]?t|must\s+not|"
    r"mustn['’]?t|cannot|can['’]?t|won['’]?t|never|not\s+(?:invoke|run|use|start|"
    r"trigger|call|execute))\b|"
    r"^[\s:：,，-]*(?:不要|无需|不需要|不能|不应|不该|别(?:再)?|禁止|已?禁用|"
    r"停用|取消|未(?:调用|运行|触发|执行)|没(?:有)?(?:调用|运行|触发|执行))",
    re.IGNORECASE,
)
_COMMAND_DECLARATION_RE = re.compile(
    r"^[\s:：,，-]*(?:wasn['’]?t|weren['’]?t|isn['’]?t|aren['’]?t|hasn['’]?t|"
    r"haven['’]?t|hadn['’]?t|was|were|is|are|has|have|had|appears?|appeared|completed|"
    r"succeeded|failed|finished|stopped|remains?|seems?|became|becomes?|previously|"
    r"currently|already|recently|ran|run|invoked|executed|triggered|called|used|"
    r"started|should|would|could|can|must)\b|"
    r"^[\s:：,，-]*(?:command|skill|workflow|hook|stage)\b.*\b(?:is|was|has|have|"
    r"failed|completed|disabled|enabled|missing|removed)\b|"
    r"^[\s:：,，-]*(?:刚刚|刚才|已经|曾经|此前|当前|目前).*(?:调用|运行|触发|执行|"
    r"完成|失败|启用|禁用|关闭)|"
    r"^[\s:：,，-]*(?:功能|命令|技能|工作流|流程|阶段).*(?:已|未|没有|状态|失败|"
    r"完成|关闭|启用|禁用|缺失|删除)",
    re.IGNORECASE,
)
_COMMAND_MENTION_RE = re.compile(
    r"^[\s:：,，-]*(?:means?|refers?\s+to|unavailable)\b|"
    r"^[\s:：,，-]*(?:command|skill|workflow|hook|stage)\b.*\b(?:appears?|"
    r"means?|refers?|mentions?|is|was)\b|"
    r"^[\s:：,，-]*(?:只是|仅是|不过是).*(?:功能名|名称|术语|引用)|"
    r"^[\s:：,，-]*(?:昨天|今日|今天).*(?:调用|运行|触发|执行)(?:过|了)?|"
    r"^[\s:：,，-]*是(?:个|一个)?(?:功能名|名称|术语)|"
    r"^[\s:：,，-]*(?:当前|目前).*(?:不可用|未启动|没有启动|关闭|禁用)|"
    r"^[\s:：,，-]*没有启动|"
    r"^[\s:：,，-]*(?:的)?(?:调用次数|调用量|状态|功能|名称).*(?:是|为|没有|零|"
    r"失败|完成|关闭|启用|禁用)",
    re.IGNORECASE,
)
_COMMAND_QUESTION_PREFIX_RE = re.compile(
    r"^[\s:：,，-]*(?:why|how|what|when|where|who|whom|whose|which|whether|can|"
    r"could|would|should|did|does|do|is|are|was|were|has|have|had)\b|"
    r"^[\s:：,，-]*(?:为什么|为何|怎么|如何|是否|能否|可否|要不要|有没有|是不是)",
    re.IGNORECASE,
)
_COMMAND_ARGUMENT_QUESTION_RE = re.compile(
    r"^[\s:：,，-]*(?:(?:me|us|on|about)\s+)?(?:how|what|when|where|whether|why)\b",
    re.IGNORECASE,
)
_COMMAND_TEACH_QUESTION_RE = re.compile(
    r"^[\s:：,，-]*(?:(?:me|us)\s*[:：,，-]\s*)?"
    r"(?:can|could|would|should|did|does|do|is|are|was|were|has|have|had)\b"
    r".*[?？]\s*$",
    re.IGNORECASE,
)
_COMMAND_INLINE_QUESTION_RE = re.compile(
    r"^[\s:：,，-]*(?:can|could|would|should|did|does|do|is|are|was|were|has|have|had)\b"
    r".*[?？]\s*$",
    re.IGNORECASE,
)
_COMMAND_TOPIC_QUESTION_RE = re.compile(
    r"^[\s:：,，-]*(?:on|about)\s+\S.*[?？]\s*$",
    re.IGNORECASE,
)
_COMMAND_SUBJECT_STATUS_RE = re.compile(
    r"^[\s:：,，-]*(?:[A-Za-z0-9_-]+\s+){1,8}"
    r"(?:is|are|was|were|has|have|had|means?|refers?|represents?|appears?|seems?|completed|"
    r"failed|finished|generated|happened|invoked|occurred|ran|started|stopped|"
    r"remains?|succeeded|triggered)\b",
    re.IGNORECASE,
)
_COMMAND_CHINESE_STATUS_RE = re.compile(
    r"^[\s:：,，-]*.{1,40}(?:已经|当前|此前|曾经|目前|昨天|今日|今天|刚刚|已).{0,20}"
    r"(?:关闭|发布|完成|失败|归档|禁用|结束|触发|调用|运行|执行)",
)
_COMMAND_STATUS_PREDICATE_RE = re.compile(
    r"(?:^|\s)(?:isn['’]?t|aren['’]?t|wasn['’]?t|weren['’]?t|might|may|"
    r"became|becomes?|denotes?|means?|refers?|represents?|remains?|seems?|appears?|"
    r"completed|disabled|enabled|failed|finished|invoked|offline|published|"
    r"required|stale|status|succeeded|unavailable|unresolved)(?:\s|$)|"
    r"(?:不是|不用|无需|不需要|未|没有|还是|当前|目前|已经|状态|完成|失败|"
    r"禁用|不可用|离线|过期)",
    re.IGNORECASE,
)
_COMMAND_DISJUNCTION_RE = re.compile(r"^[\s:：,，-]*(?:or\b|还是)", re.IGNORECASE)
_COMMAND_ARGUMENT_RE = re.compile(
    r"^[\s:：,，-]*(?:$|(?:[A-Za-z0-9_]|[\u4e00-\u9fff]).*)",
    re.IGNORECASE,
)
_COMMAND_SCOPED_NEGATION_RE = re.compile(
    r"\bnot\s+(?:now|today|yet|this\s+time)\b|(?:暂时不|先不|现在不|目前不)",
    re.IGNORECASE,
)
_PLAIN_USER_ARGUMENTS = {
    "routing": re.compile(r"^(?:about|how|what|when|where|whether|why|to)\b", re.IGNORECASE),
    "idea-to-ship": re.compile(r"^(?:on|about|this|that|these|those|the|my|our)\b", re.IGNORECASE),
    "grill-me": re.compile(
        r"^(?:harder|on|about|why|how|what|this|that|the|my|our)\b|^[A-Za-z]+ly$",
        re.IGNORECASE,
    ),
    "writing-great-skills": re.compile(
        r"^(?:review|write|create|improve|this|that|the)\b", re.IGNORECASE
    ),
    "wayfinder-to-ship": re.compile(r"^(?:for|in|through|this|that|the|my|our)\b", re.IGNORECASE),
    "architecture-feed": re.compile(r"^(?:for|in|of|this|that|the|my|our)\b", re.IGNORECASE),
    "setup": re.compile(r"^(?:for|in|here|now)\b", re.IGNORECASE),
    "teach": re.compile(
        r"^(?:me|us|this|that|these|those)\b|^[A-Za-z0-9_.+#-]+$|"
        r"^(?-i:[A-Z])[A-Za-z0-9_.+#-]+\s+\S+|^[A-Za-z]+(?:al|ic|ive|ing)\s+\S+|"
        r"^(?:给我|给我们|帮我|教我|教我们|讲解|带我)",
        re.IGNORECASE,
    ),
    "handoff": re.compile(
        r"^(?:this|that|everything|all|it|to|ownership)\b|^project\b.*\bto\b|"
        r"^(?:帮我|帮我们|交给|移交|整理|接着处理|继续处理)",
        re.IGNORECASE,
    ),
    "triage-to-ship": re.compile(
        r"^(?:issue|bug|ticket|incident|problem|request|pr|this|that)\b|"
        r"^(?:问题|故障|工单|事件|缺陷)",
        re.IGNORECASE,
    ),
}
_COMMAND_CONTEXT_QUESTION_RE = re.compile(r"(?:为什么|为何).*(?:没有|没|未|不)")
_BARE_QUESTION_RE = re.compile(r"^[\s:：,，-]*[?？]\s*$")

_GENERAL_REQUEST_PREFIX_RE = re.compile(
    r"^\s*(?:(?:please)\s+|(?:can|could|would)\s+you\s+|i\s+need\s+you\s+to\s+|"
    r"(?:instead|just)\s+|(?:请帮我|请你|帮我|而是|请|只)\s*)",
    re.IGNORECASE,
)
_GENERAL_NEGATED_CLAUSE_RE = re.compile(
    r"^\s*(?:do\s+not|don['’]?t|no\s+need\s+to|never)\b|"
    r"^\s*(?:不要|无需|不需要|禁止|别(?:再)?)",
    re.IGNORECASE,
)
_GENERAL_CLAUSE_SPLIT_RE = re.compile(
    r"[;,；，。]\s*|\b(?:and\s+then|and|but|then)\b\s*|"
    r"(?:但是|然后|并且|接着|随后|但|并)",
    re.IGNORECASE,
)
_GENERAL_NEGATED_SCOPE_RESET_RE = re.compile(
    r"[;；。]\s*(?:(?:instead|then)\s+)?|"
    r"[,，]\s*(?:(?:but|instead|then)\s+|(?:但是|而是|然后|但))|"
    r"\b(?:but|instead)\b\s*|(?:但是|而是|但)",
    re.IGNORECASE,
)
_GENERAL_SUBORDINATE_CLAUSE_RE = re.compile(
    r"\b(?:that|because|since|before|without|but|while|although|even\s+though|after|"
    r"unless|where)\b|"
    r"(?:因为|由于|之前|但是|而不|同时|虽然|之后|除非)",
    re.IGNORECASE,
)
_GENERAL_DISJUNCTION_RE = re.compile(
    r"\b(?:and|or|versus|vs\.?)\b|还是",
    re.IGNORECASE,
)
_GENERAL_AMBIGUOUS_NEGATION_RE = re.compile(
    r"\b(?:do\s+not|don['’]?t|never|not)\b|(?:不要|无需|不需要|禁止|别(?:再)?)",
    re.IGNORECASE,
)
_GENERAL_FINITE_PREDICATE_RE = re.compile(
    r"^(?:\S+\s+){1,8}(?:isn['’]?t|aren['’]?t|wasn['’]?t|weren['’]?t|won['’]?t|"
    r"doesn['’]?t|don['’]?t|hasn['’]?t|haven['’]?t|hadn['’]?t|can['’]?t|"
    r"couldn['’]?t|shouldn['’]?t|wouldn['’]?t|is|are|was|were|has|have|had|might|"
    r"may|became|becomes?|"
    r"appears?|seems?|remains?|completed|contains?|crashes?|disabled|enabled|failed|"
    r"fails?|finished|got|invoked|passed|published|succeeded|triggered|approved|"
    r"archived|closed)\b",
    re.IGNORECASE,
)
_GENERAL_LEADING_STATUS_RE = re.compile(
    r"^(?:is|are|was|were|has|have|had|might|may)\b|"
    r"^(?:completed|disabled|enabled|failed|finished|offline|passed|published|"
    r"succeeded|unavailable)(?:\s+(?:already|again|currently|previously|recently|"
    r"successfully|today|yesterday)\b|\s*[.!?]?$)",
    re.IGNORECASE,
)
_GENERAL_CHINESE_STATEMENT_RE = re.compile(
    r"^(?:报告|结果|任务|流水线|队列|服务|版本|文档|草案).*"
    r"(?:已(?:发布|完成|归档|关闭)|失败|完成|崩溃了|不可用|离线|通过审核)$"
)
_GENERAL_AMBIGUOUS_ACTIONS = frozenset(
    {"audit", "build", "change", "code", "compare", "design", "prototype", "research", "review"}
)
_GENERAL_SINGLE_OBJECT_ALLOWLIST = {
    "compare": frozenset({"alternatives", "approaches", "options"}),
    "research": frozenset({"alternatives", "approaches", "options"}),
}
_GENERAL_NOMINAL_OBJECTS = {
    "build": re.compile(r"^pipeline\b", re.IGNORECASE),
    "code": re.compile(r"^review\b", re.IGNORECASE),
    "design": re.compile(r"^patterns?(?:\s|$)", re.IGNORECASE),
    "prototype": re.compile(r"^(?:notes?|dashboard|pattern|server|version)(?:\s|$)", re.IGNORECASE),
}
_GENERAL_CHINESE_NOMINAL_OBJECT_RE = re.compile(r"^(?:论文摘要|论文综述|报告摘要)$")
_GENERAL_ROUTE_REQUESTS = (
    (
        "merge-conflict",
        re.compile(
            r"^(?:continue|fix|resolve)\b(?P<object>.*\b(?:merge|rebase)\b.*\bconflict\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "merge-conflict",
        re.compile(r"^(?:继续|解决|修复|处理)(?P<object>.*(?:合并|变基).*冲突)"),
    ),
    (
        "bug-onramp",
        re.compile(
            r"^(?:debug|diagnose|fix|repair|troubleshoot)\b(?P<object>.+)",
            re.IGNORECASE,
        ),
    ),
    (
        "bug-onramp",
        re.compile(
            r"^investigate\b(?P<object>.*\b(?:bug|error|exception|failure|regression)\b.*)",
            re.IGNORECASE,
        ),
    ),
    ("bug-onramp", re.compile(r"^(?:修复|调试|诊断|排查)(?P<object>.+)")),
    (
        "review",
        re.compile(r"^(?:audit|review)\b(?P<object>.+)", re.IGNORECASE),
    ),
    ("review", re.compile(r"^(?:审查|审核|评审)(?P<object>.+)")),
    (
        "prototype",
        re.compile(r"^prototype\b(?P<object>.+)", re.IGNORECASE),
    ),
    (
        "prototype",
        re.compile(r"^(?:build|create)\s+(?P<object>(?:a\s+)?prototype(?:\s+.*)?)$", re.I),
    ),
    ("prototype", re.compile(r"^(?:制作|创建|构建)(?P<object>原型.*)")),
    (
        "research-feed",
        re.compile(r"^(?:compare|investigate|research)\b(?P<object>.+)", re.IGNORECASE),
    ),
    ("research-feed", re.compile(r"^(?:比较|调查|研究|调研)(?P<object>.+)")),
    (
        "codebase-design",
        re.compile(r"^(?:architect|design|refactor)\b(?P<object>.+)", re.IGNORECASE),
    ),
    ("codebase-design", re.compile(r"^(?:架构|设计|重构|领域建模)(?P<object>.+)")),
    (
        "tdd-to-review",
        re.compile(
            r"^(?:add|build|change|code|create|develop|implement|ship|write)\b"
            r"(?P<object>.+)",
            re.IGNORECASE,
        ),
    ),
    (
        "tdd-to-review",
        re.compile(r"^(?:新增|添加|修改|开发|实现|编码|构建|创建)(?P<object>.+)"),
    ),
)
_MODEL_ROUTE_SKILLS = {
    "merge-conflict": (["resolving-merge-conflicts", "code-review"], []),
    "bug-onramp": (["diagnosing-bugs", "tdd", "code-review"], []),
    "review": (["code-review"], []),
    "prototype": (["prototype"], []),
    "research-feed": (["research"], ["grill-with-docs"]),
    "codebase-design": (["codebase-design"], []),
    "tdd-to-review": (["tdd", "code-review"], []),
}
_SPECIFIC_COMMAND_ROUTES = frozenset(
    {"merge-conflict", "bug-onramp", "review", "prototype", "research-feed"}
)


@dataclass(frozen=True)
class WorkflowScope:
    """Exact workflow identity shared by Hook routing and MCP calls."""

    route: str
    project_id: str
    stage_session_id: str
    flow_line_id: str

    @classmethod
    def from_route(cls, route: dict[str, Any]) -> WorkflowScope:
        return cls(
            route=str(route.get("route") or ""),
            project_id=str(route.get("project_id") or ""),
            stage_session_id=str(route.get("stage_session_id") or ""),
            flow_line_id=str(route.get("flow_line_id") or ""),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "route": self.route,
            "project_id": self.project_id,
            "stage_session_id": self.stage_session_id,
            "flow_line_id": self.flow_line_id,
        }

    def escaped_json(self) -> str:
        serialized = json.dumps(self.as_dict(), ensure_ascii=False, separators=(",", ":"))
        return escape(serialized, quote=False)


@dataclass(frozen=True)
class GeneralTaskIntent:
    """A positive command clause selected from otherwise untyped Hook text."""

    route: str = ""
    command_text: str = ""

    @staticmethod
    def _is_positive_command(
        match: re.Match[str],
        *,
        allow_question: bool,
    ) -> bool:
        command = str(match.string or "").strip()
        object_phrase = str(match.groupdict().get("object") or "").strip()
        if not object_phrase or not re.search(r"[A-Za-z0-9\u4e00-\u9fff]", object_phrase):
            return False
        if not allow_question and command.endswith(("?", "？")):
            return False
        main_clause = _GENERAL_SUBORDINATE_CLAUSE_RE.split(object_phrase, maxsplit=1)[0].strip()
        if not main_clause or not re.search(r"[A-Za-z0-9\u4e00-\u9fff]", main_clause):
            return False
        if _GENERAL_DISJUNCTION_RE.search(main_clause):
            return False
        if _GENERAL_AMBIGUOUS_NEGATION_RE.search(main_clause):
            return False
        if _GENERAL_FINITE_PREDICATE_RE.search(main_clause):
            return False
        if _GENERAL_LEADING_STATUS_RE.search(main_clause):
            return False
        if _GENERAL_CHINESE_STATEMENT_RE.search(main_clause):
            return False
        if _GENERAL_CHINESE_NOMINAL_OBJECT_RE.fullmatch(main_clause):
            return False

        action = command.split(maxsplit=1)[0].casefold()
        nominal_object = _GENERAL_NOMINAL_OBJECTS.get(action)
        if nominal_object is not None and nominal_object.search(main_clause):
            return False
        if action not in _GENERAL_AMBIGUOUS_ACTIONS:
            return True
        object_tokens = re.findall(r"[A-Za-z0-9_+#.-]+|[\u4e00-\u9fff]+", main_clause)
        single_object = main_clause.casefold().strip(" .,:;!?\"'()[]{}")
        return len(object_tokens) >= 2 or single_object in _GENERAL_SINGLE_OBJECT_ALLOWLIST.get(
            action, frozenset()
        )

    @classmethod
    def parse(cls, task_description: str) -> GeneralTaskIntent:
        description = str(task_description or "").strip()
        if _GENERAL_NEGATED_CLAUSE_RE.match(description):
            candidates = [description, *_GENERAL_NEGATED_SCOPE_RESET_RE.split(description)[1:]]
        else:
            candidates = [description, *_GENERAL_CLAUSE_SPLIT_RE.split(description)]
        seen: set[str] = set()
        for candidate in candidates:
            command = candidate.strip()
            explicit_request = False
            while prefix := _GENERAL_REQUEST_PREFIX_RE.match(command):
                explicit_request = True
                command = command[prefix.end() :].lstrip()
            normalized = command.casefold()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            if _GENERAL_NEGATED_CLAUSE_RE.match(command):
                continue
            for route, pattern in _GENERAL_ROUTE_REQUESTS:
                match = pattern.search(command)
                if match is not None and cls._is_positive_command(
                    match, allow_question=explicit_request
                ):
                    return cls(route=route, command_text=command)
        return cls()


def _xml_text(value: Any) -> str:
    return escape(str(value), quote=False)


def invocation_policy(skill_name: str) -> str:
    normalized = str(skill_name or "").strip().casefold()
    skill = OFFICIAL_SKILLS.get(normalized)
    return skill.authority if skill else "unknown"


def _positive_command_intent(
    task_description: str,
    match: re.Match[str],
    *,
    route_id: str,
    slash_command: bool,
) -> bool:
    suffix = task_description[match.end() :]
    if _COMMAND_DIRECT_NEGATION_RE.search(suffix):
        return False
    if _COMMAND_SCOPED_NEGATION_RE.search(suffix):
        return False
    if route_id == "teach" and (
        _COMMAND_ARGUMENT_QUESTION_RE.search(suffix) or _COMMAND_TEACH_QUESTION_RE.search(suffix)
    ):
        return True
    if route_id == "grill-me" and (
        _COMMAND_ARGUMENT_QUESTION_RE.search(suffix)
        or _COMMAND_INLINE_QUESTION_RE.search(suffix)
        or _COMMAND_TOPIC_QUESTION_RE.search(suffix)
    ):
        return True
    if _COMMAND_DECLARATION_RE.search(suffix):
        return False
    if _COMMAND_MENTION_RE.search(suffix):
        return False
    if _COMMAND_SUBJECT_STATUS_RE.search(suffix):
        return False
    if _COMMAND_CHINESE_STATUS_RE.search(suffix):
        return False
    if _COMMAND_CONTEXT_QUESTION_RE.search(suffix):
        return False
    if not suffix.strip():
        return True
    if slash_command and _BARE_QUESTION_RE.fullmatch(suffix):
        return True
    if _BARE_QUESTION_RE.search(suffix):
        return False
    if suffix.rstrip().endswith(("?", "？")):
        return False
    if _COMMAND_DISJUNCTION_RE.search(suffix):
        return False
    if _COMMAND_STATUS_PREDICATE_RE.search(suffix):
        return False
    if _COMMAND_QUESTION_PREFIX_RE.search(suffix):
        return False
    if not slash_command:
        argument = suffix.lstrip(" \t:：,，-")
        route_arguments = _PLAIN_USER_ARGUMENTS.get(route_id)
        if route_arguments is not None:
            return route_arguments.search(argument) is not None
    return _COMMAND_ARGUMENT_RE.fullmatch(suffix) is not None


def _explicit_route(task_description: str) -> str:
    for slash_command, patterns in (
        (True, _SLASH_ROUTE_PATTERNS),
        (False, _PLAIN_ROUTE_PATTERNS),
    ):
        for route_id, pattern in patterns:
            match = pattern.search(task_description)
            if match is not None and _positive_command_intent(
                task_description,
                match,
                route_id=route_id,
                slash_command=slash_command,
            ):
                return route_id
    return ""


def _task_type_fallback_eligible(task_description: str) -> bool:
    description = str(task_description or "").strip()
    if not description or description.endswith(("?", "？")):
        return False
    return not any(
        pattern.search(description)
        for pattern in (
            _COMMAND_DIRECT_NEGATION_RE,
            _COMMAND_QUESTION_PREFIX_RE,
            _COMMAND_DECLARATION_RE,
            _COMMAND_SUBJECT_STATUS_RE,
            _COMMAND_CHINESE_STATUS_RE,
            _COMMAND_STATUS_PREDICATE_RE,
            _GENERAL_FINITE_PREDICATE_RE,
            _GENERAL_CHINESE_STATEMENT_RE,
        )
    )


def _sp_stage_arguments(
    *,
    stage: str,
    task_description: str,
    invocation_source: str,
    scope: WorkflowScope,
) -> dict[str, str]:
    arguments = {
        "stage": stage,
        "task_description": task_description[:240],
        "invocation_source": invocation_source,
        "route": scope.route,
        "project_id": scope.project_id,
    }
    if scope.stage_session_id:
        arguments["stage_session_id"] = scope.stage_session_id
    if scope.flow_line_id:
        arguments["flow_line_id"] = scope.flow_line_id
    return arguments


def recommend_tool_route(
    *,
    task_description: str,
    task_type: str,
    project_id: str,
    stage_session_id: str = "",
    flow_line_id: str = "",
    semantic_route: str = "",
) -> dict[str, Any]:
    """Return bounded route hints; never auto-select user-invoked skills."""

    description = " ".join(str(task_description or "").split())
    normalized_type = str(task_type or "general").strip().casefold()
    auto_skills: list[str] = []
    user_skills: list[str] = []
    route = "idea-to-ship"
    selection_source = "fallback"
    explicit_route = _explicit_route(description)
    general_intent = GeneralTaskIntent.parse(description)
    task_type_eligible = _task_type_fallback_eligible(description)

    if explicit_route:
        route = explicit_route
        selection_source = "explicit_command"
        explicit_stages = list(OFFICIAL_WORKFLOW_ROUTES[route]["stages"])
        auto_skills = [stage for stage in explicit_stages if invocation_policy(stage) == "model"]
        user_skills = [stage for stage in explicit_stages if invocation_policy(stage) == "user"]
    elif general_intent.route in _SPECIFIC_COMMAND_ROUTES:
        route = general_intent.route
        selection_source = "deterministic_command"
        auto_skills, user_skills = (
            list(skills) for skills in _MODEL_ROUTE_SKILLS[general_intent.route]
        )
    elif (
        semantic_route in OFFICIAL_WORKFLOW_ROUTES
        and OFFICIAL_WORKFLOW_ROUTES[semantic_route]["stages"]
        and invocation_policy(OFFICIAL_WORKFLOW_ROUTES[semantic_route]["stages"][0]) == "model"
    ):
        semantic_stages = list(OFFICIAL_WORKFLOW_ROUTES[semantic_route]["stages"])
        route = semantic_route
        selection_source = "semantic_model"
        auto_skills = [stage for stage in semantic_stages if invocation_policy(stage) == "model"]
        user_skills = [stage for stage in semantic_stages if invocation_policy(stage) == "user"]
    elif task_type_eligible and normalized_type == "debugging":
        route = "bug-onramp"
        selection_source = "task_type"
        auto_skills = ["diagnosing-bugs", "tdd", "code-review"]
    elif task_type_eligible and normalized_type == "code_review":
        route = "review"
        selection_source = "task_type"
        auto_skills = ["code-review"]
    elif task_type_eligible and normalized_type in {"architecture", "refactoring"}:
        route = "codebase-design"
        selection_source = "task_type"
        auto_skills = ["codebase-design"]
    elif task_type_eligible and normalized_type == "code_generation":
        route = "tdd-to-review"
        selection_source = "task_type"
        auto_skills = ["tdd", "code-review"]
    elif general_intent.route:
        route = general_intent.route
        selection_source = "deterministic_command"
        auto_skills, user_skills = (
            list(skills) for skills in _MODEL_ROUTE_SKILLS[general_intent.route]
        )
    else:
        route = "routing"
        user_skills = ["ask-matt"]

    auto_skills = [skill for skill in _unique(auto_skills) if invocation_policy(skill) == "model"]
    user_skills = [skill for skill in _unique(user_skills) if invocation_policy(skill) == "user"]
    flow = OFFICIAL_WORKFLOW_ROUTES[route]
    full_chain = list(flow["stages"])
    current_stage = (user_skills or full_chain)[0] if route == "idea-to-ship" else full_chain[0]
    if current_stage in full_chain:
        current_index = full_chain.index(current_stage)
        next_stage = full_chain[current_index + 1] if current_index + 1 < len(full_chain) else ""
    else:
        next_stage = full_chain[0] if full_chain else ""
    stage_authority = {
        skill: invocation_policy(skill)
        for skill in _unique([current_stage, *full_chain])
        if invocation_policy(skill) != "unknown"
    }
    scope = WorkflowScope(route, project_id, stage_session_id, flow_line_id)
    mcp_calls = [
        {
            "tool": "session-init",
            "when": "at task start; reuse this project and workflow scope",
            "arguments": {
                "task_description": description[:240],
                "context_mode": "light",
                **scope.as_dict(),
            },
        },
        {
            "tool": "memory_recall",
            "when": "before decisions that depend on prior project work",
            "arguments": {
                "query": description[:240],
                "project_id": project_id,
                "task_type": normalized_type or "general",
                "max_results": 5,
            },
        },
        {
            "tool": "defense",
            "when": "before high-risk or state-changing operations",
            "arguments": {"action": "get"},
        },
        {
            "tool": "step-closure",
            "when": "after substantive verified output",
            "arguments": {"task_description": description[:240], "mode": "full"},
        },
    ]
    current_authority = stage_authority.get(current_stage)
    if current_authority == "model" or (explicit_route and current_authority == "user"):
        mcp_calls.insert(
            1,
            {
                "tool": "sp-stage",
                "when": (
                    "after explicit user selection, request the pinned Skill execution contract"
                    if current_authority == "user"
                    else "before entering the selected model-invoked engineering skill"
                ),
                "arguments": _sp_stage_arguments(
                    stage=current_stage,
                    task_description=description,
                    invocation_source=current_authority,
                    scope=scope,
                ),
            },
        )
    return {
        "schema_version": "project-tool-route-v1",
        "project_id": scope.project_id,
        "task_description": description,
        "stage_session_id": scope.stage_session_id,
        "flow_line_id": scope.flow_line_id,
        "route": route,
        "selection_source": selection_source,
        "new_root_selected": selection_source
        in {"explicit_command", "deterministic_command", "semantic_model"},
        "starts_workflow": route != "routing",
        "flow_label": flow["label"],
        "flow_summary": flow["summary"],
        "full_chain": full_chain,
        "branches": dict(flow.get("branches") or {}),
        "current_stage": current_stage,
        "next_stage": next_stage,
        "stage_authority": stage_authority,
        "auto_skills": auto_skills[:4],
        "user_skills": user_skills[:3],
        "mcp_calls": mcp_calls,
        "upstream": {
            "repository": UPSTREAM_SKILLS_REPOSITORY,
            "revision": UPSTREAM_SKILLS_REVISION,
        },
    }


def resume_tool_route(
    route: dict[str, Any],
    *,
    route_id: str,
    completed_step_index: int,
    flow_scope_id: str,
) -> dict[str, Any]:
    """Project a durable workflow cursor onto an otherwise fresh Hook route."""
    flow = OFFICIAL_WORKFLOW_ROUTES.get(str(route_id or ""))
    if flow is None:
        return route
    full_chain = list(flow["stages"])
    next_index = max(-1, int(completed_step_index)) + 1
    current_stage = full_chain[next_index] if next_index < len(full_chain) else ""
    following_stage = full_chain[next_index + 1] if next_index + 1 < len(full_chain) else ""
    authority = {skill: invocation_policy(skill) for skill in full_chain}
    remaining = full_chain[next_index:] if current_stage else []
    auto_skills = [skill for skill in remaining if authority[skill] == "model"]
    user_skills = [skill for skill in remaining if authority[skill] == "user"]
    requested_stage_call = next(
        (
            call
            for call in list(route.get("mcp_calls") or [])
            if call.get("tool") == "sp-stage"
            and dict(call.get("arguments") or {}).get("stage") == current_stage
            and dict(call.get("arguments") or {}).get("invocation_source") == "user"
        ),
        None,
    )
    mcp_calls = [
        call for call in list(route.get("mcp_calls") or []) if call.get("tool") != "sp-stage"
    ]
    if current_stage and (authority[current_stage] == "model" or requested_stage_call):
        invocation_source = authority[current_stage]
        sp_call = {
            "tool": "sp-stage",
            "when": (
                "after explicit user selection, request the pinned Skill execution contract"
                if invocation_source == "user"
                else (
                    "before running the selected Codex Skill, request its pinned execution "
                    "contract; after the Skill completes, repeat sp-stage with execution_receipt"
                )
            ),
            "arguments": _sp_stage_arguments(
                stage=current_stage,
                task_description=str(route.get("task_description") or ""),
                invocation_source=invocation_source,
                scope=WorkflowScope(
                    route_id,
                    str(route.get("project_id") or ""),
                    str(route.get("stage_session_id") or ""),
                    str(route.get("flow_line_id") or ""),
                ),
            ),
        }
        insertion_index = 1 if mcp_calls else 0
        mcp_calls.insert(insertion_index, sp_call)
    return {
        **route,
        "route": route_id,
        "flow_label": flow["label"],
        "flow_summary": flow["summary"],
        "full_chain": full_chain,
        "branches": dict(flow.get("branches") or {}),
        "current_stage": current_stage,
        "next_stage": following_stage,
        "last_completed_step_index": int(completed_step_index),
        "last_completed_stage": (
            full_chain[completed_step_index] if 0 <= completed_step_index < len(full_chain) else ""
        ),
        "flow_scope_id": flow_scope_id,
        "stage_authority": authority,
        "auto_skills": auto_skills[:4],
        "user_skills": user_skills[:3],
        "mcp_calls": mcp_calls,
    }


def render_tool_route(route: dict[str, Any], *, max_chars: int = 1200) -> str:
    auto_skills = ", ".join(f"/{name}" for name in route.get("auto_skills") or []) or "none"
    user_skills = ", ".join(f"/{name}" for name in route.get("user_skills") or []) or "none"
    authority = dict(route.get("stage_authority") or {})

    def stage_label(skill: str) -> str:
        policy = authority.get(skill) or invocation_policy(skill)
        return f"/{skill} [{policy}]"

    full_chain = " -> ".join(stage_label(skill) for skill in route.get("full_chain") or [])
    branch_lines = []
    for branch_name, branch_stages in dict(route.get("branches") or {}).items():
        rendered = " -> ".join(stage_label(str(skill)) for skill in branch_stages)
        branch_lines.append(f"branch {branch_name}: {rendered}")
    current_stage = str(route.get("current_stage") or "")
    next_stage = str(route.get("next_stage") or "")
    mcp_lines = []
    for call in (route.get("mcp_calls") or [])[:4]:
        tool = str(call.get("tool") or "")
        when = str(call.get("when") or "")
        arguments = dict(call.get("arguments") or {})
        if tool == "session-init":
            keys = ("context_mode", "route", "project_id", "stage_session_id", "flow_line_id")
            rendered_args = ", ".join(
                f"{key}={_xml_text(repr(arguments[key]))}" for key in keys if arguments.get(key)
            )
            example = f"session-init(task_description=&lt;task&gt;, {rendered_args})"
        elif tool == "memory_recall":
            project_argument = _xml_text(repr(arguments.get("project_id")))
            example = f"memory_recall(project_id={project_argument}, query=&lt;task&gt;)"
        elif tool == "sp-stage":
            keys = (
                "stage",
                "invocation_source",
                "route",
                "project_id",
                "stage_session_id",
                "flow_line_id",
            )
            rendered_args = ", ".join(
                f"{key}={_xml_text(repr(arguments[key]))}" for key in keys if arguments.get(key)
            )
            example = f"sp-stage({rendered_args})"
        elif tool == "defense":
            example = "defense(action='get')"
        else:
            example = "step-closure(mode='full', task_description=<verified work>)"
        mcp_lines.append(f"- {when}: {example}")
    lines = ['<workflow-routing ephemeral="true" authority="advisory">']
    lines.extend(
        [
            f"project: {_xml_text(route.get('project_id', ''))}",
            f"official flow: {_xml_text(route.get('route', ''))}",
            f"full chain: {full_chain}",
            *branch_lines,
            f"current stage: {stage_label(current_stage)}"
            if current_stage
            else "current stage: none",
            f"next stage: {stage_label(next_stage)}" if next_stage else "next stage: complete",
            f"model-invoked skills: {auto_skills}",
            f"user-only skills (suggest, never auto-run): {user_skills}",
            *mcp_lines,
            "</workflow-routing>",
        ]
    )
    body = "\n".join(lines)
    budget = max(0, min(4000, int(max_chars)))
    if len(body) <= budget:
        return body

    # Route cursor fields are an atomic contract. Optional project, branches,
    # recommendations, and examples may be omitted, but these lines must never
    # be sliced into syntactically plausible partial guidance.
    closing = "</workflow-routing>"
    cursor = (
        f"/{current_stage} [{authority.get(current_stage) or invocation_policy(current_stage)}]"
        if current_stage
        else "complete"
    )
    if next_stage:
        cursor += (
            f" -> /{next_stage} [{authority.get(next_stage) or invocation_policy(next_stage)}]"
        )
    scope = WorkflowScope.from_route(route)
    scope_json = scope.escaped_json()
    compact_call = ""
    if current_stage and any("sp-stage(" in line for line in mcp_lines):
        compact_call = (
            f"sp-stage(stage={current_stage!r}, invocation_source="
            f"{(authority.get(current_stage) or invocation_policy(current_stage))!r}, **scope)"
        )
    session_call = "session-init(task_description=&lt;task&gt;,context_mode='light',**scope)"
    minimal_session_call = "session-init(task_description=&lt;task&gt;,**scope)"
    scoped_with_branches = [
        lines[0],
        f"official flow: {route.get('route', '')}",
        f"full chain: {full_chain}",
        *branch_lines,
        f"current stage: {stage_label(current_stage)}" if current_stage else "current stage: none",
        f"next stage: {stage_label(next_stage)}" if next_stage else "next stage: complete",
        f"scope={scope_json}",
        f"calls:{session_call}" + (f";{compact_call}" if compact_call else ""),
        closing,
    ]
    scoped_contract = [
        lines[0],
        f"cursor: {cursor}",
        f"scope={scope_json}",
        f"calls:{session_call}" + (f";{compact_call}" if compact_call else ""),
        closing,
    ]
    priority_scoped_call = (
        [lines[0], f"scope={scope_json}", f"call:{compact_call}", closing] if compact_call else []
    )
    minimal_scoped_session = [
        lines[0],
        f"scope={scope_json}",
        f"call:{minimal_session_call}",
        closing,
    ]
    bounded_candidates = ["\n".join(scoped_with_branches), "\n".join(scoped_contract)]
    if priority_scoped_call:
        bounded_candidates.append("\n".join(priority_scoped_call))
    bounded_candidates.append("\n".join(minimal_scoped_session))
    return next((candidate for candidate in bounded_candidates if len(candidate) <= budget), "")


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
