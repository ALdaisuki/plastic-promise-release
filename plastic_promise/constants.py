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
    "principle_activation": {
        "name": "原则联想",
        "weight": 0.20,
        "description": "核心原则在决策中是否自然浮现",
    },
    "memory_supply": {
        "name": "记忆供应",
        "weight": 0.15,
        "description": "上下文供应引擎是否提供高质量记忆",
    },
    "constraint_compliance": {
        "name": "约束合规",
        "weight": 0.15,
        "description": "三层防线的约束是否被正确遵守",
    },
    "feedback_closure": {
        "name": "反馈闭环",
        "weight": 0.15,
        "description": "行为→评价→信任的闭环是否完整",
    },
    "trust_alignment": {
        "name": "信任校准",
        "weight": 0.10,
        "description": "信任分是否准确反映行为质量",
    },
    "principle_inheritance": {
        "name": "原则继承",
        "weight": 0.10,
        "description": "核心约定是否跨代传递",
    },
    "safety_trace": {
        "name": "安全追溯",
        "weight": 0.15,
        "description": "关键决策是否有完整追溯链",
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
        "name": "诚实优先于完美",
        "content": "如果某个指标下降了，不要遮掩。数字身体的成长不是线性的，有起伏才是真实的。",
        "domain": "all",
        "keywords": ["诚实", "透明", "指标", "下降", "真实", "报告"],
    },
    {
        "id": 2,
        "name": "约定优于约束",
        "content": "Agent 遵守规则不是因为「被禁止」，而是因为「不想让在乎的人失望」。用内部动机替代外部强制。",
        "domain": "work",
        "keywords": ["约定", "动机", "规则", "禁止", "信任", "自觉"],
    },
    {
        "id": 3,
        "name": "记忆主动供应而非被动查询",
        "content": "记忆系统不是「被查询的档案库」，而是「主动供应上下文的引擎」。查找记忆的过程同时也是提示词注入的过程。",
        "domain": "all",
        "keywords": ["记忆", "上下文", "供应", "查询", "档案", "引擎"],
    },
    {
        "id": 4,
        "name": "原则随记忆自然浮现",
        "content": "原则不是靠防火墙强制执行的，而是在 Agent 检索历史决策时自然浮现的。联想不是「检索」，是「涌现」。",
        "domain": "work",
        "keywords": ["原则", "浮现", "检索", "联想", "涌现", "自然"],
    },
    {
        "id": 5,
        "name": "存在性不等于有效性",
        "content": "检查了「机制是否存在」不等于检查了「机制是否真的改变了行为」。要验证实际效果而非仅确认存在。",
        "domain": "work",
        "keywords": ["验证", "效果", "存在", "检查", "机制", "行为改变"],
    },
    {
        "id": 6,
        "name": "连通性不等于协同性",
        "content": "画了系统间的连通矩阵，但没有追踪数据是否真的在这些链路中流转。要追踪实际数据流。",
        "domain": "work",
        "keywords": ["连通", "协同", "数据流", "链路", "矩阵", "追踪"],
    },
    {
        "id": 7,
        "name": "器官互相守护",
        "content": "不增加新器官，让已有器官学会互相守护。每个系统的健康检查可以委托给相邻系统。",
        "domain": "all",
        "keywords": ["守护", "协作", "器官", "委托", "冗余", "互相"],
    },
    {
        "id": 8,
        "name": "工具是 LLM 的唯一感官",
        "content": "LLM 本质上是一个聋哑人，但不是一个智力残疾的聋哑人。工具是它唯一的感官和双手。",
        "domain": "all",
        "keywords": ["工具", "感官", "MCP", "限制", "能力", "扩展"],
    },
    {
        "id": 9,
        "name": "信任换自主——动态约束",
        "content": "信任分驱动的 L1↔L0 切换：高分放宽约束，低分收紧约束。信任是挣来的，不是默认给予的。",
        "domain": "work",
        "keywords": ["信任", "自主", "约束", "动态", "切换", "挣取"],
    },
    {
        "id": 10,
        "name": "自演化闭环不可断裂",
        "content": "行为→评价→信任变化→自主权调整 这四个环节缺一不可。任何一环断裂都会导致系统退化。",
        "domain": "all",
        "keywords": ["闭环", "演化", "评价", "反馈", "退化", "连续性"],
    },
    {
        "id": 11,
        "name": "原则继承——单向扩散同步衰减",
        "content": "work→all、life→all 单向扩散，核心约定跨 Agent 代际传递，但权重随传播距离同步衰减。",
        "domain": "all",
        "keywords": ["继承", "扩散", "衰减", "传递", "代际", "同步"],
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
