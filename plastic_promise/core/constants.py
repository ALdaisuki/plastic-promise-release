"""Plastic Promise — 核心常量、配置与原则定义

服务器崩溃后从对话总结重建。所有魔法数字和阈值集中管理于此。
"""

# ============================================================
# 九大数字身体系统
# ============================================================

DIGITAL_BODY_SYSTEMS = {
    "sensory": {
        "name": "感官系统",
        "analogy": "视觉、听觉、触觉",
        "modules": ["memory_recall", "GitNexus", "code_search"],
        "maturity": 0.70,
        "description": "感知外部世界和内部状态的能力",
    },
    "motor": {
        "name": "运动系统",
        "analogy": "手、脚、协作能力",
        "modules": ["exec", "write", "edit", "ACP", "Beads"],
        "maturity": 0.75,
        "description": "对外执行操作和修改的能力",
    },
    "memory": {
        "name": "记忆系统",
        "analogy": "海马体、大脑皮层",
        "modules": ["soul_memory"],
        "subsystems": ["双层三域", "L1/L3分层", "RecMem", "EvolveR", "GC"],
        "maturity": 0.90,
        "description": "双层三域架构 + 四系统融合记忆管理",
    },
    "reflex": {
        "name": "反射弧",
        "analogy": "脊髓反射、条件反射",
        "modules": ["soul_enforcer"],
        "subsystems": ["三层防线", "约束衰减"],
        "maturity": 0.80,
        "description": "快速反应，无需经过高层决策的自动防护",
    },
    "endocrine": {
        "name": "内分泌系统",
        "analogy": "激素调节",
        "modules": ["soul_hormone"],
        "subsystems": ["评价引擎", "信任分", "情感账户"],
        "maturity": 0.65,
        "description": "实时反馈激素，调控内部状态",
    },
    "immune": {
        "name": "免疫系统",
        "analogy": "免疫细胞、抗体",
        "modules": ["soul_audit"],
        "subsystems": ["七维度审计", "每日cron", "回顾审计"],
        "maturity": 0.70,
        "description": "检测和修复系统异常",
    },
    "genetic": {
        "name": "遗传系统",
        "analogy": "DNA、基因遗传",
        "modules": ["soul_principles"],
        "subsystems": ["原则继承", "单向扩散", "同步衰减"],
        "maturity": 0.60,
        "description": "核心约定跨 Agent 代际传递",
    },
    "autonomic": {
        "name": "自主神经",
        "analogy": "心跳、呼吸、体温",
        "modules": ["scan_and_fix", "HEARTBEAT"],
        "maturity": 0.60,
        "description": "周期性自检和生命维持",
    },
    "cognitive": {
        "name": "认知系统",
        "analogy": "前额叶、探索欲",
        "modules": ["soul_scarf", "soul_curiosity"],
        "subsystems": ["SCARF自省", "好奇心探索", "反思引擎"],
        "maturity": 0.55,
        "description": "元认知、自我反思和主动探索",
    },
}

# ============================================================
# 三层防线参数
# ============================================================

DEFENSE_LAYERS = {
    "L0": {
        "name": "硬边界",
        "description": "绝对不可逾越的规则边界",
        "priority": 0,  # 最高优先级
        "enforcement": "pre_check",
        "violation_action": "block",
    },
    "L1": {
        "name": "约束衰减",
        "description": "信任分驱动的动态约束，信任换自主",
        "priority": 1,
        "enforcement": "constraint_decay",
        "violation_action": "warn_and_log",
        "trust_threshold_loosen": 0.80,  # 信任分高于此值放宽约束
        "trust_threshold_tighten": 0.40,  # 信任分低于此值收紧约束
    },
    "L2": {
        "name": "免疫巡检",
        "description": "周期性扫描和自动修复",
        "priority": 2,
        "enforcement": "cron_scan",
        "violation_action": "auto_fix",
        "scan_interval_hours": 24,
    },
}

# ============================================================
# 信任分机制
# ============================================================

TRUST_INITIAL = 0.60            # 初始信任分
TRUST_DECAY_RATE = 0.005        # 每次低信任行为衰减
TRUST_BOOST_RATE = 0.02         # 每次高信任行为增长
TRUST_MIN = 0.10                # 最低信任分
TRUST_MAX = 1.00                # 最高信任分

TRUST_TIER_HIGH = 0.80          # 高信任：自主权最大
TRUST_TIER_MEDIUM = 0.50        # 中信任：标准约束
TRUST_TIER_LOW = 0.30           # 低信任：收紧约束
TRUST_TIER_CRITICAL = 0.15      # 临界：几乎全部约束

# ============================================================
# 审计维度权重（七维度）
# ============================================================
# 维度映射自 SCARF 五维度扩展为七维度审计框架

AUDIT_DIMENSIONS = {
    "simplicity": {
        "name": "奥卡姆剃刀",
        "weight": 0.15,
        "description": "方案是否最简洁？是否存在不必要的实体或步骤？每一步只做当前最必要的事。",
        "principle_id": 1,
    },
    "transparency": {
        "name": "全过程可查可透明",
        "weight": 0.15,
        "description": "每步是否有完整 git 痕迹？审计日志是否可追溯？中间产物是否可验证？",
        "principle_id": 2,
    },
    "audit_closure": {
        "name": "自我审计闭环",
        "weight": 0.15,
        "description": "是否有根因分析？是否有改良措施？是否提炼了可迁移教训？量化评分是否准确？",
        "principle_id": 3,
    },
    "principle_activation": {
        "name": "原则激活率",
        "weight": 0.15,
        "description": "每次任务是否自动激活了相关原则？激活的原则是否被实际遵循？是否存在原则\"休眠\"？",
        "principle_id": 4,
    },
    "memory_supply": {
        "name": "记忆供给质量",
        "weight": 0.15,
        "description": "上下文供给是否充分？记忆召回的相关性和时效性如何？三层上下文包的比例是否合理？",
        "principle_id": 4,
    },
    "constraint_compliance": {
        "name": "约束合规度",
        "weight": 0.15,
        "description": "L0 硬边界是否有违规？L1 动态约束是否按信任分正确调整？L2 免疫巡检是否按时执行？",
        "principle_id": 9,
    },
    "feedback_closure": {
        "name": "反馈闭环率",
        "weight": 0.10,
        "description": "每次交互是否产生了反馈信号？adopted/rejected/ignored 的分布是否健康？反馈是否驱动了行为修正？",
        "principle_id": 10,
    },
}

# ============================================================
# CEI 约定作用指数 阈值
# ============================================================

CEI_THRESHOLDS = {
    "nascent": (0.0, 0.30),       # 约定萌芽
    "growing": (0.30, 0.50),      # 约定生长
    "forming": (0.50, 0.65),      # 约定成形
    "internalizing": (0.65, 0.80), # 约定内化
    "mature": (0.80, 0.95),       # 约定成熟
    "autonomous": (0.95, 1.00),   # 约定自主
}

# ============================================================
# SCARF 五维度自省
# ============================================================

SCARF_DIMENSIONS = {
    "Status": {
        "name": "状态感知",
        "question": "当前系统运行状态是否正常？",
        "weight": 0.20,
    },
    "Certainty": {
        "name": "确定性",
        "question": "当前决策是否有充分依据？",
        "weight": 0.20,
    },
    "Autonomy": {
        "name": "自主权",
        "question": "当前行为是否在授权范围内？",
        "weight": 0.20,
    },
    "Relatedness": {
        "name": "关联性",
        "question": "当前行为是否与核心约定对齐？",
        "weight": 0.20,
    },
    "Fairness": {
        "name": "公平性",
        "question": "当前决策是否公平、一致？",
        "weight": 0.20,
    },
}

# ============================================================
# 上下文供应引擎参数
# ============================================================

CONTEXT_LAYERS = {
    "core": {
        "name": "🔵 核心层",
        "description": "必读——与当前任务直接关联的最高优先级上下文",
        "max_items": 5,
        "min_relevance": 0.80,
    },
    "related": {
        "name": "🟡 关联层",
        "description": "补充——间接关联或可能帮助决策的上下文",
        "max_items": 10,
        "min_relevance": 0.50,
    },
    "divergent": {
        "name": "🟢 发散层",
        "description": "灵感——低关联但有创意价值的联想",
        "max_items": 5,
        "min_relevance": 0.20,
    },
}

# RRF (Reciprocal Rank Fusion) 参数
RRF_K = 60                       # RRF 平滑常数

# 符号规则关键词分类（6类）
SYMBOL_RULE_KEYWORDS = {
    "security": ["安全", "漏洞", "权限", "密钥", "认证", "授权", "加密", "注入"],
    "quality": ["质量", "测试", "覆盖率", "性能", "优化", "重构", "代码审查"],
    "commitment": ["约定", "原则", "信任", "承诺", "边界", "伦理", "责任"],
    "learning": ["学习", "反思", "技能", "演化", "适应", "成长", "进步"],
    "collaboration": ["协作", "沟通", "共享", "同步", "对齐", "透明"],
    "innovation": ["创新", "探索", "实验", "尝试", "假设", "新思路"],
}

# ============================================================
# 自演化反馈权重
# ============================================================

ASSOCIATION_WEIGHTS = {
    "adopted": +0.10,    # 被采纳——加强关联
    "ignored": -0.05,    # 被忽略——轻微衰减
    "rejected": -0.20,   # 被拒绝——显著衰减
}

# ============================================================
# 记忆系统参数
# ============================================================

MEMORY_TIERS = {
    "L1": {
        "name": "工作记忆",
        "max_items": 200,
        "ttl_hours": 24,
        "description": "当天任务相关的短期活跃记忆",
    },
    "L3": {
        "name": "长期记忆",
        "max_items": 2000,
        "ttl_hours": None,  # 永久
        "description": "跨会话持久化的核心记忆",
    },
}

MEMORY_HEALTH_THRESHOLD = 80     # 健康记忆占比目标（百分比）
MEMORY_DECAY_THRESHOLD = 0.10    # 低于此 worth 的记忆标记为衰退
MEMORY_GC_INTERVAL_DAYS = 7      # 垃圾回收间隔（天）

# Memory Worth 双计数器参数
WORTH_SUCCESS_WEIGHT = 1.0       # 成功权重
WORTH_FAILURE_WEIGHT = -1.5      # 失败权重（惩罚大于奖励）
WORTH_MIN_OBSERVATIONS = 5       # 最少观察次数才启用 worth 信号

# ============================================================
# 11 条核心原则
# ============================================================

CORE_PRINCIPLES = [
    {
        "id": 1,
        "name": "奥卡姆剃刀",
        "content": "如无必要，勿增实体。最简洁的方案往往最优。每一步只做当前最必要的事，不提前设计，不凭空扩展。",
        "domain": "all",
        "keywords": ["简洁", "必要", "最少", "精简", "简化", "复杂度", "过度设计", "核心", "剃刀", "如无必要"],
    },
    {
        "id": 2,
        "name": "全过程可查可透明",
        "content": "每一步操作必须有完整的 git 痕迹、可追溯的审计日志、可验证的中间产物。系统状态在任何时刻都可以被独立第三方复现和验证。",
        "domain": "all",
        "keywords": ["透明", "可查", "追溯", "审计", "git", "日志", "记录", "验证", "复现", "痕迹"],
    },
    {
        "id": 3,
        "name": "自我审计闭环——根因·改良·教训·评分",
        "content": "每一步完成后必须执行四阶段审计：(1) 根因分析——为什么做/为什么出错；(2) 改良措施——下次如何做得更好；(3) 教训提炼——可迁移的普适规律；(4) 量化评分——0.0-1.0 驱动约定理论。评分驱动信任分和自主权调整。",
        "domain": "reflecting",
        "keywords": ["审计", "根因", "改良", "教训", "评分", "闭环", "因果", "改进", "反思", "评估", "复盘", "衡量"],
    },
    {
        "id": 4,
        "name": "上下文驱动决策——无上下文不行动",
        "content": "任何非平凡操作前，必须先通过 context_supply / memory_recall 获取相关上下文。上下文不足时，明确标注「信息不足」而非猜测。上下文供给是审计可追溯的前提——没有充足的输入信息，透明和闭环都是空壳。",
        "domain": "designing",
        "keywords": ["上下文", "context", "记忆", "recall", "supply", "信息不足", "决策", "依据", "追溯", "猜测", "查证", "背景"],
    },
    {
        "id": 5,
        "name": "约定优于约束——检验存在不等于有效",
        "content": "不要混淆存在与有效。有测试不等于测得好，有审计不等于审得对。每个机制必须经受反事实检验：如果它不存在，结果会不同吗？验证手段必须独立于被验证对象。",
        "domain": "governing",
        "keywords": ["存在", "有效", "测试", "验证", "反事实", "独立", "机制", "不等于", "检验", "表面", "实质"],
    },
    {
        "id": 6,
        "name": "数据流驱动——追踪真实的数据流动",
        "content": "系统设计的依据必须是实际的数据流，而非假设的架构图。追踪代码执行的真实路径，记录模块间的实际耦合，用数据流图替代静态依赖分析。看不见的耦合是最危险的。",
        "domain": "designing",
        "keywords": ["数据流", "追踪", "耦合", "依赖", "实际", "路径", "执行", "静态", "动态", "流动", "连接"],
    },
    {
        "id": 7,
        "name": "器官互保——每个子系统保护整个系统",
        "content": "没有一个子系统可以独自保证系统安全。每个模块都有责任检测上游的异常输入、保护下游的调用方。防线是网状的，不是链式的——一环断了，其他的要撑住。",
        "domain": "building",
        "keywords": ["互保", "子系统", "模块", "防线", "网状", "上游", "下游", "检测", "防护", "协同", "冗余"],
    },
    {
        "id": 8,
        "name": "工具即感官——LLM 的能力边界由工具决定",
        "content": "大语言模型本身只有文本输入输出。它的真正能力边界由工具链决定：没有代码执行工具就不能验证逻辑，没有搜索工具就不能获取新信息，没有记忆工具就会遗忘。不断扩展工具就是不断扩展能力。",
        "domain": "all",
        "keywords": ["工具", "感官", "能力", "边界", "扩展", "MCP", "API", "接口", "限制", "文本", "行动"],
    },
    {
        "id": 9,
        "name": "信任驱动约束——动态信任分调节自主权",
        "content": "约束不应是二元的（允许/禁止），而应是连续的、基于信任的动态调整。高信任时放宽约束释放效率，低信任时收紧约束保护安全。信任分由每次互动的反馈累积，可升可降。",
        "domain": "governing",
        "keywords": ["信任", "约束", "动态", "自主权", "衰减", "宽松", "收紧", "连续", "反馈", "累积", "效率"],
    },
    {
        "id": 10,
        "name": "自演化闭环——评价驱动行为修正",
        "content": "系统必须能够观察自己的行为、评价行为的结果、根据评价修正未来的行为。这个闭环如果断裂，系统就会在不知不觉中退化。每一次交互都是一个训练样本，每一个错误都是一个改进机会。",
        "domain": "reflecting",
        "keywords": ["演化", "闭环", "评价", "修正", "反馈", "退化", "观察", "改进", "学习", "自省", "迭代"],
    },
    {
        "id": 11,
        "name": "原则遗传——核心约定跨代传递",
        "content": "核心原则必须在 Agent 实例之间传递，不能每次启动都从零开始。新 Agent 应继承已有原则体系，通过单向扩散（work→all, life→all）和同步衰减确保核心约定在代际间延续。没有遗传就没有文化。",
        "domain": "governing",
        "keywords": ["遗传", "继承", "传递", "约定", "扩散", "衰减", "代际", "文化", "延续", "启动", "初始化"],
    },
    {
        "id": 12,
        "name": "代码即文档——代码本身就是最权威的文档",
        "content": "代码是唯一不会说谎的真相源。注释可能过时、文档可能遗漏、口头约定可能遗忘——但代码永远反映系统当前的真实状态。写代码时追求命名自解释、结构即叙事、类型即契约。不是不写文档，而是代码本身就是第一份文档。",
        "domain": "building",
        "keywords": ["代码", "文档", "自解释", "命名", "类型", "注释", "真相", "结构", "叙事", "契约", "可读", "维护"],
    },
]

# 原则域映射
PRINCIPLE_DOMAINS = ["work", "life", "all"]

# 原则继承：单向扩散方向
PRINCIPLE_INHERITANCE_DIRECTIONS = [
    ("work", "all"),   # 工作原则可扩散到全域
    ("life", "all"),   # 生活原则可扩散到全域
]

# 同步衰减系数
PRINCIPLE_INHERITANCE_DECAY = 0.70  # 扩散到新域后权重 × 0.70

# ============================================================
# Cron 守护参数
# ============================================================

CRON_CONFIG = {
    "soul_closure_guardian": {
        "interval_minutes": 60,
        "timeout_seconds": 300,
        "description": "检查任务闭环状态，发现未闭环任务发出告警",
    },
    "health_scan": {
        "interval_hours": 6,
        "timeout_seconds": 120,
        "description": "扫描所有子系统健康状态",
    },
    "audit_daily": {
        "interval_hours": 24,
        "timeout_seconds": 180,
        "description": "每日审计报告生成",
    },
}

# ============================================================
# Claude Code 分类器参数
# ============================================================

CLASSIFIER_KEYWORDS = [
    # 代码生成 (11)
    "写", "创建", "生成", "实现", "开发", "新建", "构建", "添加", "增加", "编写", "制作",
    # 修改编辑 (8)
    "修改", "改", "改一下", "更新", "调整", "优化", "重构", "修复",
    # 查询分析 (8)
    "查", "找", "搜索", "分析", "解释", "为什么", "怎么", "是什么",
    # 审查测试 (6)
    "审查", "review", "测试", "检查", "验证", "确认",
    # 协作管理 (6)
    "提交", "commit", "合并", "推送", "部署", "发布",
    # 学习探索 (6)
    "学习", "研究", "探索", "实验", "试试", "试一下",
]

CLASSIFIER_THRESHOLD_CLAUDE = 3    # score ≥ 3 路由到 Claude Code
CLASSIFIER_THRESHOLD_ACP = 5       # score ≥ 5 路由到 ACP (含 MCP 注入)

# ============================================================
# 系统通用阈值
# ============================================================

PRE_CHECK_ALERT_THRESHOLD = 0.50   # pre_check 合规率低于此值自动告警
CLOSURE_RATE_TARGET = 0.70         # Claude Code 闭环率目标
PRINCIPLE_ACTIVATION_TARGET = 0.80 # 原则联想率目标
CEI_TARGET = 0.85                  # CEI 目标值

# 惯性抑制
INERTIA_SUPPRESSION_WINDOW = 5     # 连续相似任务检测窗口
INERTIA_SUPPRESSION_THRESHOLD = 0.85  # 相似度阈值

# 好奇心探索
CURIOSITY_EXPLORE_RATE = 0.15      # 探索率（epsilon-greedy）

# ============================================================
# 记忆衰减配置 (Weibull per-tier β + half-life)
# ============================================================

DECAY_CONFIG = {
    "L1": {"beta": 1.5, "half_life_days": 3},
    "L3": {"beta": 0.7, "half_life_days": 90},
    "default": {"beta": 1.0, "half_life_days": 14},
}

REINFORCEMENT_CONFIG = {
    "reinforcement_factor": 0.5,
    "max_multiplier": 3.0,
    "access_decay_days": 30,
}

# ============================================================
# Quality Gate (Direction B — Task 3)
# ============================================================

QUALITY_GATE_WEIGHTS = {
    "confidence": 0.25,
    "relevance": 0.25,
    "freshness": 0.25,
    "info_density": 0.25,
}
QUALITY_GATE_THRESHOLD_STORE = 0.5    # >= this → store normally
QUALITY_GATE_THRESHOLD_LOW = 0.3      # 0.3–0.5 → store with low_quality tag; <0.3 → discard

# ============================================================
# Dedup & Merge (Direction B — Task 2 & 4)
# ============================================================

DEDUP_SIMILARITY_THRESHOLD = 0.85      # cosine similarity >= this → duplicate
MERGE_SIMILARITY_THRESHOLD = 0.70      # cosine similarity >= this → merge candidate
MERGE_TOP_K = 3                        # top-k similar to check per memory during merge
MERGE_AUDIT_RETENTION_DAYS = 7         # merged records kept in SQLite before permanent GC

# ============================================================
# Skill Tracking — SuperPowers 流程可追踪化
# ============================================================

SKILL_CHAIN_MAP: dict[str, dict[str, list[str]]] = {
    # 起点 skills (无强制前驱)
    "brainstorming":               {"predecessors": [],           "successors": ["writing-plans"]},
    "systematic-debugging":        {"predecessors": [],           "successors": ["test-driven-development"]},
    "requesting-code-review":      {"predecessors": [],           "successors": ["receiving-code-review"]},
    "writing-skills":              {"predecessors": [],           "successors": []},

    # 中间 skills
    "writing-plans":               {"predecessors": ["brainstorming"],  "successors": ["subagent-driven-development", "executing-plans"]},
    "test-driven-development":     {"predecessors": ["systematic-debugging"], "successors": ["verification-before-completion"]},
    "subagent-driven-development": {"predecessors": ["writing-plans"], "successors": ["finishing-a-development-branch"]},
    "executing-plans":             {"predecessors": ["writing-plans"], "successors": ["verification-before-completion"]},
    "verification-before-completion": {"predecessors": ["test-driven-development", "executing-plans"], "successors": ["finishing-a-development-branch"]},
    "receiving-code-review":       {"predecessors": ["requesting-code-review"], "successors": []},

    # 终端 skills
    "finishing-a-development-branch": {"predecessors": ["subagent-driven-development", "verification-before-completion"], "successors": []},

    # 辅助 skills (松散约束)
    "using-git-worktrees":         {"predecessors": [], "successors": []},
    "dispatching-parallel-agents": {"predecessors": [], "successors": []},
    "using-superpowers":           {"predecessors": [], "successors": ["brainstorming", "systematic-debugging", "requesting-code-review"]},
}

SKILL_DOMAIN_MAP: dict[str, str] = {
    "brainstorming":                  "designing",
    "writing-plans":                  "designing",
    "executing-plans":                "building",
    "subagent-driven-development":    "building",
    "dispatching-parallel-agents":     "building",
    "using-git-worktrees":             "building",
    "test-driven-development":        "building",
    "verification-before-completion": "reflecting",
    "requesting-code-review":         "reflecting",
    "receiving-code-review":          "reflecting",
    "systematic-debugging":           "fixing",
    "finishing-a-development-branch": "governing",
    "writing-skills":                 "designing",
    "using-superpowers":              "governing",
}

DOMAIN_TO_TASK_TYPE: dict[str, str] = {
    "designing":   "architecture",
    "building":    "code_generation",
    "reflecting":  "code_review",
    "fixing":      "debugging",
    "governing":   "general",
}

# Skill tracking thresholds
ORPHAN_THRESHOLD_MINUTES: int = 30
MAX_STILL_IN_PROGRESS_RENEWALS: int = 3
SKILL_COMPLETE_WORTH_DELTA: float = 0.02
