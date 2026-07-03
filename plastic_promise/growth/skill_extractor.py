"""技能沉淀提取器

从已完成的任务中提取可复用的技能条目，支持去重与合并。
"""

import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

# ── 中文动词/名词粗提取 ──────────────────────────────────────────────
# 常见中文动词后缀与高频技术动词，用于从任务描述中抽关键词。
_CN_VERB_PATTERNS = [
    r"创建",
    r"构建",
    r"配置",
    r"部署",
    r"调试",
    r"修复",
    r"重构",
    r"实现",
    r"设计",
    r"编写",
    r"优化",
    r"集成",
    r"迁移",
    r"测试",
    r"分析",
    r"提取",
    r"生成",
    r"转换",
    r"合并",
    r"拆分",
    r"导入",
    r"导出",
    r"打包",
    r"发布",
    r"初始化",
    r"安装",
    r"更新",
    r"升级",
    r"回滚",
    r"监控",
    r"调度",
    r"编排",
    r"注册",
    r"解析",
    r"序列化",
    r"压缩",
    r"加密",
    r"签名",
    r"验证",
    r"清理",
    r"归档",
    r"索引",
]

_CN_NOUN_PATTERNS = [
    r"Rust库",
    r"Python包",
    r"API",
    r"CLI",
    r"数据库",
    r"缓存",
    r"容器",
    r"镜像",
    r"配置",
    r"日志",
    r"监控",
    r"管道",
    r"向量嵌入",
    r"模型",
    r"特征",
    r"训练",
    r"推理",
    r"数据集",
    r"接口",
    r"服务",
    r"微服务",
    r"插件",
    r"模块",
    r"工作流",
    r"认证",
    r"授权",
    r"令牌",
    r"密钥",
    r"证书",
    r"会话",
    r"消息队列",
    r"事件",
    r"存储",
    r"备份",
    r"网络",
    r"代理",
]


def _tokenize(text: str) -> set:
    """将文本切分为词元集合（中英混合粗分）。"""
    tokens = set()
    # 英文/驼峰分词
    for tok in re.findall(r"[A-Za-z][a-z]+|[A-Z]{2,}(?=[A-Z][a-z]|\b)|[A-Z][a-z]*", text):
        tokens.add(tok.lower())
    # 中文双字/三字片段
    cleaned = re.sub(r"[^一-鿿]", "", text)
    for i in range(len(cleaned) - 1):
        tokens.add(cleaned[i : i + 2])
    for i in range(len(cleaned) - 2):
        tokens.add(cleaned[i : i + 3])
    return tokens


def _jaccard(a: str, b: str) -> float:
    """计算两段文本的 Jaccard 相似度 (0.0 ~ 1.0)。"""
    set_a = _tokenize(a)
    set_b = _tokenize(b)
    if not set_a or not set_b:
        return 0.0
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union)


def _extract_triggers(description: str, limit: int = 8) -> list[str]:
    """从描述中提取触发关键词（动词 + 名词）。"""
    triggers: list[str] = []
    for pat in _CN_VERB_PATTERNS:
        if pat in description and pat not in triggers:
            triggers.append(pat)
    for pat in _CN_NOUN_PATTERNS:
        if pat in description and pat not in triggers:
            triggers.append(pat)
    # 补充分词结果中的高频词
    extra = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", description)
    for w in extra:
        wl = w.lower()
        if wl not in triggers and len(wl) > 3:
            triggers.append(wl)
    return triggers[:limit]


def _derive_category(triggers: list[str]) -> str:
    """根据触发词推导技能类别。"""
    cat_map = {
        "创建": "build",
        "构建": "build",
        "初始化": "build",
        "实现": "build",
        "编写": "build",
        "生成": "build",
        "部署": "deploy",
        "发布": "deploy",
        "打包": "deploy",
        "配置": "config",
        "安装": "config",
        "更新": "config",
        "升级": "config",
        "测试": "test",
        "调试": "test",
        "验证": "test",
        "修复": "fix",
        "重构": "fix",
        "优化": "fix",
        "分析": "analyze",
        "监控": "analyze",
        "提取": "analyze",
        "合并": "merge",
        "拆分": "merge",
        "迁移": "merge",
        "加密": "security",
        "签名": "security",
        "认证": "security",
    }
    for t in triggers:
        if t in cat_map:
            return cat_map[t]
    return "general"


class SkillExtractor:
    """技能提取与沉淀引擎。

    监听任务完成事件，从任务描述和结果中抽取可复用的技能模式，
    存入技能库并检测重复项。
    """

    def __init__(self) -> None:
        """初始化技能提取器，加载已沉淀的技能库。"""
        self._skills: dict[str, dict[str, Any]] = {}
        self._patterns: dict[str, Any] = {}

    def extract(
        self,
        task_description: str,
        task_result: dict[str, Any],
    ) -> dict[str, Any] | None:
        """从单个任务中提取技能条目。

        如果任务结果不足以形成可复用技能（例如任务失败或描述过于模糊），
        返回 None。

        Args:
            task_description: 任务的自然语言描述。
            task_result: 任务执行结果，应包含至少 "success", "summary",
                "tools_used" 等字段。

        Returns:
            提取的技能条目字典，或 None (无可提取技能)。条目包含:
            - skill_id: str
            - name: str
            - description: str
            - triggers: List[str] (触发关键词)
            - tools: List[str] (使用的工具)
            - success_rate: float
            - extracted_at: str (ISO-8601 时间戳)
            - usage_count: int
            - category: str
        """
        # 1. 检查 task_result 是否有意义
        if not task_result:
            return None
        status = task_result.get("success", task_result.get("status", ""))
        # 如果明确失败且无可用信息，则跳过
        if status is False or status == "failed" or status == "error":
            # 但仍尝试从 summary 提取（部分失败也有价值）
            summary = task_result.get("summary", task_result.get("output", ""))
            if not summary or len(str(summary).strip()) < 10:
                return None

        # 2. 提取关键动词/名词
        triggers = _extract_triggers(task_description)
        if not triggers:
            return None

        # 3. 生成 skill_id (基于时间戳)
        ts = int(time.time() * 1_000_000)
        skill_id = f"skill_{ts}"

        # 4. 构建技能名称（取前两个 trigger + 描述摘要）
        name_tail = task_description[:40].strip()
        name = f"{triggers[0]}: {name_tail}"

        # 5. 构建条目
        tools_used = task_result.get("tools_used", task_result.get("tools", []))
        if isinstance(tools_used, str):
            tools_used = [tools_used]

        success_val = task_result.get("success", task_result.get("status"))
        success_rate = 1.0 if success_val in (True, "success", "ok") else 0.5

        skill: dict[str, Any] = {
            "skill_id": skill_id,
            "name": name,
            "description": task_description,
            "triggers": triggers,
            "tools": tools_used,
            "success_rate": success_rate,
            "extracted_at": datetime.now(timezone.utc).isoformat(),
            "usage_count": 1,
            "category": _derive_category(triggers),
            "summary": task_result.get("summary", ""),
        }

        # 6. 存储
        self._skills[skill_id] = skill
        return skill

    def get_all_skills(self) -> list[dict[str, Any]]:
        """获取所有已沉淀的技能条目。

        Returns:
            技能条目列表，按 usage_count 降序排列。
        """
        return sorted(
            self._skills.values(),
            key=lambda s: s.get("usage_count", 0),
            reverse=True,
        )

    def find_duplicates(self) -> list[tuple[str, str, float]]:
        """检测技能库中的重复条目。

        基于技能描述的 Jaccard 相似度 (> 0.7) 进行匹配。

        Returns:
            重复对列表，每个元素为 (skill_id_a, skill_id_b, similarity)。
        """
        pairs: list[tuple[str, str, float]] = []
        ids = list(self._skills.keys())
        n = len(ids)
        for i in range(n):
            for j in range(i + 1, n):
                desc_a = self._skills[ids[i]].get("description", "")
                desc_b = self._skills[ids[j]].get("description", "")
                sim = _jaccard(desc_a, desc_b)
                if sim > 0.7:
                    pairs.append((ids[i], ids[j], round(sim, 4)))
        # 按相似度降序
        pairs.sort(key=lambda x: x[2], reverse=True)
        return pairs

    def merge_skills(self, skill_a_id: str, skill_b_id: str) -> dict[str, Any]:
        """合并两个重复技能条目。

        将 skill_b 的信息合并到 skill_a，移除 skill_b，
        返回合并后的技能条目。

        Args:
            skill_a_id: 保留的技能 ID (主技能)。
            skill_b_id: 被合并的技能 ID (将被移除)。

        Returns:
            合并后的技能条目字典。

        Raises:
            KeyError: 任一技能 ID 不存在。
        """
        if skill_a_id not in self._skills:
            raise KeyError(f"Skill '{skill_a_id}' not found")
        if skill_b_id not in self._skills:
            raise KeyError(f"Skill '{skill_b_id}' not found")

        a = self._skills[skill_a_id]
        b = self._skills[skill_b_id]

        # 合并 triggers (去重)
        merged_triggers = list(dict.fromkeys(a.get("triggers", []) + b.get("triggers", [])))

        # 合并 tools (去重)
        merged_tools = list(dict.fromkeys(a.get("tools", []) + b.get("tools", [])))

        # 合并 usage_count
        merged_usage = a.get("usage_count", 0) + b.get("usage_count", 0)

        # 成功率取加权平均
        rate_a = a.get("success_rate", 0.5)
        rate_b = b.get("success_rate", 0.5)
        cnt_a = a.get("usage_count", 1)
        cnt_b = b.get("usage_count", 1)
        merged_rate = round((rate_a * cnt_a + rate_b * cnt_b) / (cnt_a + cnt_b), 4)

        # 描述：保留较长的
        desc_a = a.get("description", "")
        desc_b = b.get("description", "")
        merged_desc = desc_a if len(desc_a) >= len(desc_b) else desc_b

        # 更新 a
        a["triggers"] = merged_triggers
        a["tools"] = merged_tools
        a["usage_count"] = merged_usage
        a["success_rate"] = merged_rate
        a["description"] = merged_desc
        a["merged_from"] = a.get("merged_from", []) + [skill_b_id]
        a["extracted_at"] = datetime.now(timezone.utc).isoformat()

        # 删除 b
        del self._skills[skill_b_id]

        return a

    def get_stats(self) -> dict[str, Any]:
        """获取技能库统计信息。

        Returns:
            统计字典，包含:
            - total_skills: int
            - by_category: Dict[str, int]
            - new_this_week: int
            - top_skills: List[Dict[str, Any]] (top 5)
            - total_triggers: int
            - avg_success_rate: float
            - duplicate_pairs: int
            - last_extraction: Optional[str]
        """
        skills = list(self._skills.values())
        total = len(skills)

        # 按类别统计
        by_category: dict[str, int] = {}
        for s in skills:
            cat = s.get("category", "general")
            by_category[cat] = by_category.get(cat, 0) + 1

        # 本周新增
        now = datetime.now(timezone.utc)
        week_ago = now - timedelta(days=7)
        new_this_week = 0
        for s in skills:
            extracted = s.get("extracted_at", "")
            if extracted:
                try:
                    dt = datetime.fromisoformat(extracted)
                    if dt >= week_ago:
                        new_this_week += 1
                except (ValueError, TypeError):
                    pass

        # Top skills (按 usage_count)
        sorted_skills = sorted(skills, key=lambda s: s.get("usage_count", 0), reverse=True)
        top_skills = [
            {
                "skill_id": s["skill_id"],
                "name": s["name"],
                "usage_count": s.get("usage_count", 0),
                "category": s.get("category", "general"),
            }
            for s in sorted_skills[:5]
        ]

        # 总触发词数
        total_triggers = sum(len(s.get("triggers", [])) for s in skills)

        # 平均成功率
        avg_success_rate = (
            round(sum(s.get("success_rate", 0.0) for s in skills) / total, 4) if total > 0 else 0.0
        )

        # 重复对数量
        duplicate_pairs = len(self.find_duplicates())

        # 最近提取时间
        last_extraction: str | None = None
        if skills:
            last_extraction = max(s.get("extracted_at", "") for s in skills) or None

        return {
            "total_skills": total,
            "by_category": by_category,
            "new_this_week": new_this_week,
            "top_skills": top_skills,
            "total_triggers": total_triggers,
            "avg_success_rate": avg_success_rate,
            "duplicate_pairs": duplicate_pairs,
            "last_extraction": last_extraction,
        }


# ── 模块级便捷函数 ──────────────────────────────────────────────────

_module_extractor: SkillExtractor | None = None


def _get_extractor() -> SkillExtractor:
    """获取或创建模块级单例提取器。"""
    global _module_extractor
    if _module_extractor is None:
        _module_extractor = SkillExtractor()
    return _module_extractor


def extract_skill(
    task_description: str,
    task_result: dict[str, Any],
) -> dict[str, Any] | None:
    """模块级便捷函数 — 使用默认提取器实例从任务中提取技能。

    Args:
        task_description: 任务的自然语言描述。
        task_result: 任务执行结果。

    Returns:
        提取的技能条目字典，或 None。
    """
    return _get_extractor().extract(task_description, task_result)
