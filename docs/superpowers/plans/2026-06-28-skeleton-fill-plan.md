# Skeleton Fill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补齐 Plastic Promise 全模块骨架——确保每个 .py 文件具备完整的类/函数签名、docstring、类型标注和模块间 import，逻辑留 `pass` 占位。

**Architecture:** 8 个子系统并行填充。所有模块从 `plastic_promise.core.constants` 和 `plastic_promise.core.context_engine` 单向导入，子系统之间无交叉依赖。MCP 工具 handler 从 server.py 拆分为 6 个独立文件。

**Tech Stack:** Python 3.11+, Rust (仅骨架注释), 纯标准库无额外依赖

## Global Constraints

- Skeleton depth: Level B — 完整 docstring（描述/Args/Returns） + 类型标注 + `pass` body
- No implementation logic beyond `pass` / `...` and return-type placeholders
- All imports go through `plastic_promise.core.*` only
- `__init__.py` must export all public API for each subpackage
- `server.py` `call_tool` must delegate to tool files (no inline handler logic)
- Zero cross-imports between sibling subsystems (memory/loop/principles/reflection/defense/growth)

---

### Task 1: 目录结构搭建

**Files:**
- Create: `plastic_promise/core/__init__.py`
- Create: `plastic_promise/memory/__init__.py`
- Create: `plastic_promise/loop/__init__.py`
- Create: `plastic_promise/principles/__init__.py`
- Create: `plastic_promise/reflection/__init__.py`
- Create: `plastic_promise/defense/__init__.py`
- Create: `plastic_promise/growth/__init__.py`
- Move: `plastic_promise/constants.py` → `plastic_promise/core/constants.py`
- Move: `plastic_promise/context_engine.py` → `plastic_promise/core/context_engine.py`
- Create: `plastic_promise/mcp/resources.py`
- Create: `plastic_promise/mcp/prompts.py`
- Create: `plastic_promise/mcp/tools/memory.py`
- Create: `plastic_promise/mcp/tools/principles.py`
- Create: `plastic_promise/mcp/tools/context.py`
- Create: `plastic_promise/mcp/tools/audit_defense.py`
- Create: `plastic_promise/mcp/tools/reflection.py`
- Create: `plastic_promise/mcp/tools/management.py`

**Interfaces:**
- Consumes: 无
- Produces: 空 `__init__.py` 文件和空骨架文件，目录结构就绪

- [ ] **Step 1: 创建所有子目录**

```bash
mkdir -p plastic_promise/core
mkdir -p plastic_promise/memory
mkdir -p plastic_promise/loop
mkdir -p plastic_promise/principles
mkdir -p plastic_promise/reflection
mkdir -p plastic_promise/defense
mkdir -p plastic_promise/growth
touch plastic_promise/mcp/resources.py
touch plastic_promise/mcp/prompts.py
touch plastic_promise/mcp/tools/memory.py
touch plastic_promise/mcp/tools/principles.py
touch plastic_promise/mcp/tools/context.py
touch plastic_promise/mcp/tools/audit_defense.py
touch plastic_promise/mcp/tools/reflection.py
touch plastic_promise/mcp/tools/management.py
```

- [ ] **Step 2: 迁移 core/ 模块**

```bash
git mv plastic_promise/constants.py plastic_promise/core/constants.py
git mv plastic_promise/context_engine.py plastic_promise/core/context_engine.py
```

- [ ] **Step 3: 验证目录结构**

```bash
find plastic_promise -type f -name "*.py" | sort
```
Expected: 列出所有 26 个 .py 文件（7 个现有 + 19 个新创建/迁移的）

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "scaffold: directory structure for 7-subsystem skeleton"
```

---

### Task 2: core/__init__.py + 迁移模块导入修复

**Files:**
- Create: `plastic_promise/core/__init__.py`
- Modify: `plastic_promise/core/constants.py` — 无修改，仅确认导入路径
- Modify: `plastic_promise/core/context_engine.py` — 修复导入路径为 `plastic_promise.core.constants`

**Interfaces:**
- Consumes: 无
- Produces:
  - `from plastic_promise.core import CORE_PRINCIPLES, DIGITAL_BODY_SYSTEMS, DEFENSE_LAYERS, AUDIT_DIMENSIONS, SCARF_DIMENSIONS, MEMORY_TIERS, ContextEngine, ContextPack`
  - `from plastic_promise.core.constants import ...` (细粒度导入)
  - `from plastic_promise.core.context_engine import ContextEngine, ContextPack, ContextItem`

- [ ] **Step 1: 写 core/__init__.py**

```python
"""Plastic Promise 核心基础层

包含：
- constants: 九大系统、三层防线、信任分、审计维度、11条核心原则、SCARF等全部常量
- context_engine: ContextEngine 上下文供应引擎（Python回退版 + Rust PyO3桥接）
"""

from plastic_promise.core.constants import (
    # 九大系统
    DIGITAL_BODY_SYSTEMS,
    # 三层防线
    DEFENSE_LAYERS,
    # 信任分
    TRUST_INITIAL,
    TRUST_DECAY_RATE,
    TRUST_BOOST_RATE,
    TRUST_MIN,
    TRUST_MAX,
    # 审计维度
    AUDIT_DIMENSIONS,
    # SCARF
    SCARF_DIMENSIONS,
    # 上下文引擎
    CONTEXT_LAYERS,
    RRF_K,
    SYMBOL_RULE_KEYWORDS,
    ASSOCIATION_WEIGHTS,
    # 记忆系统
    MEMORY_TIERS,
    MEMORY_HEALTH_THRESHOLD,
    MEMORY_DECAY_THRESHOLD,
    MEMORY_GC_INTERVAL_DAYS,
    WORTH_SUCCESS_WEIGHT,
    WORTH_FAILURE_WEIGHT,
    WORTH_MIN_OBSERVATIONS,
    # 11条核心原则
    CORE_PRINCIPLES,
    PRINCIPLE_DOMAINS,
    PRINCIPLE_INHERITANCE_DIRECTIONS,
    PRINCIPLE_INHERITANCE_DECAY,
    # Cron
    CRON_CONFIG,
    # Claude Code
    CLASSIFIER_KEYWORDS,
    CLASSIFIER_THRESHOLD_CLAUDE,
    CLASSIFIER_THRESHOLD_ACP,
    # CEI
    CEI_THRESHOLDS,
    CEI_TARGET,
    # 通用阈值
    PRE_CHECK_ALERT_THRESHOLD,
    CLOSURE_RATE_TARGET,
    PRINCIPLE_ACTIVATION_TARGET,
    INERTIA_SUPPRESSION_WINDOW,
    INERTIA_SUPPRESSION_THRESHOLD,
    CURIOSITY_EXPLORE_RATE,
)

from plastic_promise.core.context_engine import (
    ContextEngine,
    ContextPack,
    ContextItem,
)

__all__ = [
    "DIGITAL_BODY_SYSTEMS",
    "DEFENSE_LAYERS",
    "TRUST_INITIAL", "TRUST_DECAY_RATE", "TRUST_BOOST_RATE", "TRUST_MIN", "TRUST_MAX",
    "AUDIT_DIMENSIONS",
    "SCARF_DIMENSIONS",
    "CONTEXT_LAYERS", "RRF_K", "SYMBOL_RULE_KEYWORDS", "ASSOCIATION_WEIGHTS",
    "MEMORY_TIERS", "MEMORY_HEALTH_THRESHOLD", "MEMORY_DECAY_THRESHOLD",
    "MEMORY_GC_INTERVAL_DAYS",
    "WORTH_SUCCESS_WEIGHT", "WORTH_FAILURE_WEIGHT", "WORTH_MIN_OBSERVATIONS",
    "CORE_PRINCIPLES", "PRINCIPLE_DOMAINS", "PRINCIPLE_INHERITANCE_DIRECTIONS",
    "PRINCIPLE_INHERITANCE_DECAY",
    "CRON_CONFIG",
    "CLASSIFIER_KEYWORDS", "CLASSIFIER_THRESHOLD_CLAUDE", "CLASSIFIER_THRESHOLD_ACP",
    "CEI_THRESHOLDS", "CEI_TARGET",
    "PRE_CHECK_ALERT_THRESHOLD", "CLOSURE_RATE_TARGET", "PRINCIPLE_ACTIVATION_TARGET",
    "INERTIA_SUPPRESSION_WINDOW", "INERTIA_SUPPRESSION_THRESHOLD",
    "CURIOSITY_EXPLORE_RATE",
    "ContextEngine", "ContextPack", "ContextItem",
]
```

- [ ] **Step 2: 修复 context_engine.py 导入路径**

```python
# 将 context_engine.py 中的:
# from plastic_promise.constants import ...
# 改为:
# from plastic_promise.core.constants import ...
```

Run:
```bash
sed -i 's/from plastic_promise.constants import/from plastic_promise.core.constants import/g' plastic_promise/core/context_engine.py
```

- [ ] **Step 3: 验证导入**

```bash
cd F:/Agent/Memory\ system && python -c "from plastic_promise.core import CORE_PRINCIPLES, ContextEngine; print('OK:', len(CORE_PRINCIPLES), 'principles')"
```
Expected: `OK: 11 principles`

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "feat: core/__init__.py with full public API exports; fix import paths"
```

---

### Task 3: memory/soul_memory.py 骨架（~300 行签名）

**Files:**
- Create: `plastic_promise/memory/__init__.py`
- Create: `plastic_promise/memory/soul_memory.py`

**Interfaces:**
- Consumes: `plastic_promise.core.constants` (MEMORY_TIERS, WORTH_*, COREPRINCIPLES)
- Consumes: `plastic_promise.core.context_engine` (ContextEngine, ContextPack)
- Produces: `RecMem`, `MemoryRecord`, `MemoryTierManager`, `EvolveR`, `MemoryGC`

- [ ] **Step 1: 写 memory/__init__.py**

```python
"""记忆系统（海马体/大脑皮层）

双层三域架构 + L1/L3 分层 + 四系统融合记忆管理。
包含 RecMem 存储检索、分层管理、EvolveR 演化、GC 垃圾回收。
"""

from plastic_promise.memory.soul_memory import (
    MemoryRecord,
    RecMem,
    MemoryTierManager,
    EvolveR,
    MemoryGC,
    MemoryWorthCalculator,
)

__all__ = [
    "MemoryRecord",
    "RecMem",
    "MemoryTierManager",
    "EvolveR",
    "MemoryGC",
    "MemoryWorthCalculator",
]
```

- [ ] **Step 2: 写 soul_memory.py 骨架**

Create `plastic_promise/memory/soul_memory.py` with complete docstrings:

```python
"""记忆系统核心模块 — RecMem 存储/检索 + L1 分层 + EvolveR 演化 + GC 回收

对应数字身体中的「记忆系统」（海马体/大脑皮层），成熟度 0.90。
实现双层三域架构：工作记忆(L1) / 长期记忆(L3) × 文本域/实体域/原则域。
四系统融合：RecMem + EvolveR + GC + 上下文供应。

崩溃前规模: ~7618 行。此文件为骨架重建。
"""

import datetime
import json
import uuid
from typing import Optional, List, Dict, Any, Tuple, Callable

from plastic_promise.core.constants import (
    MEMORY_TIERS,
    MEMORY_HEALTH_THRESHOLD,
    MEMORY_DECAY_THRESHOLD,
    MEMORY_GC_INTERVAL_DAYS,
    WORTH_SUCCESS_WEIGHT,
    WORTH_FAILURE_WEIGHT,
    WORTH_MIN_OBSERVATIONS,
)
from plastic_promise.core.context_engine import ContextEngine, ContextPack


class MemoryWorthCalculator:
    """Memory Worth 双计数器计算器。

    每条记忆维护 success/failure 两个标量计数器，
    根据共现历史计算 worth_score。
    学术界已验证 ρ≈0.89 的相关公式。

    Attributes:
        min_observations: 最少观察次数才启用 worth 信号（默认 5）。
    """

    def __init__(self, min_observations: int = WORTH_MIN_OBSERVATIONS) -> None:
        """初始化双计数器计算器。

        Args:
            min_observations: 最少观察次数阈值。
        """
        pass

    def calculate_worth(
        self,
        success_count: int,
        failure_count: int,
        total_observations: Optional[int] = None,
    ) -> float:
        """基于双计数器计算 worth_score。

        公式: worth = success / (success + failure)，冷启动时返回 0.5 中性分。

        Args:
            success_count: 采纳次数。
            failure_count: 拒绝/忽略次数。
            total_observations: 总观察次数（可选，默认 = success + failure）。

        Returns:
            worth_score 浮点数，范围 [0.0, 1.0]。
        """
        pass

    def update_counters(
        self,
        record: "MemoryRecord",
        feedback_type: str,
    ) -> None:
        """根据反馈类型递增多对应计数器。

        Args:
            record: 被评价的记忆记录。
            feedback_type: 反馈类型 — 'adopted' / 'ignored' / 'rejected'。
        """
        pass


class MemoryRecord:
    """Plastic Promise 记忆记录。

    每条记忆包含内容、类型、来源、worth 双计数器、
    激活权重和时间戳。worth_success / worth_failure
    内嵌在记录中以保证原子性。

    Attributes:
        id: 唯一标识符。
        content: 记忆内容文本。
        memory_type: 分类 — task / experience / principle / code。
        source: 来源 — user / system / previous_output。
        worth_success: 成功（采纳）计数。
        worth_failure: 失败（拒绝/忽略）计数。
        activation_weight: 激活权重 [0.0, 1.0]。
        created_at: ISO 时间戳。
        last_accessed_at: ISO 时间戳，最近一次被检索的时间。
        tier: 当前分层 — L1（工作记忆）或 L3（长期记忆）。
        metadata: 扩展元数据字典。
    """

    def __init__(
        self,
        content: str,
        memory_type: str = "experience",
        source: str = "user",
        memory_id: Optional[str] = None,
        worth_success: int = 0,
        worth_failure: int = 0,
        activation_weight: float = 0.5,
        tier: str = "L1",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """创建一条记忆记录。

        Args:
            content: 记忆内容文本。
            memory_type: 分类标签。
            source: 来源标识。
            memory_id: 唯一 ID（不提供则自动生成）。
            worth_success: 初始成功计数。
            worth_failure: 初始失败计数。
            activation_weight: 初始激活权重。
            tier: 初始分层。
            metadata: 可选元数据。
        """
        pass

    @property
    def worth_score(self) -> float:
        """基于双计数器计算当前 worth_score。

        Returns:
            float: worth_score [0.0, 1.0]。
        """
        pass

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典。

        Returns:
            dict: 包含所有字段的可序列化字典。
        """
        pass

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MemoryRecord":
        """从字典反序列化。

        Args:
            data: 序列化后的字典。

        Returns:
            MemoryRecord 实例。
        """
        pass


class MemoryTierManager:
    """记忆分层管理器。

    管理 L1（工作记忆，~200条/24h TTL）
    和 L3（长期记忆，~2000条/永久）之间的
    晋升和降级。

    Attributes:
        l1_max: L1 最大条目数。
        l3_max: L3 最大条目数。
        l1_ttl_hours: L1 生存时间（小时）。
    """

    def __init__(self) -> None:
        """初始化分层管理器，从 MEMORY_TIERS 读取配置。"""
        pass

    def classify_tier(self, record: MemoryRecord) -> str:
        """根据记录的访问频率和 worth_score 决定分层。

        Args:
            record: 记忆记录。

        Returns:
            分层标签: 'L1' 或 'L3'。
        """
        pass

    def promote_to_l3(self, record: MemoryRecord) -> None:
        """将记录从 L1 晋升到 L3。

        晋升条件: worth_score ≥ 0.60 且 access_count ≥ 3。

        Args:
            record: 要晋升的记录。
        """
        pass

    def demote_to_l1(self, record: MemoryRecord) -> None:
        """将记录从 L3 降级到 L1（将被 GC 候选）。

        Args:
            record: 要降级的记录。
        """
        pass

    def evict_l1_overflow(self, records: List[MemoryRecord]) -> List[str]:
        """L1 超量时驱逐最不活跃的记录。

        Args:
            records: 当前 L1 记录列表。

        Returns:
            被驱逐的记录 ID 列表。
        """
        pass


class RecMem:
    """推荐记忆存储与检索引擎。

    双层三域架构的核心执行者：
    - 文本域: 基于内容的记忆检索
    - 实体域: 基于实体关联图的检索
    - 原则域: 核心原则的激活与联想

    调用 ContextEngine.supply() 进行混合检索，
    返回结构化 ContextPack。

    Attributes:
        records: 内存中的记忆字典 {id: MemoryRecord}。
        engine: ContextEngine 引用（用于 supply 调用）。
        tier_manager: 分层管理器。
        worth_calc: Memory Worth 计算器。
    """

    def __init__(self, engine: Optional[ContextEngine] = None) -> None:
        """初始化记忆引擎。

        Args:
            engine: ContextEngine 实例（可选，延迟初始化）。
        """
        pass

    def store(
        self,
        content: str,
        memory_type: str = "experience",
        source: str = "user",
        importance: float = 0.7,
        entity_ids: Optional[List[str]] = None,
    ) -> MemoryRecord:
        """存储一条记忆并返回记录。

        执行去重检查（余弦相似度 > 0.98 视为重复），
        自动注册到 ContextEngine 的记忆池和 EntityGraph。

        Args:
            content: 记忆内容。
            memory_type: 分类标签。
            source: 来源标识。
            importance: 重要性评分 [0.0, 1.0]。
            entity_ids: 关联实体 ID 列表。

        Returns:
            新创建的 MemoryRecord。
        """
        pass

    def recall(
        self,
        query: str,
        task_type: str = "general",
        max_results: int = 20,
        min_relevance: float = 0.2,
        include_principles: bool = True,
    ) -> ContextPack:
        """混合检索记忆，返回三层上下文包。

        委托给 ContextEngine.supply() 执行双路检索 → 融合 → 分层。

        Args:
            query: 检索查询或任务描述。
            task_type: 任务类型标签。
            max_results: 各层最大返回条目数。
            min_relevance: 最低关联分数阈值。
            include_principles: 是否注入激活的原则。

        Returns:
            ContextPack 三层上下文包。
        """
        pass

    def update(
        self,
        memory_id: str,
        content: Optional[str] = None,
        importance: Optional[float] = None,
        reset_worth: bool = False,
    ) -> Optional[MemoryRecord]:
        """更新已有记忆的内容或元数据。

        如果提供了新内容，会重新嵌入并检查去重。
        如果 reset_worth=True，重置双计数器。

        Args:
            memory_id: 记忆 ID。
            content: 新内容（可选）。
            importance: 新重要性评分（可选）。
            reset_worth: 是否重置 worth 计数器。

        Returns:
            更新后的 MemoryRecord，如果未找到返回 None。
        """
        pass

    def forget(self, memory_id: str, reason: str = "") -> bool:
        """软删除记忆（标记为衰退而非物理删除）。

        衰退的记忆将在 7 天后被 GC 清理，
        在清理前可以通过 update 恢复。

        Args:
            memory_id: 记忆 ID。
            reason: 删除原因。

        Returns:
            是否成功标记。
        """
        pass

    def list_records(
        self,
        memory_type: Optional[str] = None,
        source: Optional[str] = None,
        min_worth: Optional[float] = None,
        limit: int = 50,
    ) -> List[MemoryRecord]:
        """按条件列出记忆记录。

        Args:
            memory_type: 筛选类型（可选）。
            source: 筛选来源（可选）。
            min_worth: 最低 worth_score（可选）。
            limit: 返回数量上限。

        Returns:
            匹配的记录列表，按 worth_score 降序排列。
        """
        pass

    def stats(self) -> Dict[str, Any]:
        """获取记忆池统计信息。

        Returns:
            dict 包含: total, healthy, decaying, by_type, worth_distribution。
        """
        pass

    def apply_feedback(
        self,
        memory_id: str,
        feedback_type: str,
        task_context: str = "",
    ) -> Dict[str, Any]:
        """对记忆应用反馈并更新 worth 计数器。

        Args:
            memory_id: 记忆 ID。
            feedback_type: 反馈类型 — 'adopted' / 'ignored' / 'rejected'。
            task_context: 触发反馈的任务上下文。

        Returns:
            dict 包含: updated, new_worth_score, counters。
        """
        pass

    @property
    def total_count(self) -> int:
        """记忆总量。"""
        pass

    @property
    def health_ratio(self) -> float:
        """健康记忆占比。"""
        pass


class EvolveR:
    """记忆演化引擎。

    周期性检查记忆健康状况，执行:
    - 基于 worth_score 的衰减
    - 低价值记忆的标记
    - 关联反馈的应用
    - 激活权重的更新

    Attributes:
        rec_mem: RecMem 引用。
        decay_threshold: 低于此 worth_score 标记为衰退。
    """

    def __init__(
        self,
        rec_mem: RecMem,
        decay_threshold: float = MEMORY_DECAY_THRESHOLD,
    ) -> None:
        """初始化演化引擎。

        Args:
            rec_mem: 记忆系统引用。
            decay_threshold: 衰退阈值。
        """
        pass

    def evolve_cycle(self) -> Dict[str, Any]:
        """执行一次演化周期。

        遍历所有记忆，更新 worth_score 和 tier，
        标记衰退记忆，返回变更统计。

        Returns:
            dict: {promoted, demoted, decayed, unchanged}。
        """
        pass

    def decay_stale(self, days_threshold: int = MEMORY_GC_INTERVAL_DAYS) -> int:
        """衰减长时间未访问的陈旧记忆。

        Args:
            days_threshold: 未访问天数阈值。

        Returns:
            被衰减的记忆数量。
        """
        pass


class MemoryGC:
    """记忆垃圾回收器。

    清理 worth_score 低于阈值且超过指定天数
    未访问的衰退记忆。支持 dry_run 预览模式。

    Attributes:
        rec_mem: RecMem 引用。
    """

    def __init__(self, rec_mem: RecMem) -> None:
        """初始化 GC。

        Args:
            rec_mem: 记忆系统引用。
        """
        pass

    def collect(
        self,
        dry_run: bool = True,
        force: bool = False,
    ) -> Dict[str, Any]:
        """执行垃圾回收。

        Args:
            dry_run: 仅预览不删除（默认 True）。
            force: 强制执行（忽略安全阈值）。

        Returns:
            dict: {collected_ids, count, freed_items}。
        """
        pass

    def mark_decaying(self) -> List[str]:
        """标记所有满足衰退条件的记忆。

        Returns:
            被标记的记忆 ID 列表。
        """
        pass
```

- [ ] **Step 3: 验证导入**

```bash
cd "F:/Agent/Memory system" && python -c "from plastic_promise.memory import RecMem, MemoryRecord, MemoryTierManager, EvolveR, MemoryGC, MemoryWorthCalculator; print('memory OK')"
```
Expected: `memory OK`

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "feat: memory/soul_memory.py skeleton — RecMem + MemoryWorth + EvolveR + GC"
```

---

### Task 4: loop/soul_loop.py 骨架（~120 行签名）

**Files:**
- Create: `plastic_promise/loop/__init__.py`
- Create: `plastic_promise/loop/soul_loop.py`

**Interfaces:**
- Consumes: `plastic_promise.core.*`, `plastic_promise.memory.*`, `plastic_promise.principles.*`, `plastic_promise.defense.*`, `plastic_promise.reflection.*`, `plastic_promise.growth.*`
- Produces: `SoulLoop`, `pre_task_v2()`, `post_task()`

- [ ] **Step 1: 写 loop/__init__.py**

```python
"""主控编排系统（神经中枢）

pre_task_v2 + post_task 完整编排：
上下文供应 → SCARF 自省 → 激素更新 → 记忆演化 → 审计记录。
"""

from plastic_promise.loop.soul_loop import SoulLoop, pre_task_v2, post_task

__all__ = ["SoulLoop", "pre_task_v2", "post_task"]
```

- [ ] **Step 2: 写 soul_loop.py 骨架**

```python
"""主控编排模块 — pre_task_v2 + post_task 完整任务闭环

对应数字身体中的「神经中枢」。
协调上下文供应引擎、SCARF 自省、激素系统、记忆演化、
审计记录之间的数据流。

崩溃前规模: ~958 行。此文件为骨架重建。
"""

from typing import Optional, Dict, Any

from plastic_promise.core.constants import (
    CEI_TARGET,
    PRE_CHECK_ALERT_THRESHOLD,
)
from plastic_promise.core.context_engine import ContextEngine, ContextPack


class SoulLoop:
    """主控编排器。

    管理完整的任务生命周期:
    1. pre_task_v2: 上下文准备 + 原则注入 + 审计预检查
    2. post_task: 反馈收集 + 记忆更新 + 激素调整 + 审计记录

    Attributes:
        engine: ContextEngine 实例。
        cei_history: CEI 指数历史记录。
        task_count: 累计任务数。
    """

    def __init__(self, engine: Optional[ContextEngine] = None) -> None:
        """初始化编排器。

        Args:
            engine: ContextEngine 实例（可选，延迟初始化）。
        """
        pass

    def pre_task_v2(
        self,
        task_description: str,
        task_type: str = "general",
        pre_context: Optional[str] = None,
    ) -> ContextPack:
        """任务前上下文准备。

        调用 ContextEngine.supply() 获取三层上下文包，
        注入激活原则，执行审计预检查。

        Args:
            task_description: 任务完整描述。
            task_type: 任务类型标签。
            pre_context: 已有前文上下文。

        Returns:
            ContextPack 三层上下文包，可直接注入 Agent 决策上下文。
        """
        pass

    def post_task(
        self,
        task_description: str,
        task_type: str,
        context_pack: ContextPack,
        feedback: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """任务后收尾。

        收集反馈 → 更新记忆 worth → 触发激素更新 →
        执行 SCARF 自省 → 写入审计记录 → 更新 CEI。

        Args:
            task_description: 任务描述。
            task_type: 任务类型。
            context_pack: pre_task 返回的上下文包。
            feedback: 用户/系统反馈数据。

        Returns:
            dict: {audit_record, hormone_changes, cei_delta}。
        """
        pass

    def calculate_cei(self) -> float:
        """计算当前 CEI 约定作用指数。

        基于七个维度的加权平均:
        原则联想 0.20 + 记忆供应 0.15 + 约束合规 0.15
        + 反馈闭环 0.15 + 信任校准 0.10 + 原则继承 0.10
        + 安全追溯 0.15。

        Returns:
            CEI 指数 [0.0, 1.0]。
        """
        pass

    @property
    def current_cei(self) -> float:
        """当前 CEI 值。"""
        pass

    @property
    def cei_tier(self) -> str:
        """当前 CEI 评级: nascent/growing/forming/internalizing/mature/autonomous。"""
        pass


def pre_task_v2(
    task_description: str,
    task_type: str = "general",
    pre_context: Optional[str] = None,
) -> ContextPack:
    """便捷函数: 任务前上下文供应。

    自动初始化 SoulLoop 单例并调用 pre_task_v2。

    Args:
        task_description: 任务完整描述。
        task_type: 任务类型标签。
        pre_context: 已有前文上下文。

    Returns:
        ContextPack 三层上下文包。
    """
    pass


def post_task(
    task_description: str,
    task_type: str,
    context_pack: ContextPack,
    feedback: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """便捷函数: 任务后收尾。

    Args:
        task_description: 任务描述。
        task_type: 任务类型。
        context_pack: pre_task 返回的上下文包。
        feedback: 反馈数据。

    Returns:
        dict: 收尾结果。
    """
    pass
```

- [ ] **Step 3: 验证导入**

```bash
cd "F:/Agent/Memory system" && python -c "from plastic_promise.loop import SoulLoop, pre_task_v2, post_task; print('loop OK')"
```
Expected: `loop OK`

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "feat: loop/soul_loop.py skeleton — SoulLoop + pre_task_v2 + post_task"
```

---

### Task 5: principles/soul_principles.py 骨架（~100 行签名）

**Files:**
- Create: `plastic_promise/principles/__init__.py`
- Create: `plastic_promise/principles/soul_principles.py`

**Interfaces:**
- Consumes: `plastic_promise.core.constants` (CORE_PRINCIPLES, PRINCIPLE_*)
- Produces: `PrincipleManager`, `principle_activate()`, `principle_inherit()`, `principle_diffuse()`, `principle_evaluate()`

- [ ] **Step 1: 写 principles/__init__.py**

```python
"""原则遗传系统（DNA/基因遗传）

核心约定跨 Agent 代际传递:
work→all / life→all 单向扩散 + 同步衰减。
"""

from plastic_promise.principles.soul_principles import (
    PrincipleManager,
    principle_activate,
    principle_inherit,
    principle_diffuse,
    principle_evaluate,
)

__all__ = [
    "PrincipleManager",
    "principle_activate",
    "principle_inherit",
    "principle_diffuse",
    "principle_evaluate",
]
```

- [ ] **Step 2: 写 soul_principles.py 骨架**

```python
"""原则检索与继承同步模块

对应数字身体中的「遗传系统」（DNA/基因遗传），成熟度 0.60。
实现:
- 从 EntityGraph 检索激活原则
- work→all / life→all 单向扩散
- 同步衰减系数 (0.70) 控制跨域传播权重
- 反事实评估: 「如果违反会怎样」预演

崩溃前规模: ~1200 行。此文件为骨架重建。
"""

from typing import List, Dict, Optional, Any

from plastic_promise.core.constants import (
    CORE_PRINCIPLES,
    PRINCIPLE_DOMAINS,
    PRINCIPLE_INHERITANCE_DIRECTIONS,
    PRINCIPLE_INHERITANCE_DECAY,
)
from plastic_promise.core.context_engine import ContextEngine


class PrincipleManager:
    """原则管理器。

    管理 11 条核心原则的激活、扩散、继承和评估。
    与 EntityGraph 交互以建立任务→原则的显式关联边。

    Attributes:
        engine: ContextEngine 引用（用于访问 EntityGraph）。
        activated: 当前激活的原则 ID 列表。
        inheritance_log: 原则继承日志。
    """

    def __init__(self, engine: Optional[ContextEngine] = None) -> None:
        """初始化原则管理器。

        Args:
            engine: ContextEngine 实例。
        """
        pass

    def activate(
        self,
        task_type: str,
        task_description: str = "",
        max_principles: int = 5,
    ) -> List[Dict[str, Any]]:
        """根据任务类型激活相关核心原则。

        使用预定义的任务类型→原则映射，
        并通过关键词匹配补充额外原则。

        Args:
            task_type: 任务类型标签。
            task_description: 任务描述（用于关键词匹配）。
            max_principles: 最多返回原则数。

        Returns:
            激活的原则列表 [{id, name, content, relevance}]。
        """
        pass

    def inject_to_graph(self, task_type: str) -> List[str]:
        """将激活的原则注入 EntityGraph，建立任务→原则关联边。

        Args:
            task_type: 任务类型。

        Returns:
            创建的边 ID 列表。
        """
        pass

    def inherit(
        self,
        source_domain: str,
        target_domain: str = "all",
        principle_ids: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        """执行原则单向扩散继承。

        work→all 或 life→all，权重按同步衰减系数传播。

        Args:
            source_domain: 源域 ('work' / 'life')。
            target_domain: 目标域（默认 'all'）。
            principle_ids: 要扩散的原则 ID（None = 全部）。

        Returns:
            {inherited_count, decayed_weights, affected_principles}。
        """
        pass

    def diffuse(self, principle_id: Optional[int] = None) -> Dict[str, Any]:
        """查询原则在域间的传播状态。

        返回当前激活域、传播路径、衰减后的权重。

        Args:
            principle_id: 原则 ID（None = 全部）。

        Returns:
            {principle_id: {active_domains, propagation_path, current_weight}}。
        """
        pass

    def evaluate(
        self,
        principle_id: int,
        scenario: str,
    ) -> Dict[str, Any]:
        """反事实评估: 对指定原则执行「如果违反会怎样」预演。

        为 Agent 提供非强制但充分的决策依据。

        Args:
            principle_id: 原则 ID。
            scenario: 当前决策场景描述。

        Returns:
            {principle, scenario, consequences, recommendation}。
        """
        pass

    def get_all_principles(self) -> List[Dict[str, Any]]:
        """获取全部 11 条核心原则。

        Returns:
            原则列表。
        """
        pass

    def get_by_domain(self, domain: str) -> List[Dict[str, Any]]:
        """按域获取原则。

        Args:
            domain: 'work' / 'life' / 'all'。

        Returns:
            该域的原则列表。
        """
        pass


def principle_activate(
    task_type: str,
    task_description: str = "",
    max_principles: int = 5,
) -> List[Dict[str, Any]]:
    """便捷函数: 激活原则。"""
    pass


def principle_inherit(
    source_domain: str,
    target_domain: str = "all",
    principle_ids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """便捷函数: 原则继承扩散。"""
    pass


def principle_diffuse(
    principle_id: Optional[int] = None,
) -> Dict[str, Any]:
    """便捷函数: 查询传播状态。"""
    pass


def principle_evaluate(
    principle_id: int,
    scenario: str,
) -> Dict[str, Any]:
    """便捷函数: 反事实评估。"""
    pass
```

- [ ] **Step 3: 验证导入**

```bash
cd "F:/Agent/Memory system" && python -c "from plastic_promise.principles import PrincipleManager; print('principles OK')"
```
Expected: `principles OK`

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "feat: principles/soul_principles.py skeleton — PrincipleManager + activate/inherit/diffuse/evaluate"
```

---

### Task 6: reflection/ 三模块骨架（~180 行签名）

**Files:**
- Create: `plastic_promise/reflection/__init__.py`
- Create: `plastic_promise/reflection/soul_scarf.py`
- Create: `plastic_promise/reflection/soul_proprioception.py`
- Create: `plastic_promise/reflection/soul_curiosity.py`

**Interfaces:**
- Consumes: `plastic_promise.core.constants` (SCARF_DIMENSIONS, INERTIA_*, CURIOSITY_*)
- Produces: `SCARFReflector`, `ProprioceptionManager`, `CuriosityExplorer`

- [ ] **Step 1: 写 reflection/__init__.py**

```python
"""认知系统（前额叶/探索欲）

包含:
- SCARF 五维度自省 (Status/Certainty/Autonomy/Relatedness/Fairness)
- 本体觉 + 惯性抑制
- 好奇心探索引擎
"""

from plastic_promise.reflection.soul_scarf import SCARFReflector, scarf_reflect
from plastic_promise.reflection.soul_proprioception import (
    ProprioceptionManager,
    inertia_check,
)
from plastic_promise.reflection.soul_curiosity import CuriosityExplorer, curiosity_explore

__all__ = [
    "SCARFReflector", "scarf_reflect",
    "ProprioceptionManager", "inertia_check",
    "CuriosityExplorer", "curiosity_explore",
]
```

- [ ] **Step 2: 写 soul_scarf.py 骨架**

```python
"""SCARF 五维度自省模块

对应数字身体中的「认知系统」（前额叶），成熟度 0.55。
五维度: Status(状态感知) / Certainty(确定性) / Autonomy(自主权)
        / Relatedness(关联性) / Fairness(公平性)。

崩溃前规模: ~3016 行。此文件为骨架重建。
"""

from typing import List, Dict, Optional, Any

from plastic_promise.core.constants import SCARF_DIMENSIONS


class SCARFReflector:
    """SCARF 五维度自省引擎。

    对 Agent 的行为进行结构化自我评估，
    每个维度 0.0–1.0 评分，低于 0.50 给出改进建议。

    Attributes:
        dimensions: 五维度配置。
        history: 历史自省记录。
    """

    def __init__(self) -> None:
        """初始化 SCARF 自省引擎。"""
        pass

    def reflect(
        self,
        context: str,
        dimensions: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """执行五维度自省。

        Args:
            context: 当前上下文/最近行为描述。
            dimensions: 指定维度（None = 全部）。

        Returns:
            {dimension_name: {score, assessment, suggestion}}。
        """
        pass

    def get_status_summary(self) -> Dict[str, Any]:
        """获取五维度状态摘要。

        Returns:
            {overall_score, weakest_dimension, strongest_dimension, trend}。
        """
        pass

    def compare_with_history(self, window: int = 10) -> Dict[str, Any]:
        """与历史自省对比，检测趋势变化。

        Args:
            window: 对比窗口大小。

        Returns:
            {dimension: {current, avg, trend_direction}}。
        """
        pass


def scarf_reflect(
    context: str,
    dimensions: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """便捷函数: SCARF 自省。"""
    pass
```

- [ ] **Step 3: 写 soul_proprioception.py 骨架**

```python
"""本体觉 + 惯性抑制模块

对应数字身体中的「认知系统」的自我感知层。
本体觉: 对自身行为的实时监控。
惯性抑制: 检测连续相似任务，防止行为固化。

崩溃前规模: ~2010 行。此文件为骨架重建。
"""

from typing import List, Dict, Optional, Any

from plastic_promise.core.constants import (
    INERTIA_SUPPRESSION_WINDOW,
    INERTIA_SUPPRESSION_THRESHOLD,
)


class ProprioceptionManager:
    """本体觉管理器。

    监控 Agent 自身行为模式，检测惯性趋势，
    在连续相似任务中触发探索建议。

    Attributes:
        recent_tasks: 最近任务描述队列。
        suppressed_count: 惯性抑制触发次数。
        window_size: 检测窗口大小。
        similarity_threshold: 相似度阈值。
    """

    def __init__(
        self,
        window_size: int = INERTIA_SUPPRESSION_WINDOW,
        threshold: float = INERTIA_SUPPRESSION_THRESHOLD,
    ) -> None:
        """初始化本体觉管理器。

        Args:
            window_size: 检测窗口大小。
            threshold: 惯性触发相似度阈值。
        """
        pass

    def check_inertia(
        self,
        recent_tasks: List[str],
    ) -> Dict[str, Any]:
        """惯性抑制检测。

        检查最近 N 个任务是否过于相似，
        如触发则给出探索建议。

        Args:
            recent_tasks: 最近任务描述列表。

        Returns:
            {inertia_detected, similarity_score, suggestion, suppressed}。
        """
        pass

    def record_task(self, task_description: str) -> None:
        """记录任务到历史队列。

        Args:
            task_description: 任务描述。
        """
        pass

    def get_pattern_analysis(self) -> Dict[str, Any]:
        """获取行为模式分析。

        Returns:
            {dominant_patterns, variety_score, suggestions}。
        """
        pass


def inertia_check(recent_tasks: List[str]) -> Dict[str, Any]:
    """便捷函数: 惯性检测。"""
    pass
```

- [ ] **Step 4: 写 soul_curiosity.py 骨架**

```python
"""好奇心探索引擎

对应数字身体中的「认知系统」的探索欲层。
使用 epsilon-greedy 策略平衡利用与探索，
主动发现新的知识领域和关联。

崩溃前规模: ~4590 行。此文件为骨架重建。
"""

from typing import List, Dict, Optional, Any

from plastic_promise.core.constants import CURIOSITY_EXPLORE_RATE


class CuriosityExplorer:
    """好奇心探索引擎。

    驱动 Agent 在执行已知任务的同时探索新领域。
    epsilon-greedy: 以 explore_rate 概率选择探索行为。

    Attributes:
        explore_rate: 探索率 (epsilon)。
        explored_topics: 已探索主题集合。
        exploration_history: 探索历史记录。
    """

    def __init__(self, explore_rate: float = CURIOSITY_EXPLORE_RATE) -> None:
        """初始化探索引擎。

        Args:
            explore_rate: 探索率 [0.0, 1.0]。
        """
        pass

    def should_explore(self) -> bool:
        """判断当前是否应该执行探索行为。

        Returns:
            bool: True 表示应探索。
        """
        pass

    def get_exploration_suggestion(
        self,
        current_context: str,
    ) -> Dict[str, Any]:
        """生成探索建议。

        基于已探索主题的盲区，提出新探索方向。

        Args:
            current_context: 当前任务上下文。

        Returns:
            {suggested_topic, rationale, expected_value}。
        """
        pass

    def record_exploration(
        self,
        topic: str,
        result: Dict[str, Any],
    ) -> None:
        """记录探索结果。

        Args:
            topic: 探索主题。
            result: 探索结果。
        """
        pass

    def get_exploration_stats(self) -> Dict[str, Any]:
        """获取探索统计。

        Returns:
            {total_explorations, topics_covered, blind_spots, explore_ratio}。
        """
        pass


def curiosity_explore(current_context: str) -> Dict[str, Any]:
    """便捷函数: 获取探索建议。"""
    pass
```

- [ ] **Step 5: 验证导入**

```bash
cd "F:/Agent/Memory system" && python -c "from plastic_promise.reflection import SCARFReflector, ProprioceptionManager, CuriosityExplorer; print('reflection OK')"
```
Expected: `reflection OK`

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: reflection/ skeleton — SCARFReflector + ProprioceptionManager + CuriosityExplorer"
```

---

### Task 7: defense/ 两模块骨架（~120 行签名）

**Files:**
- Create: `plastic_promise/defense/__init__.py`
- Create: `plastic_promise/defense/soul_enforcer.py`
- Create: `plastic_promise/defense/soul_audit.py`

**Interfaces:**
- Consumes: `plastic_promise.core.constants` (DEFENSE_LAYERS, AUDIT_DIMENSIONS, TRUST_*)
- Produces: `SoulEnforcer`, `SoulAuditor`

- [ ] **Step 1: 写 defense/__init__.py**

```python
"""免疫 + 反射弧系统

包含:
- 三层防线 (L0硬边界/L1约束衰减/L2免疫巡检)
- 七维度审计 (原则联想/记忆供应/约束合规/反馈闭环/信任校准/原则继承/安全追溯)
"""

from plastic_promise.defense.soul_enforcer import SoulEnforcer, TrustManager
from plastic_promise.defense.soul_audit import SoulAuditor, AuditReport

__all__ = [
    "SoulEnforcer", "TrustManager",
    "SoulAuditor", "AuditReport",
]
```

- [ ] **Step 2: 写 soul_enforcer.py 骨架**

```python
"""三层防线执行引擎

对应数字身体中的「反射弧」（脊髓反射/条件反射），成熟度 0.80。
L0: 硬边界 — 绝对不可逾越，pre_check 实时拦截。
L1: 约束衰减 — 信任分驱动的动态约束，L1↔L0 切换。
L2: 免疫巡检 — 周期性扫描和自动修复。

崩溃前规模: ~1024 行。此文件为骨架重建。
"""

from typing import Dict, List, Optional, Any

from plastic_promise.core.constants import (
    DEFENSE_LAYERS,
    TRUST_INITIAL,
    TRUST_DECAY_RATE,
    TRUST_BOOST_RATE,
    TRUST_MIN,
    TRUST_MAX,
    TRUST_TIER_HIGH,
    TRUST_TIER_MEDIUM,
    TRUST_TIER_LOW,
    TRUST_TIER_CRITICAL,
)


class TrustManager:
    """信任分管理器。

    管理信任分的增加、衰减、查询和历史追踪。
    信任分驱动 L1↔L0 的动态约束切换。

    Attributes:
        trust_score: 当前信任分。
        history: 信任分变化历史列表。
    """

    def __init__(self, initial_trust: float = TRUST_INITIAL) -> None:
        """初始化信任分管理器。

        Args:
            initial_trust: 初始信任分。
        """
        pass

    def boost(self, delta: float, reason: str = "") -> float:
        """增加信任分。

        Args:
            delta: 增加量（通常 TRUST_BOOST_RATE = 0.02）。
            reason: 增加原因。

        Returns:
            新的信任分值。
        """
        pass

    def decay(self, delta: float = TRUST_DECAY_RATE, reason: str = "") -> float:
        """衰减信任分。

        Args:
            delta: 衰减量（默认 TRUST_DECAY_RATE = 0.005）。
            reason: 衰减原因。

        Returns:
            新的信任分值。
        """
        pass

    def get(self) -> float:
        """获取当前信任分。

        Returns:
            当前信任分 [TRUST_MIN, TRUST_MAX]。
        """
        pass

    def history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """获取信任分变化历史。

        Args:
            limit: 返回条数上限。

        Returns:
            变化记录列表 [{timestamp, from, to, reason}]。
        """
        pass

    @property
    def tier(self) -> str:
        """当前信任等级: high/medium/low/critical。"""
        pass

    @property
    def autonomy_level(self) -> str:
        """当前自主权级别: full/standard/restricted/minimal。"""
        pass


class SoulEnforcer:
    """三层防线执行器。

    在每次操作前执行三层检查:
    1. L0 硬边界 — 绝对不可逾越
    2. L1 约束衰减 — 信任分驱动的动态调整
    3. L2 免疫巡检 — 周期性扫描

    Attributes:
        trust: TrustManager 实例。
        l0_rules: L0 硬边界规则列表。
        violations: 违规记录。
    """

    def __init__(self, trust_manager: Optional[TrustManager] = None) -> None:
        """初始化防线执行器。

        Args:
            trust_manager: 信任分管理器（可选）。
        """
        pass

    def pre_check(
        self,
        action_description: str,
        action_type: str = "exec",
    ) -> Dict[str, Any]:
        """操作前实时合规检查。

        依次执行 L0 → L1 → L2 三层检查。

        Args:
            action_description: 操作描述。
            action_type: 操作类型 (exec/write/edit/delete/read)。

        Returns:
            {passed, layer_checks: [{layer, passed, reason}], risk_score}。
        """
        pass

    def get_defense_status(self) -> Dict[str, Any]:
        """获取三层防线当前状态。

        Returns:
            {L0: {status, active_rules}, L1: {status, trust_driven, autonomy},
             L2: {status, last_scan, next_scan}}。
        """
        pass

    def log_violation(
        self,
        action: str,
        layer: str,
        reason: str,
    ) -> None:
        """记录违规事件。

        Args:
            action: 违规操作描述。
            layer: 触发层级 (L0/L1/L2)。
            reason: 违规原因。
        """
        pass

    def get_violation_stats(self) -> Dict[str, Any]:
        """获取违规统计。

        Returns:
            {total, by_layer, by_type, recent}。
        """
        pass
```

- [ ] **Step 3: 写 soul_audit.py 骨架**

```python
"""审计系统模块

对应数字身体中的「免疫系统」（免疫细胞/抗体），成熟度 0.70。
七维度审计:
1. 原则联想 2. 记忆供应 3. 约束合规 4. 反馈闭环
5. 信任校准 6. 原则继承 7. 安全追溯

崩溃前规模: ~5900 行。此文件为骨架重建。
"""

import datetime
from typing import Dict, List, Optional, Any

from plastic_promise.core.constants import (
    AUDIT_DIMENSIONS,
    PRE_CHECK_ALERT_THRESHOLD,
)


class AuditReport:
    """审计报告。

    包含七维度评分、发现的问题、建议的修复措施。

    Attributes:
        dimensions: 七维度评分映射。
        findings: 发现的问题列表。
        overall_score: 加权总分。
        timestamp: 审计时间戳。
    """

    def __init__(self) -> None:
        """创建空白审计报告。"""
        pass

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典。

        Returns:
            dict: 完整审计报告。
        """
        pass

    def to_json(self) -> str:
        """序列化为 JSON 字符串。

        Returns:
            str: JSON 格式的审计报告。
        """
        pass

    def to_markdown(self) -> str:
        """格式化为 Markdown 报告。

        Returns:
            str: Markdown 格式。
        """
        pass


class SoulAuditor:
    """审计执行器。

    执行七维度审计、回顾审计和实时 pre_check 审计。

    Attributes:
        reports: 历史审计报告列表。
        pre_check_log: pre_check 日志。
    """

    def __init__(self) -> None:
        """初始化审计器。"""
        pass

    def run_audit(
        self,
        scope: str = "full",
        time_range_hours: Optional[int] = None,
    ) -> AuditReport:
        """执行七维度审计。

        对每个维度进行结构化评分 (0.0–1.0)，
        发现的问题按严重程度分类，
        评分 < 0.60 标记为 P0 并告警。

        Args:
            scope: 审计范围 — 'full' / 'quick' / 'principles_only' / 'memory_only'。
            time_range_hours: 审计时间范围（小时）。

        Returns:
            AuditReport 包含七维度评分和发现。
        """
        pass

    def pre_check(self, action_description: str, action_type: str = "exec") -> Dict[str, Any]:
        """实时合规检查。

        对即将执行的操作进行 L0 硬边界和 L1 约束衰减检查。
        合规率 < 50% 自动告警。

        Args:
            action_description: 操作描述。
            action_type: 操作类型。

        Returns:
            {passed, compliance_score, violations}。
        """
        pass

    def get_report(
        self,
        dimension: Optional[str] = None,
        format: str = "json",
    ) -> Any:
        """获取最近一次审计报告。

        Args:
            dimension: 指定维度（None = 全部）。
            format: 输出格式 — 'json' / 'markdown' / 'summary'。

        Returns:
            审计报告或指定维度数据。
        """
        pass

    def get_compliance_rate(self) -> float:
        """获取当前合规率。

        Returns:
            合规率 [0.0, 1.0]。
        """
        pass

    def get_alert_status(self) -> Dict[str, Any]:
        """获取告警状态。

        Returns:
            {active_alerts, compliance_rate, needs_attention}。
        """
        pass
```

- [ ] **Step 4: 验证导入**

```bash
cd "F:/Agent/Memory system" && python -c "from plastic_promise.defense import SoulEnforcer, TrustManager, SoulAuditor, AuditReport; print('defense OK')"
```
Expected: `defense OK`

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: defense/ skeleton — SoulEnforcer + TrustManager + SoulAuditor + AuditReport"
```

---

### Task 8: growth/ 三模块骨架（~150 行签名）

**Files:**
- Create: `plastic_promise/growth/__init__.py`
- Create: `plastic_promise/growth/soul_hormone.py`
- Create: `plastic_promise/growth/soul_classifier.py`
- Create: `plastic_promise/growth/skill_extractor.py`

**Interfaces:**
- Consumes: `plastic_promise.core.constants` (CLASSIFIER_*, TRUST_*)
- Produces: `HormoneEngine`, `TaskClassifier`, `SkillExtractor`

- [ ] **Step 1: 写 growth/__init__.py**

```python
"""内分泌 + 技能沉淀系统

包含:
- 实时反馈激素 (评价引擎 + 信任分联动)
- 任务分类器 (45关键词 + ACP路由)
- 技能沉淀提取
"""

from plastic_promise.growth.soul_hormone import HormoneEngine, EmotionAccount
from plastic_promise.growth.soul_classifier import TaskClassifier, classify_task
from plastic_promise.growth.skill_extractor import SkillExtractor, extract_skill

__all__ = [
    "HormoneEngine", "EmotionAccount",
    "TaskClassifier", "classify_task",
    "SkillExtractor", "extract_skill",
]
```

- [ ] **Step 2: 写 soul_hormone.py 骨架**

```python
"""实时反馈激素系统

对应数字身体中的「内分泌系统」（激素调节），成熟度 0.65。
评价引擎 + 情感账户 + 信任分联动:
行为 → 评价 → 信任变化 → 自主权调整。
dopamine: 正向反馈激素 (成功/采纳)
cortisol: 压力激素 (失败/拒绝)

崩溃前规模: ~2310 行。此文件为骨架重建。
"""

from typing import Dict, Optional, Any

from plastic_promise.core.constants import (
    TRUST_BOOST_RATE,
    TRUST_DECAY_RATE,
    ASSOCIATION_WEIGHTS,
)


class EmotionAccount:
    """情感账户。

    追踪正向/负向行为累计，计算情感余额。
    高余额 → 高信任 → 高自主权。

    Attributes:
        balance: 当前情感余额。
        transactions: 交易历史记录。
    """

    def __init__(self) -> None:
        """初始化情感账户。"""
        pass

    def deposit(self, amount: float, reason: str = "") -> float:
        """正向存款（采纳/成功）。

        Args:
            amount: 存款额。
            reason: 原因。

        Returns:
            新的余额。
        """
        pass

    def withdraw(self, amount: float, reason: str = "") -> float:
        """负向取款（拒绝/失败）。

        Args:
            amount: 取款额。
            reason: 原因。

        Returns:
            新的余额。
        """
        pass

    def get_balance(self) -> float:
        """获取当前余额。"""
        pass


class HormoneEngine:
    """激素调节引擎。

    根据行为结果释放 dopamine（奖励）或 cortisol（压力），
    更新情感账户，联动信任分调整。

    Attributes:
        account: EmotionAccount 实例。
        dopamine_level: 当前多巴胺水平。
        cortisol_level: 当前皮质醇水平。
    """

    def __init__(self, trust_manager: Optional[Any] = None) -> None:
        """初始化激素引擎。

        Args:
            trust_manager: 信任分管理器引用。
        """
        pass

    def apply_feedback(
        self,
        feedback_type: str,
        intensity: float = 1.0,
        context: str = "",
    ) -> Dict[str, Any]:
        """根据反馈类型释放激素。

        adopted → dopamine + trust boost
        rejected → cortisol + trust decay
        ignored → mild cortisol

        Args:
            feedback_type: 'adopted' / 'ignored' / 'rejected'。
            intensity: 反馈强度 [0.0, 1.0]。
            context: 上下文描述。

        Returns:
            {hormone_changes, trust_delta, account_balance}。
        """
        pass

    def get_hormone_status(self) -> Dict[str, Any]:
        """获取当前激素水平。

        Returns:
            {dopamine, cortisol, d_c_ratio, mood}。
        """
        pass
```

- [ ] **Step 3: 写 soul_classifier.py 骨架**

```python
"""任务分类器 + ACP 路由

Claude Code 常态化入口:
- 45 关键词分类，准确率 ≥ 90%
- 阈值 score ≥ 3 → Claude Code
- 阈值 score ≥ 5 → ACP (含 MCP 注入)
- 短指令误判修复

崩溃前规模: ~3010 行。此文件为骨架重建。
"""

from typing import Dict, List, Tuple, Optional

from plastic_promise.core.constants import (
    CLASSIFIER_KEYWORDS,
    CLASSIFIER_THRESHOLD_CLAUDE,
    CLASSIFIER_THRESHOLD_ACP,
)


class TaskClassifier:
    """任务分类器。

    基于 45 关键词对用户指令进行分类，
    决定路由路径 (Claude Code / ACP / 其他)。

    Attributes:
        keywords: 分类关键词列表（6大类别）。
        threshold_claude: Claude Code 路由阈值。
        threshold_acp: ACP 路由阈值。
    """

    def __init__(self) -> None:
        """初始化分类器。"""
        pass

    def classify(
        self,
        instruction: str,
    ) -> Dict[str, Any]:
        """对用户指令进行分类。

        Args:
            instruction: 用户原始指令。

        Returns:
            {score, category, route, matched_keywords, confidence}。
        """
        pass

    def route(self, instruction: str) -> str:
        """根据分类结果返回路由路径。

        Args:
            instruction: 用户原始指令。

        Returns:
            路由路径: 'claude_print' / 'acpx_claude_exec' / 'other'。
        """
        pass

    def batch_classify(
        self,
        instructions: List[str],
    ) -> List[Dict[str, Any]]:
        """批量分类。

        Args:
            instructions: 指令列表。

        Returns:
            分类结果列表。
        """
        pass

    @property
    def accuracy_stats(self) -> Dict[str, Any]:
        """分类准确率统计。"""
        pass


def classify_task(instruction: str) -> Dict[str, Any]:
    """便捷函数: 分类任务。"""
    pass
```

- [ ] **Step 4: 写 skill_extractor.py 骨架**

```python
"""技能沉淀提取器

从任务经验中提取可复用的技能模式，
自动发现重复模式并沉淀为结构化技能条目。

崩溃前规模: ~4400 行。此文件为骨架重建。
"""

from typing import Dict, List, Optional, Any


class SkillExtractor:
    """技能沉淀引擎。

    分析任务历史，识别重复模式，
    提取可复用的技能知识和最佳实践。

    Attributes:
        skills: 已沉淀的技能集合。
        patterns: 识别的行为模式。
    """

    def __init__(self) -> None:
        """初始化技能提取器。"""
        pass

    def extract(
        self,
        task_description: str,
        task_result: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """从单次任务中尝试提取技能。

        Args:
            task_description: 任务描述。
            task_result: 任务执行结果。

        Returns:
            提取的技能条目，或 None（无新技能）。
        """
        pass

    def get_all_skills(self) -> List[Dict[str, Any]]:
        """获取所有已沉淀技能。

        Returns:
            技能列表，按使用频率降序。
        """
        pass

    def find_duplicates(self) -> List[Tuple[str, str]]:
        """查找重复技能。

        Returns:
            [(skill_a_id, skill_b_id, similarity)] 列表。
        """
        pass

    def merge_skills(
        self,
        skill_a_id: str,
        skill_b_id: str,
    ) -> Dict[str, Any]:
        """合并两个重复技能。

        Args:
            skill_a_id: 保留的技能 ID。
            skill_b_id: 被合并的技能 ID。

        Returns:
            合并后的技能。
        """
        pass

    def get_stats(self) -> Dict[str, Any]:
        """获取技能库统计。

        Returns:
            {total_skills, by_category, new_this_week, top_skills}。
        """
        pass


def extract_skill(
    task_description: str,
    task_result: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """便捷函数: 提取技能。"""
    pass
```

- [ ] **Step 5: 验证导入**

```bash
cd "F:/Agent/Memory system" && python -c "from plastic_promise.growth import HormoneEngine, TaskClassifier, SkillExtractor; print('growth OK')"
```
Expected: `growth OK`

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: growth/ skeleton — HormoneEngine + TaskClassifier + SkillExtractor"
```

---

### Task 9: mcp/ 工具拆分 + resources + prompts（~400 行签名）

**Files:**
- Create: `plastic_promise/mcp/tools/memory.py`
- Create: `plastic_promise/mcp/tools/principles.py`
- Create: `plastic_promise/mcp/tools/context.py`
- Create: `plastic_promise/mcp/tools/audit_defense.py`
- Create: `plastic_promise/mcp/tools/reflection.py`
- Create: `plastic_promise/mcp/tools/management.py`
- Create: `plastic_promise/mcp/resources.py`
- Create: `plastic_promise/mcp/prompts.py`
- Modify: `plastic_promise/mcp/server.py` — 将 stub handler 替换为从 tools/ 导入

**Interfaces:**
- Consumes: `plastic_promise.core.*`, `plastic_promise.memory.*`, `plastic_promise.principles.*`, `plastic_promise.defense.*`, `plastic_promise.reflection.*`, `plastic_promise.growth.*`, `plastic_promise.loop.*`
- Produces: 每个 tool 文件导出一个 `register_*_tools(api, context)` 函数或同签名的 async handler 函数列表

- [ ] **Step 1: 写 mcp/resources.py**

```python
"""MCP Resources — 系统数据的只读视图

暴露为 MCP Resource 的系统数据:
- plastic-promise://principles — 11条核心原则
- plastic-promise://systems — 九大数字身体系统
- plastic-promise://trust-history — 信任分历史
- plastic-promise://audit-latest — 最新审计报告
- plastic-promise://memory-stats — 记忆池统计
"""

import json
from typing import List


def get_resource_list() -> List[dict]:
    """返回所有可用的 MCP Resource 定义。

    Returns:
        Resource 定义列表，每项含 uri/name/description/mimeType。
    """
    pass


def read_resource(uri: str) -> str:
    """读取指定 Resource 的当前数据。

    Args:
        uri: 资源 URI (如 plastic-promise://principles)。

    Returns:
        JSON 字符串格式的资源数据。
    """
    pass
```

- [ ] **Step 2: 写 mcp/prompts.py**

```python
"""MCP Prompts — 标准操作流程模板

暴露为 MCP Prompt 的标准操作流程:
- run-full-audit — 执行完整的七维度审计
- check-principle-alignment — 检查决策与原则对齐
- daily-reflection — 每日 SCARF 自省 + 记忆演化检查
"""

from typing import List, Dict, Optional


def get_prompt_list() -> List[dict]:
    """返回所有可用的 MCP Prompt 定义。

    Returns:
        Prompt 定义列表，每项含 name/description/arguments。
    """
    pass


def get_prompt(
    name: str,
    arguments: Optional[Dict[str, str]] = None,
) -> dict:
    """获取指定 Prompt 模板的内容。

    Args:
        name: Prompt 名称。
        arguments: Prompt 参数。

    Returns:
        {messages: [{role, content}]} 格式的 Prompt 结果。
    """
    pass
```

- [ ] **Step 3: 写 mcp/tools/memory.py**

```python
"""记忆域 MCP 工具处理器 (7 tools)

memory_recall / memory_store / memory_update / memory_forget
/ memory_stats / memory_list / memory_gc
"""

import json
from typing import Any


async def handle_memory_recall(engine: Any, args: dict) -> Any:
    """处理 memory_recall 工具调用。

    调用 ContextEngine.supply() 进行混合检索，
    返回三层上下文包 JSON。

    Args:
        engine: ContextEngine 实例。
        args: {query, task_type?, max_results?, min_relevance?, include_principles?}。

    Returns:
        list[TextContent]: MCP 响应。
    """
    pass


async def handle_memory_store(engine: Any, args: dict) -> Any:
    """处理 memory_store 工具调用。

    存储一条新记忆，执行去重检查和噪声过滤。

    Args:
        engine: ContextEngine 实例。
        args: {content, memory_type?, source?, entity_ids?}。

    Returns:
        list[TextContent]: 包含新记忆 ID 的 MCP 响应。
    """
    pass


async def handle_memory_update(engine: Any, args: dict) -> Any:
    """处理 memory_update 工具调用。

    Args:
        engine: ContextEngine 实例。
        args: {memory_id, content?, reset_worth?}。

    Returns:
        list[TextContent]: MCP 响应。
    """
    pass


async def handle_memory_forget(engine: Any, args: dict) -> Any:
    """处理 memory_forget 工具调用。

    Args:
        engine: ContextEngine 实例。
        args: {memory_id, reason?}。

    Returns:
        list[TextContent]: MCP 响应。
    """
    pass


async def handle_memory_stats(engine: Any, args: dict) -> Any:
    """处理 memory_stats 工具调用。

    Args:
        engine: ContextEngine 实例。
        args: {}。

    Returns:
        list[TextContent]: 记忆池统计 JSON。
    """
    pass


async def handle_memory_list(engine: Any, args: dict) -> Any:
    """处理 memory_list 工具调用。

    Args:
        engine: ContextEngine 实例。
        args: {memory_type?, source?, min_worth?, limit?}。

    Returns:
        list[TextContent]: MCP 响应。
    """
    pass


async def handle_memory_gc(engine: Any, args: dict) -> Any:
    """处理 memory_gc 工具调用。

    Args:
        engine: ContextEngine 实例。
        args: {dry_run?, force?}。

    Returns:
        list[TextContent]: MCP 响应。
    """
    pass
```

- [ ] **Step 4: 写其余 5 个 tool 文件**

Write `mcp/tools/principles.py`, `mcp/tools/context.py`, `mcp/tools/audit_defense.py`, `mcp/tools/reflection.py`, `mcp/tools/management.py` — each following the same pattern as memory.py, with async handler functions matching the tool names declared in server.py's `list_tools()`.

- [ ] **Step 5: 重写 server.py call_tool 为委托模式**

Modify `server.py`:
- Remove all `async def _handle_*` stub functions
- Replace `call_tool` body with import-based delegation:

```python
@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    engine = get_engine()
    try:
        # 记忆域
        if name == "memory_recall":
            from plastic_promise.mcp.tools.memory import handle_memory_recall
            return await handle_memory_recall(engine, arguments)
        elif name == "memory_store":
            from plastic_promise.mcp.tools.memory import handle_memory_store
            return await handle_memory_store(engine, arguments)
        # ... (其余 25 个工具同理)
        else:
            return [TextContent(type="text", text=json.dumps(
                {"error": f"Unknown tool: {name}"}, ensure_ascii=False))]
    except Exception as e:
        logging.exception(f"Tool {name} failed")
        return [TextContent(type="text", text=json.dumps(
            {"error": str(e), "tool": name}, ensure_ascii=False))]
```

- [ ] **Step 6: 验证全部导入链**

```bash
cd "F:/Agent/Memory system" && python -c "
from plastic_promise.core import CORE_PRINCIPLES, ContextEngine
from plastic_promise.memory import RecMem, MemoryRecord
from plastic_promise.loop import SoulLoop
from plastic_promise.principles import PrincipleManager
from plastic_promise.reflection import SCARFReflector, ProprioceptionManager, CuriosityExplorer
from plastic_promise.defense import SoulEnforcer, SoulAuditor
from plastic_promise.growth import HormoneEngine, TaskClassifier, SkillExtractor
print('All imports OK')
"
```
Expected: `All imports OK`

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat: mcp/ tool split + resources + prompts — 17 handlers delegated"
```

---

### Task 10: Rust .rs 文件 doc comment 补全

**Files:**
- Modify: `rust/context-engine-core/src/lib.rs`
- Modify: `rust/context-engine-core/src/entity_graph.rs`
- Modify: `rust/context-engine-core/src/rank_fuser.rs`
- Modify: `rust/context-engine-core/src/memory_worth.rs`
- Modify: `rust/context-engine-core/src/context_engine.rs`
- Modify: `rust/context-engine-core/src/principles.rs`
- Modify: `rust/context-engine-core/src/source_tracker.rs`
- Modify: `rust/context-engine-core/src/association_feedback.rs`

**Interfaces:**
- Consumes: 无
- Produces: 每个 Rust 文件的所有 `pub` 项有 `///` doc comment

- [ ] **Step 1: 为每个 .rs 文件的 pub 项添加 doc comment**

对每个文件中所有 `pub struct`、`pub fn`、`pub enum`、`pub trait`、`pub impl`、`#[pymethods]` 块添加 `///` 三重斜线注释。

- [ ] **Step 2: 验证 Rust 编译**

```bash
cd rust/context-engine-core && cargo check 2>&1
```
Expected: 0 errors

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "docs: Rust doc comments on all public items"
```

---

### Task 11: 全链路集成验证

- [ ] **Step 1: 完整导入链路测试**

```bash
cd "F:/Agent/Memory system" && python -c "
# 全链路导入 — core → subsystems → mcp
from plastic_promise.core import *
from plastic_promise.memory import *
from plastic_promise.loop import *
from plastic_promise.principles import *
from plastic_promise.reflection import *
from plastic_promise.defense import *
from plastic_promise.growth import *
print('Full import chain OK')
# 验证关键常量
from plastic_promise.core.constants import CORE_PRINCIPLES
assert len(CORE_PRINCIPLES) == 11, f'Expected 11 principles, got {len(CORE_PRINCIPLES)}'
print(f'All {len(CORE_PRINCIPLES)} principles present')
"
```
Expected: `Full import chain OK` + `All 11 principles present`

- [ ] **Step 2: 文件清单验证**

```bash
find plastic_promise -name "*.py" | wc -l
```
Expected: 26+ Python files

- [ ] **Step 3: 最终 Commit**

```bash
git add -A
git commit -m "verify: full import chain passes, 11 principles, 26+ module files"
```
