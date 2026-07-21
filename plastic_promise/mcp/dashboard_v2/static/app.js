(function () {
  "use strict";

  var API_ROOT = "/api/dashboard/v2";
  var PAGE_LIMIT = 25;
  var MAX_JSON_CHARS = 50000;
  var SENSITIVE_KEY = /(api[_-]?key|authorization|cookie|credential|password|private[_-]?key|secret|token)/i;
  var STATUS_LABELS = {
    ok: "正常",
    ready: "就绪",
    success: "成功",
    succeeded: "成功",
    verified: "已验证",
    done: "已完成",
    complete: "已完成",
    completed: "已完成",
    active: "活跃",
    healthy: "健康",
    autonomous: "自主",
    accepted: "已接受",
    error: "错误",
    failed: "失败",
    failure: "失败",
    denied: "已拒绝",
    blocked: "已阻止",
    contested: "有争议",
    stale: "已失效",
    forgotten: "已遗忘",
    corrected: "已纠正",
    fatal: "严重错误",
    unhealthy: "异常",
    rejected: "已驳回",
    permanent_failure: "永久失败",
    warning: "警告",
    warn: "警告",
    degraded: "降级",
    pending: "待处理",
    processing: "处理中",
    retry: "重试",
    retrying: "重试中",
    draft: "草稿",
    standard: "标准",
    medium: "中等",
    recovering: "恢复中",
    claimed: "已认领",
    executing: "执行中",
    info: "信息",
    local: "本地",
    global: "全局",
    readonly: "只读",
    read_only: "只读",
    low: "低",
    high: "高",
    critical: "严重",
    cached: "已缓存",
    unknown: "未知",
    experience: "经验",
    memory: "记忆",
    task: "任务",
    principle: "原则",
    code: "代码",
    reflection: "反思",
    public: "公开",
    private: "私有",
    project: "项目",
    bounded: "受限",
    redacted: "已脱敏",
    object: "对象",
    array: "数组",
    string: "字符串",
    number: "数字",
    boolean: "布尔值",
    synthesis: "综合记忆",
    governed: "受治理",
    shadow: "影子模式",
    unavailable: "不可用",
    disabled: "已禁用",
    "not planned": "未计划",
    "not executed": "未执行",
    "evidence only": "仅作证据",
    participating: "参与中",
    executed: "已执行",
    available: "可用",
    enabled: "已启用",
    planned: "已计划",
    on: "开启",
    off: "关闭",
    effective: "有效",
    inactive: "未生效",
    open: "待处理",
    closed: "已关闭",
    included: "已纳入",
    excluded: "已排除",
    allowed: "已允许",
    dropped: "已丢弃",
    kept: "已保留",
    related: "关联",
    derived_from: "派生自",
    synthesized_from: "综合自",
    supersedes: "替代",
    superseded_by: "被替代",
    corrects: "纠正",
    corrected_by: "被纠正",
    supports: "支持",
    supported_by: "被支持",
    contradicts: "相互冲突",
    references: "引用",
    promoted_from_proposal: "由提案晋升",
    synthesis_invalidated: "综合记忆已失效",
    ordinary_source_corrected: "来源已纠正",
    ordinary_source_forgotten: "来源已遗忘",
    system_global: "系统全局",
    not_captured: "未采集",
    invalid: "时间无效",
    measured: "已测量",
    legacy_global: "全局兼容",
    project_evidence: "项目证据",
    paragraph: "段落",
    list: "列表",
    table: "表格",
    heading: "标题",
    core: "核心层",
    divergent: "发散层",
    hybrid: "混合检索",
    vector: "向量检索",
    bm25: "BM25 检索",
    fts: "全文检索",
    graph: "图谱检索",
    rrf: "RRF 融合",
    matched: "快照已记录切片 ID",
    available_not_recorded: "有结构切片清单，未记录精确命中",
    manifest_available_not_lineage_specific: "有端点切片清单，关系未指定切片",
    not_recorded: "未记录"
  };
  var COMPONENT_LABELS = {
    "Canonical SQLite": "规范 SQLite",
    "Project authority": "项目权限",
    "Retrieval explain": "检索解释",
    Trust: "信任",
    Issues: "问题",
    "read-only projection": "只读投影",
    "bounded stored snapshots": "受限存储快照",
    "no project ownership model": "尚无项目归属模型",
    unavailable: "不可用"
  };
  var FIELD_LABELS = {
    id: "ID",
    memory_id: "记忆 ID",
    parent_memory_id: "父记忆 ID",
    source_memory_id: "来源记忆 ID",
    target_memory_id: "目标记忆 ID",
    call_id: "调用 ID",
    created_by_call_id: "创建调用 ID",
    request_scope_id: "请求范围 ID",
    project_id: "项目 ID",
    content_preview: "内容摘要",
    memory_type: "记忆类型",
    source_class: "来源类别",
    lifecycle_state: "生命周期状态",
    created_at: "创建时间",
    updated_at: "更新时间",
    started_at: "开始时间",
    ended_at: "结束时间",
    duration_ms: "耗时（毫秒）",
    duration_status: "耗时采集状态",
    worth_success: "成功价值计数",
    worth_failure: "失败价值计数",
    access_count: "访问次数",
    activation_weight: "激活权重",
    last_accessed: "最近访问",
    source: "来源",
    owner: "所有者",
    tier: "层级",
    scope: "范围",
    category: "类别",
    domain: "领域",
    importance: "重要度",
    visibility: "可见性",
    origin_kind: "来源类型",
    origin_uri: "来源地址",
    origin_ref: "来源引用",
    l0_abstract: "L0 摘要",
    l1_summary: "L1 摘要",
    relation: "关系",
    roles: "角色",
    direction: "方向",
    directed: "有向关系",
    evidence_scope: "证据范围",
    chunking: "结构切片",
    chunk_anchors: "切片清单锚点",
    chunk_anchor_summary: "切片清单摘要",
    metadata: "元数据",
    evidence: "证据",
    call: "调用证据",
    channel_scores: "通道分数",
    chunk_evidence: "切片清单证据",
    gate_decision: "门控决策",
    gate_reason: "门控原因",
    filter_decision: "过滤决策",
    filter_reason: "过滤原因",
    retrieval_source: "检索来源",
    final_score: "最终分数",
    initial_score: "初始分数",
    rank: "排名",
    status: "状态",
    degraded: "是否降级",
    tags: "标签"
  };
  var ROLE_LABELS = {
    anchor: "当前锚点",
    parent: "父记忆",
    child: "子记忆",
    related: "关联记忆",
    source: "来源",
    target: "目标"
  };
  var CHANNEL_LABELS = {
    vector: "向量检索",
    vector_search: "向量检索",
    bm25: "BM25 检索",
    bm25_search: "BM25 检索",
    fts: "全文检索",
    fts_search: "全文检索",
    graph: "图谱检索",
    graph_search: "图谱检索",
    symbolic: "符号检索",
    lexical: "词法检索"
  };

  var VIEWS = {
    overview: {
      title: "概览",
      description: "集中查看系统就绪状态、规范记忆、信任、问题与运行健康度。",
      endpoint: "/overview"
    },
    requests: {
      title: "请求",
      description: "查看当前项目范围内的调用链、耗时、状态、调用方与降级证据。",
      endpoint: "/requests",
      paginated: true
    },
    memories: {
      title: "记忆",
      description: "查看当前本地项目范围内可见的规范记忆。",
      endpoint: "/memories",
      paginated: true
    },
    lineage: {
      title: "记忆谱系",
      description: "追踪单条记忆的来源、替代、纠正与综合关系。",
      endpoint: "/memories",
      paginated: true
    },
    synthesis: {
      title: "综合记忆",
      description: "查看受治理综合记忆的生命周期、证据支持与验证状态。",
      endpoint: "/synthesis",
      paginated: true
    },
    operations: {
      title: "运行运维",
      description: "查看运行事件、可恢复故障、回退、重试与持久化发件箱状态。",
      endpoint: "/operations",
      paginated: true
    },
    "trust-issues": {
      title: "信任与问题",
      description: "查看当前权限、信任历史与需要干预的问题。",
      endpoint: "/trust-issues",
      paginated: true
    },
    explain: {
      title: "检索解释",
      description: "查看受限保存的检索证据，不暴露查询文本、提示词、记忆正文或向量。",
      endpoint: "/requests",
      paginated: true
    },
    configuration: {
      title: "有效配置",
      description: "只读查看运行模式与功能开关，敏感字段会递归脱敏。",
      endpoint: "/configuration"
    }
  };

  var state = {
    currentView: "overview",
    routeParams: {},
    payloads: Object.create(null),
    pagination: Object.create(null),
    filters: Object.create(null),
    filterTimers: Object.create(null),
    operationTab: "runtime",
    trustTab: "trust",
    requestSequence: 0,
    controller: null,
    lineageSequence: 0,
    explainSequence: 0,
    detailSequence: 0,
    detailFocus: null,
    featureFlags: {
      retrievalExplain: null
    }
  };

  var refs = {
    shell: document.getElementById("app-shell"),
    sidebar: document.getElementById("sidebar"),
    sidebarToggle: document.getElementById("sidebar-toggle"),
    mobileToggle: document.getElementById("mobile-toggle"),
    mobileBackdrop: document.getElementById("mobile-backdrop"),
    mainNav: document.getElementById("main-nav"),
    topbarTitle: document.getElementById("topbar-title"),
    refreshButton: document.getElementById("refresh-button"),
    scopeChip: document.getElementById("scope-chip"),
    readinessBadge: document.getElementById("readiness-badge"),
    connectionDot: document.getElementById("connection-dot"),
    connectionLabel: document.getElementById("connection-label"),
    noticeStack: document.getElementById("notice-stack"),
    viewRoot: document.getElementById("view-root"),
    lastUpdated: document.getElementById("last-updated"),
    detailDialog: document.getElementById("detail-dialog"),
    detailEyebrow: document.getElementById("detail-eyebrow"),
    detailTitle: document.getElementById("detail-title"),
    detailBody: document.getElementById("detail-body"),
    detailClose: document.getElementById("detail-close"),
    toastRegion: document.getElementById("toast-region")
  };

  function isObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function append(parent, child) {
    if (child === null || child === undefined || child === false) {
      return;
    }
    if (Array.isArray(child)) {
      child.forEach(function (item) {
        append(parent, item);
      });
      return;
    }
    if (child instanceof Node) {
      parent.appendChild(child);
      return;
    }
    parent.appendChild(document.createTextNode(String(child)));
  }

  function h(tag, options, children) {
    var element = document.createElement(tag);
    var settings = options || {};
    if (settings.className) {
      element.className = settings.className;
    }
    if (settings.text !== undefined) {
      element.textContent = String(settings.text);
    }
    if (settings.attrs) {
      Object.keys(settings.attrs).forEach(function (key) {
        var value = settings.attrs[key];
        if (value === false || value === null || value === undefined) {
          return;
        }
        if (value === true) {
          element.setAttribute(key, "");
        } else {
          element.setAttribute(key, String(value));
        }
      });
    }
    if (settings.dataset) {
      Object.keys(settings.dataset).forEach(function (key) {
        element.dataset[key] = String(settings.dataset[key]);
      });
    }
    if (settings.on) {
      Object.keys(settings.on).forEach(function (eventName) {
        element.addEventListener(eventName, settings.on[eventName]);
      });
    }
    append(element, children || []);
    return element;
  }

  function makeIcon(name, extraClass) {
    var namespace = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(namespace, "svg");
    var use = document.createElementNS(namespace, "use");
    svg.setAttribute("class", extraClass ? "icon " + extraClass : "icon");
    svg.setAttribute("aria-hidden", "true");
    use.setAttribute("href", "#icon-" + name);
    svg.appendChild(use);
    return svg;
  }

  function iconButton(name, label, onClick) {
    return h("button", {
      className: "icon-button",
      attrs: { type: "button", title: label, "aria-label": label },
      on: { click: onClick }
    }, makeIcon(name));
  }

  function textButton(label, onClick, options) {
    var settings = options || {};
    var button = h("button", {
      className: "text-button" + (settings.primary ? " primary" : ""),
      attrs: {
        type: settings.type || "button",
        disabled: Boolean(settings.disabled)
      },
      on: onClick ? { click: onClick } : {}
    }, []);
    if (settings.icon) {
      button.appendChild(makeIcon(settings.icon));
    }
    button.appendChild(document.createTextNode(label));
    return button;
  }

  function readPath(object, path) {
    var cursor = object;
    var parts = path.split(".");
    for (var index = 0; index < parts.length; index += 1) {
      if (!isObject(cursor) && !Array.isArray(cursor)) {
        return undefined;
      }
      cursor = cursor[parts[index]];
      if (cursor === undefined || cursor === null) {
        return cursor;
      }
    }
    return cursor;
  }

  function firstValue(object, paths, fallback) {
    for (var index = 0; index < paths.length; index += 1) {
      var value = readPath(object, paths[index]);
      if (value !== undefined && value !== null && value !== "") {
        return value;
      }
    }
    return fallback;
  }

  function asArray(value) {
    return Array.isArray(value) ? value : [];
  }

  function envelopeObject(payload) {
    if (isObject(payload) && isObject(payload.data)) {
      return payload.data;
    }
    if (isObject(payload)) {
      return payload;
    }
    return {};
  }

  function envelopeRows(payload, keys) {
    if (isObject(payload) && Array.isArray(payload.data)) {
      return payload.data;
    }
    var data = envelopeObject(payload);
    var candidates = keys || ["items", "results", "rows"];
    for (var index = 0; index < candidates.length; index += 1) {
      if (Array.isArray(data[candidates[index]])) {
        return data[candidates[index]];
      }
    }
    return [];
  }

  function toSearchText(value) {
    if (value === null || value === undefined) {
      return "";
    }
    if (typeof value === "string") {
      return value.toLowerCase();
    }
    try {
      return JSON.stringify(value).slice(0, 10000).toLowerCase();
    } catch (error) {
      return String(value).toLowerCase();
    }
  }

  function formatNumber(value) {
    var number = Number(value);
    if (!Number.isFinite(number)) {
      return value === null || value === undefined || value === "" ? "--" : String(value);
    }
    return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(number);
  }

  function formatTimestamp(value) {
    if (!value) {
      return "--";
    }
    var date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return String(value);
    }
    return new Intl.DateTimeFormat("zh-CN", {
      dateStyle: "medium",
      timeStyle: "medium"
    }).format(date);
  }

  function formatDuration(value, row) {
    if (row && row.duration_status === "not_captured") {
      return "未采集";
    }
    if (row && row.duration_status === "invalid") {
      return "时间无效";
    }
    var duration = value === null || value === undefined || value === "" ? NaN : Number(value);
    if (!Number.isFinite(duration) && row) {
      var start = new Date(firstValue(row, ["started_at", "created_at"], "")).getTime();
      var end = new Date(firstValue(row, ["ended_at", "updated_at"], "")).getTime();
      if (Number.isFinite(start) && Number.isFinite(end) && end > start) {
        duration = end - start;
      }
    }
    if (!Number.isFinite(duration)) {
      return "未采集";
    }
    if (duration < 0) {
      return "时间无效";
    }
    if (duration < 1) {
      return "<1 ms";
    }
    if (duration < 1000) {
      return Math.round(duration) + " ms";
    }
    return (duration / 1000).toFixed(duration < 10000 ? 2 : 1) + " s";
  }

  function durationKind(row) {
    var status = String(firstValue(row || {}, ["duration_status"], "")).toLowerCase();
    if (status === "measured") {
      return "measured";
    }
    if (status === "invalid") {
      return "invalid";
    }
    if (status === "not_captured") {
      return "missing";
    }
    var direct = Number(firstValue(row || {}, ["duration_ms", "latency_ms", "elapsed_ms"], NaN));
    if (Number.isFinite(direct)) {
      return direct >= 0 ? "measured" : "invalid";
    }
    if (row) {
      var start = new Date(firstValue(row, ["started_at", "created_at"], "")).getTime();
      var end = new Date(firstValue(row, ["ended_at", "updated_at"], "")).getTime();
      if (Number.isFinite(start) && Number.isFinite(end) && end !== start) {
        return end > start ? "measured" : "invalid";
      }
    }
    return "missing";
  }

  function explainReasonLabel(value) {
    var labels = {
      unavailable: "当前运行模式未提供此通道",
      disabled: "该通道未启用",
      evidence_only: "仅提供结构证据，不参与排序",
      not_planned: "本次调用未规划此通道",
      not_executed: "已规划，但本次没有执行",
      snapshot_not_captured: "旧调用未保存解释快照"
    };
    var normalized = String(value || "").trim().toLowerCase();
    return labels[normalized] || statusLabel(value);
  }

  function pipelineLabel(value) {
    var labels = {
      retrieval_mode: "检索模式",
      fusion_policy: "融合策略",
      fusion_runtime: "融合运行时",
      fusion_algorithm: "融合算法",
      engine_mode: "引擎模式",
      vector_count: "向量候选数",
      bm25_count: "BM25 候选数",
      fts_count: "全文候选数",
      graph_count: "图谱候选数",
      fused_count: "融合候选数",
      candidate_count: "候选总数",
      result_count: "结果数",
      reranked_count: "重排后数量",
      after_noise_filter: "噪声过滤后",
      after_source_filter: "来源过滤后",
      after_hard_score_filter: "硬分数过滤后",
      after_mmr: "MMR 后",
      core_count: "核心层数量",
      related_count: "关联层数量",
      divergent_count: "发散层数量",
      canonical_hot_count: "热记忆数量",
      context_gate_evaluated: "上下文门控评估数",
      minimum_score: "最低分",
      maximum_score: "最高分",
      mean_score: "平均分",
      degraded: "是否降级",
      vector_search: "向量检索",
      bm25_search: "BM25 检索",
      fts_search: "全文检索",
      graph_search: "图谱检索",
      fusion: "候选融合",
      rerank: "候选重排",
      noise_filter: "噪声过滤",
      source_filter: "来源过滤",
      hard_score_filter: "硬分数过滤",
      mmr: "MMR 去重",
      context_gate: "上下文门控",
      principle_injection: "原则注入",
      snapshot_parse: "快照解析",
      candidate_retrieval: "候选检索",
      filter_and_layer: "过滤与分层",
      fallback_filter_and_layer: "回退过滤与分层",
      total: "检索总耗时",
      total_retrieval: "检索总耗时"
    };
    var normalized = String(value || "").trim().toLowerCase();
    return labels[normalized] || String(value || "阶段").replace(/_/g, " ");
  }

  function formatScalar(value) {
    if (value === null || value === undefined || value === "") {
      return "--";
    }
    if (typeof value === "boolean") {
      return value ? "是" : "否";
    }
    if (typeof value === "number") {
      return formatNumber(value);
    }
    if (Array.isArray(value)) {
      return value.map(function (item) {
        return isObject(item) ? safeJson(item) : String(item);
      }).join(", ");
    }
    if (isObject(value)) {
      return safeJson(value);
    }
    return String(value);
  }

  function redactForDisplay(value, key) {
    if (key && SENSITIVE_KEY.test(key)) {
      return "[已脱敏]";
    }
    if (Array.isArray(value)) {
      return value.map(function (item) {
        return redactForDisplay(item, "");
      });
    }
    if (isObject(value)) {
      var output = {};
      Object.keys(value).forEach(function (childKey) {
        output[childKey] = redactForDisplay(value[childKey], childKey);
      });
      return output;
    }
    return value;
  }

  function safeJson(value) {
    var serialized;
    try {
      serialized = JSON.stringify(redactForDisplay(value, ""), null, 2);
    } catch (error) {
      serialized = String(value);
    }
    if (serialized.length > MAX_JSON_CHARS) {
      return serialized.slice(0, MAX_JSON_CHARS) + "\n... 显示内容已截断";
    }
    return serialized;
  }

  function compactId(value) {
    var text = formatScalar(value);
    if (text.length <= 20) {
      return text;
    }
    return text.slice(0, 9) + "..." + text.slice(-7);
  }

  function statusKind(value) {
    var normalized = String(value || "").trim().toLowerCase();
    if (/^(ok|ready|success|succeeded|verified|done|complete|completed|active|healthy|autonomous|accepted)$/.test(normalized)) {
      return "success";
    }
    if (/^(error|failed|failure|denied|blocked|contested|stale|fatal|unhealthy|rejected|permanent_failure)$/.test(normalized)) {
      return "danger";
    }
    if (/^(warning|warn|degraded|pending|processing|retry|retrying|draft|standard|medium|recovering|claimed|executing)$/.test(normalized)) {
      return "warning";
    }
    if (/^(info|local|global|readonly|read_only|low|cached)$/.test(normalized)) {
      return "info";
    }
    if (/^(synthesis|governed|shadow)$/.test(normalized)) {
      return "brand";
    }
    return "neutral";
  }

  function statusLabel(value) {
    var text = formatScalar(value);
    var normalized = String(value || "").trim().toLowerCase();
    return STATUS_LABELS[normalized] || text;
  }

  function fieldLabel(value) {
    var normalized = String(value || "").trim().toLowerCase();
    return FIELD_LABELS[normalized] || String(value || "字段").replace(/_/g, " ");
  }

  function roleLabel(value) {
    var normalized = String(value || "").trim().toLowerCase();
    return ROLE_LABELS[normalized] || statusLabel(value);
  }

  function channelLabel(value) {
    var normalized = String(value || "").trim().toLowerCase();
    return CHANNEL_LABELS[normalized] || pipelineLabel(value);
  }

  function componentLabel(value) {
    if (value === null || value === undefined || value === "") {
      return "";
    }
    var text = formatScalar(value);
    return COMPONENT_LABELS[text] || text;
  }

  function makeBadge(value, forcedKind) {
    var rawText = formatScalar(value);
    var text = statusLabel(value);
    var kind = forcedKind || statusKind(rawText);
    return h("span", {
      className: "badge badge-" + kind,
      text: text,
      attrs: { title: text === rawText ? text : text + "（" + rawText + "）" }
    });
  }

  function parseBoolean(value) {
    return value === true || value === 1 || String(value).toLowerCase() === "true";
  }

  function uniqueValues(rows, getter) {
    var values = [];
    var seen = new Set();
    rows.forEach(function (row) {
      var value = formatScalar(getter(row));
      if (value !== "--" && !seen.has(value)) {
        seen.add(value);
        values.push(value);
      }
    });
    return values.sort(function (left, right) {
      return left.localeCompare(right);
    });
  }

  function viewHeader(title, description, actions) {
    var heading = h("div", { className: "view-heading" }, [
      h("h1", { text: title }),
      h("p", { text: description })
    ]);
    return h("header", { className: "view-header" }, [
      heading,
      actions && actions.length ? h("div", { className: "view-header-actions" }, actions) : null
    ]);
  }

  function sectionHeader(title, meta) {
    return h("div", { className: "section-header" }, [
      h("h2", { text: title }),
      meta ? h("span", { className: "section-meta", text: meta }) : null
    ]);
  }

  function statePanel(kind, title, description, retry) {
    var iconName = kind === "error" ? "alert-triangle" : kind === "empty" ? "database" : "info";
    var content = h("div", { className: "state-content" }, [
      h("div", { className: "state-icon" }, makeIcon(iconName)),
      h("h2", { text: title }),
      h("p", { text: description })
    ]);
    if (retry) {
      content.appendChild(textButton("重试", retry, { icon: "refresh" }));
    }
    return h("div", { className: "state-panel" }, content);
  }

  function loadingState() {
    var lines = [];
    for (var index = 0; index < 7; index += 1) {
      lines.push(h("div", { className: "skeleton-line" }));
    }
    return h("div", { className: "skeleton-stack", attrs: { "aria-label": "正在加载控制台数据" } }, [
      h("div", { className: "skeleton-header" }),
      h("div", { className: "skeleton-cards" }, [
        h("div", { className: "skeleton-card" }),
        h("div", { className: "skeleton-card" }),
        h("div", { className: "skeleton-card" }),
        h("div", { className: "skeleton-card" })
      ]),
      h("div", { className: "skeleton-table" }, lines)
    ]);
  }

  function showLoading() {
    refs.viewRoot.setAttribute("aria-busy", "true");
    refs.viewRoot.replaceChildren(loadingState());
    refs.refreshButton.classList.add("is-loading");
    refs.refreshButton.disabled = true;
  }

  function showError(error) {
    var definition = VIEWS[state.currentView];
    var message = error && error.message ? error.message : "无法加载控制台接口。";
    refs.viewRoot.replaceChildren(
      viewHeader(definition.title, definition.description),
      statePanel("error", "无法加载此视图", message, function () {
        loadCurrentView();
      })
    );
    setConnection("error", "API 不可用");
    setReadiness("不可用", "danger");
  }

  function makeNotice(kind, title, message) {
    return h("div", { className: "notice notice-" + kind }, [
      makeIcon(kind === "danger" || kind === "warning" ? "alert-triangle" : "info"),
      h("div", { className: "notice-copy" }, [
        title ? h("strong", { text: title + " " }) : null,
        h("span", { text: message })
      ])
    ]);
  }

  function renderNotices(payload) {
    var notices = [];
    if (payload && payload.degraded) {
      notices.push(makeNotice(
        "warning",
        "结果已降级。",
        "至少一个可恢复通道失败或使用了回退路径，请前往“运行运维”查看证据。"
      ));
    }
    asArray(payload && payload.warnings).slice(0, 6).forEach(function (warning) {
      var message = isObject(warning)
        ? firstValue(warning, ["message", "reason", "code"], safeJson(warning))
        : String(warning);
      notices.push(makeNotice("warning", "", message));
    });
    refs.noticeStack.replaceChildren.apply(refs.noticeStack, notices);
  }

  function setConnection(kind, label) {
    refs.connectionDot.className = "connection-dot" + (kind ? " is-" + kind : "");
    refs.connectionLabel.textContent = label;
  }

  function setReadiness(label, kind) {
    refs.readinessBadge.textContent = label;
    refs.readinessBadge.className = "status-badge status-" + (kind || statusKind(label));
    refs.readinessBadge.title = label;
  }

  function updateEnvelopeChrome(payload) {
    var scope = isObject(payload && payload.scope) ? payload.scope : {};
    var project = firstValue(scope, ["project_id", "project"], "本地范围");
    var auth = firstValue(scope, ["auth_mode"], "");
    var scopeLabel = auth ? project + " / " + statusLabel(auth) : project;
    refs.scopeChip.textContent = scopeLabel;
    refs.scopeChip.title = "当前生效范围：" + scopeLabel;
    if (payload && payload.degraded) {
      setConnection("degraded", "已降级");
      setReadiness("降级", "warning");
    } else {
      setConnection("ready", "已连接");
      setReadiness("就绪", "success");
    }
    refs.lastUpdated.textContent = "更新于 " + formatTimestamp(new Date().toISOString());
  }

  function toast(message, isError) {
    var item = h("div", {
      className: "toast" + (isError ? " is-error" : ""),
      text: message
    });
    refs.toastRegion.appendChild(item);
    window.setTimeout(function () {
      item.remove();
    }, 4200);
  }

  function ApiError(message, status, payload) {
    this.name = "ApiError";
    this.message = message;
    this.status = status;
    this.payload = payload;
  }
  ApiError.prototype = Object.create(Error.prototype);

  function apiRequest(path, params, signal) {
    var url = new URL(API_ROOT + path, window.location.origin);
    Object.keys(params || {}).forEach(function (key) {
      var value = params[key];
      if (value !== null && value !== undefined && value !== "") {
        url.searchParams.set(key, String(value));
      }
    });
    return fetch(url.toString(), {
      method: "GET",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      signal: signal
    }).then(function (response) {
      return response.text().then(function (body) {
        var payload = {};
        if (body) {
          try {
            payload = JSON.parse(body);
          } catch (error) {
            payload = { error: body.slice(0, 500) };
          }
        }
        if (!response.ok) {
          var detail = firstValue(payload, [
            "error.message", "detail.message", "message", "detail", "error.code"
          ], response.statusText || "请求失败");
          if (isObject(detail) || Array.isArray(detail)) {
            detail = safeJson(detail);
          }
          throw new ApiError(response.status + " " + detail, response.status, payload);
        }
        return payload;
      });
    });
  }

  function pageState(view) {
    if (!state.pagination[view]) {
      state.pagination[view] = { cursor: null, history: [] };
    }
    return state.pagination[view];
  }

  function pageParams(view) {
    var definition = VIEWS[view];
    if (!definition.paginated) {
      return {};
    }
    var params = {
      limit: PAGE_LIMIT,
      cursor: pageState(view).cursor
    };
    if (view === "memories") {
      params.query = String(firstValue(state.filters.memories || {}, ["query"], "")).trim();
    }
    return params;
  }

  function resetPage(view) {
    state.pagination[view] = { cursor: null, history: [] };
  }

  function paginationNode(payload, view, visibleCount) {
    var page = isObject(payload && payload.page) ? payload.page : {};
    var current = pageState(view);
    var total = Number(page.total);
    var hasTotal = Number.isFinite(total);
    var pageNumber = current.history.length + 1;
    var copy = "第 " + pageNumber + " 页 / 当前显示 " + visibleCount + " 条";
    if (hasTotal) {
      copy += " / 共 " + formatNumber(total) + " 条";
    }
    var previous = iconButton("chevron-left", "上一页", function () {
      if (!current.history.length) {
        return;
      }
      current.cursor = current.history.pop() || null;
      loadCurrentView();
    });
    previous.disabled = current.history.length === 0;
    var next = iconButton("chevron-right", "下一页", function () {
      if (!page.next_cursor) {
        return;
      }
      current.history.push(current.cursor);
      current.cursor = page.next_cursor;
      loadCurrentView();
    });
    next.disabled = !page.next_cursor && !page.has_more;
    return h("div", { className: "pagination" }, [
      h("span", { className: "pagination-copy", text: copy }),
      h("div", { className: "pagination-actions" }, [previous, next])
    ]);
  }

  function payloadTotal(payload) {
    var total = Number(payload && payload.page && payload.page.total);
    return Number.isFinite(total) ? total : null;
  }

  function paginatedEmptyState(payload, view, title, message) {
    var panel = statePanel("empty", title, message);
    panel.classList.add("is-paginated");
    return h("div", { className: "table-frame" }, [
      panel,
      paginationNode(payload, view, 0)
    ]);
  }

  function tableNode(columns, rows, options) {
    var settings = options || {};
    var table = h("table", { className: "data-table" });
    var colgroup = h("colgroup");
    columns.forEach(function (column) {
      var col = h("col");
      if (column.width && /^\d+%$/.test(column.width)) {
        col.className = "col-w-" + column.width.slice(0, -1);
      }
      colgroup.appendChild(col);
    });
    table.appendChild(colgroup);

    var headerRow = h("tr");
    columns.forEach(function (column) {
      headerRow.appendChild(h("th", {
        text: column.label,
        attrs: { scope: "col" }
      }));
    });
    table.appendChild(h("thead", {}, headerRow));

    var tbody = h("tbody");
    rows.forEach(function (row, rowIndex) {
      var tr = h("tr", {
        className: settings.onRow ? "is-interactive" : "",
        attrs: settings.onRow ? {
          tabindex: "0",
          role: "button",
          "aria-label": settings.rowLabel ? settings.rowLabel(row) : "打开记录详情"
        } : {}
      });
      columns.forEach(function (column) {
        var value = column.value ? column.value(row, rowIndex) : row[column.key];
        var rendered = column.render ? column.render(value, row, rowIndex) : formatScalar(value);
        var td = h("td", { className: column.className || "" });
        append(td, rendered);
        if (!(rendered instanceof Node) && value !== null && value !== undefined) {
          td.title = String(value);
        }
        tr.appendChild(td);
      });
      if (settings.onRow) {
        tr.addEventListener("click", function (event) {
          if (event.target.closest("button, a, input, select, textarea, summary, [contenteditable='true']")) {
            return;
          }
          settings.onRow(row, rowIndex, tr);
        });
        tr.addEventListener("keydown", function (event) {
          if (event.target !== tr) {
            return;
          }
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            settings.onRow(row, rowIndex, tr);
          }
        });
      }
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);

    var frame = h("div", { className: "table-frame" }, [
      h("div", { className: "table-scroll" }, table)
    ]);
    if (settings.caption) {
      frame.appendChild(h("div", { className: "table-caption", text: settings.caption }));
    }
    if (settings.payload && settings.view) {
      frame.appendChild(paginationNode(settings.payload, settings.view, rows.length));
    }
    return frame;
  }

  function filterToolbar(view, rows, options, onChange) {
    var settings = options || {};
    if (!state.filters[view]) {
      state.filters[view] = { query: "", status: "" };
    }
    var filters = state.filters[view];
    if (settings.serverQuery && filters.draftQuery === undefined) {
      filters.draftQuery = filters.query;
    }
    var query = h("input", {
      className: "input",
      attrs: {
        type: "search",
        value: settings.serverQuery ? filters.draftQuery : filters.query,
        placeholder: settings.placeholder || "筛选当前页",
        "aria-label": settings.placeholder || "筛选当前页"
      },
      on: {
        input: function () {
          if (settings.serverQuery) {
            filters.draftQuery = query.value;
            if (state.filterTimers[view]) {
              window.clearTimeout(state.filterTimers[view]);
            }
            state.filterTimers[view] = window.setTimeout(function () {
              state.filterTimers[view] = null;
              filters.restoreFocus = document.activeElement === query;
              filters.query = filters.draftQuery;
              resetPage(view);
              if (state.currentView === view) {
                loadCurrentView();
              }
            }, Number(settings.queryDebounceMs) || 320);
            return;
          }
          filters.query = query.value;
          onChange();
        }
      }
    });
    var children = [query];
    if (settings.statusGetter) {
      var select = h("select", {
        className: "select",
        attrs: { "aria-label": settings.statusLabel || "按状态筛选" },
        on: {
          change: function () {
            filters.status = select.value;
            onChange();
          }
        }
      });
      select.appendChild(h("option", { text: settings.allLabel || "全部状态", attrs: { value: "" } }));
      var statusValues = uniqueValues(rows, settings.statusGetter);
      if (filters.status && statusValues.indexOf(filters.status) === -1) {
        statusValues.push(filters.status);
      }
      statusValues.forEach(function (value) {
        select.appendChild(h("option", {
          text: statusLabel(value),
          attrs: { value: value, selected: value === filters.status }
        }));
      });
      children.push(select);
    }
    return h("div", { className: "toolbar" }, children);
  }

  function filteredRows(view, rows, statusGetter, skipQuery) {
    var filters = state.filters[view] || { query: "", status: "" };
    var query = skipQuery ? "" : filters.query.trim().toLowerCase();
    return rows.filter(function (row) {
      if (query && toSearchText(row).indexOf(query) === -1) {
        return false;
      }
      if (filters.status && statusGetter && formatScalar(statusGetter(row)) !== filters.status) {
        return false;
      }
      return true;
    });
  }

  function kpiCard(label, value, detail) {
    return h("article", { className: "kpi-card" }, [
      h("div", { className: "kpi-label", text: label }),
      h("div", { className: "kpi-value", text: formatScalar(value), attrs: { title: formatScalar(value) } }),
      h("div", { className: "kpi-detail", text: detail || "当前范围投影", attrs: { title: detail || "" } })
    ]);
  }

  function normalizedNamedRows(value) {
    if (Array.isArray(value)) {
      return value;
    }
    if (!isObject(value)) {
      return [];
    }
    return Object.keys(value).map(function (key) {
      var entry = value[key];
      if (isObject(entry)) {
        var copy = Object.assign({}, entry);
        if (!copy.name) {
          copy.name = key;
        }
        return copy;
      }
      return { name: key, value: entry };
    });
  }

  function renderOverview(payload) {
    var data = envelopeObject(payload);
    var memoryTotal = firstValue(data, [
      "memory.total", "memories.total", "metrics.memory_total", "memory_count",
      "memory_total", "total_memories"
    ], "--");
    var requestTotal = firstValue(data, [
      "requests.total", "metrics.request_total", "request_count", "request_total",
      "total_requests", "calls.total"
    ], "--");
    var synthesisTotal = firstValue(data, [
      "synthesis.total", "metrics.synthesis_total", "synthesis_count", "total_synthesis"
    ], "--");
    var operationTotal = firstValue(data, [
      "operations.total", "metrics.operation_total", "operation_count", "total_operations"
    ], "--");
    var readiness = String(firstValue(data, ["readiness.status", "status", "health.status"], payload.degraded ? "degraded" : "ready"));
    var readinessKind = statusKind(readiness);
    if (parseBoolean(firstValue(data, ["recovering", "readiness.recovering"], false))) {
      readiness = "recovering";
      readinessKind = "warning";
    }
    setReadiness(statusLabel(readiness), readinessKind);

    var components = normalizedNamedRows(firstValue(data, [
      "readiness.components", "components", "health.components", "body_systems"
    ], {}));
    var operational = [
      { label: "运行模式", value: firstValue(data, ["runtime.mode", "runtime_mode", "mode"], "--") },
      { label: "待处理发件箱", value: firstValue(data, ["outbox.pending", "operations.pending_outbox", "pending_outbox_count", "pending_outbox"], "--") },
      { label: "降级事件", value: firstValue(data, ["degradations.total", "operations.degradation_count", "degradation_count"], "--") },
      { label: "运行事件", value: firstValue(data, ["runtime_event_count", "operations.runtime_event_count"], "--") }
    ];

    var root = h("div", {}, [
      viewHeader(VIEWS.overview.title, VIEWS.overview.description),
      h("div", { className: "kpi-grid" }, [
        kpiCard("规范记忆", memoryTotal, "已授权的记忆记录"),
        kpiCard("近期请求", requestTotal, "当前范围内的调用链"),
        kpiCard("综合记忆", synthesisTotal, "受治理的生命周期记录"),
        kpiCard("运行运维", operationTotal, "运行、降级与发件箱")
      ])
    ]);

    var componentContent;
    if (components.length) {
      componentContent = h("ul", { className: "status-list" }, components.map(function (component) {
        var name = componentLabel(firstValue(component, ["name", "component", "key"], "组件"));
        var status = firstValue(component, ["status", "state"], component.value !== undefined ? component.value : "--");
        var detail = componentLabel(firstValue(component, ["detail", "message", "reason"], ""));
        return h("li", { className: "status-row" }, [
          h("span", { className: "status-row-label", text: detail ? name + " - " + detail : name }),
          makeBadge(status)
        ]);
      }));
    } else {
      componentContent = statePanel("empty", "暂无组件详情", "概览接口没有返回组件级就绪状态记录。");
    }

    var operationalList = h("dl", { className: "metric-list" }, operational.map(function (item) {
      return h("div", { className: "metric-row" }, [
        h("dt", { text: item.label }),
        h("dd", { text: formatScalar(item.value) })
      ]);
    }));

    root.appendChild(h("section", { className: "section-block" }, [
      sectionHeader("系统状态", "只读"),
      h("div", { className: "split-grid" }, [
        h("div", { className: "panel" }, [
          h("div", { className: "panel-header" }, h("h2", { text: "组件就绪状态" })),
          h("div", { className: "panel-body" }, componentContent)
        ]),
        h("div", { className: "panel" }, [
          h("div", { className: "panel-header" }, h("h2", { text: "运行摘要" })),
          h("div", { className: "panel-body" }, operationalList)
        ])
      ])
    ]));
    refs.viewRoot.replaceChildren(root);
  }

  function requestStatus(row) {
    return firstValue(row, ["status", "state"], "unknown");
  }

  function renderRequests(payload) {
    var rows = envelopeRows(payload, ["requests", "call_spans", "spans"]);
    var root = h("div", {}, viewHeader(VIEWS.requests.title, VIEWS.requests.description));
    if (!rows.length) {
      root.appendChild(paginatedEmptyState(
        payload,
        "requests",
        "当前范围内暂无请求",
        "配置的项目调用 MCP 工具后，请求会显示在这里。"
      ));
      refs.viewRoot.replaceChildren(root);
      return;
    }

    var tableHost = h("div");
    var update = function () {};
    var toolbar = filterToolbar("requests", rows, {
      placeholder: "筛选工具、调用方、范围或调用 ID",
      statusGetter: requestStatus
    }, function () {
      update();
    });
    var columns = [
      {
        label: "时间", width: "16%",
        value: function (row) { return firstValue(row, ["started_at", "created_at"], ""); },
        render: function (value) { return h("span", { className: "cell-text", text: formatTimestamp(value), attrs: { title: formatTimestamp(value) } }); }
      },
      {
        label: "工具", width: "19%",
        value: function (row) { return firstValue(row, ["tool_name", "tool", "stage_name"], "--"); },
        render: function (value) { return h("span", { className: "cell-text cell-primary", text: formatScalar(value) }); }
      },
      {
        label: "状态", width: "11%",
        value: requestStatus,
        render: function (value, row) {
          return makeBadge(parseBoolean(row.degraded) && statusKind(value) === "success" ? "degraded" : value);
        }
      },
      {
        label: "耗时", width: "10%",
        value: function (row) { return firstValue(row, ["duration_ms", "latency_ms", "elapsed_ms"], null); },
        render: function (value, row) { return h("span", { className: "cell-text duration-value duration-" + durationKind(row), text: formatDuration(value, row) }); }
      },
      {
        label: "调用方", width: "12%",
        value: function (row) { return firstValue(row, ["caller", "actor", "principal"], "--"); },
        render: function (value) { return h("span", { className: "cell-text", text: formatScalar(value) }); }
      },
      {
        label: "范围", width: "17%",
        value: function (row) { return firstValue(row, ["request_scope_id", "project_id"], "--"); },
        render: function (value) { return h("code", { className: "cell-code", text: compactId(value), attrs: { title: formatScalar(value) } }); }
      },
      {
        label: "调用 ID", width: "11%",
        value: function (row) { return firstValue(row, ["call_id", "id"], "--"); },
        render: function (value) { return h("code", { className: "cell-code", text: compactId(value), attrs: { title: formatScalar(value) } }); }
      },
      {
        label: "", width: "4%",
        value: function () { return ""; },
        render: function (value, row) {
          var tool = String(firstValue(row, ["tool_name", "tool"], "")).toLowerCase();
          var callId = firstValue(row, ["call_id", "id"], "");
          if (!callId || (tool.indexOf("recall") === -1 && tool.indexOf("context") === -1)) {
            return "";
          }
          return iconButton("file-search", "打开检索解释", function (event) {
            event.stopPropagation();
            navigate("explain", { call_id: callId });
          });
        }
      }
    ];
    update = function () {
      var visible = filteredRows("requests", rows, requestStatus);
      if (!visible.length) {
        tableHost.replaceChildren(paginatedEmptyState(
          payload,
          "requests",
          "没有匹配的请求",
          "请调整当前页筛选条件，或浏览其他页面查看更多调用。"
        ));
        return;
      }
      tableHost.replaceChildren(tableNode(columns, visible, {
        payload: payload,
        view: "requests",
        onRow: function (row, index, trigger) {
          openRecordDetail("请求详情", "调用链", row, trigger);
        },
        rowLabel: function (row) {
          return "打开请求 " + firstValue(row, ["call_id", "id"], "详情");
        }
      }));
    };
    root.appendChild(toolbar);
    root.appendChild(tableHost);
    update();
    refs.viewRoot.replaceChildren(root);
  }

  function memoryId(row) {
    return firstValue(row, ["id", "memory_id"], "");
  }

  function memoryStatus(row) {
    var direct = firstValue(row, [
      "synthesis_status", "status", "lifecycle_state", "state", "metadata.lifecycle_status"
    ], "");
    if (direct) {
      return direct;
    }
    var lifecycleTag = asArray(row && row.tags).find(function (tag) {
      return String(tag).indexOf("status:") === 0;
    });
    return lifecycleTag ? String(lifecycleTag).slice(7) : "active";
  }

  function memoryContent(row) {
    return firstValue(row, ["content", "content_preview", "memory", "text", "summary"], "--");
  }

  function renderMemories(payload) {
    var rows = envelopeRows(payload, ["memories", "results", "items"]);
    var root = h("div", {}, viewHeader(VIEWS.memories.title, VIEWS.memories.description));
    var tableHost = h("div");
    var update = function () {};
    var toolbar = filterToolbar("memories", rows, {
      placeholder: "搜索全部记忆内容",
      statusGetter: memoryStatus,
      serverQuery: true,
      queryDebounceMs: 320
    }, function () {
      update();
    });
    var columns = [
      {
        label: "更新时间", width: "14%",
        value: function (row) { return firstValue(row, ["updated_at", "created_at"], ""); },
        render: function (value) { return h("span", { className: "cell-text", text: formatTimestamp(value), attrs: { title: formatTimestamp(value) } }); }
      },
      {
        label: "内容", width: "31%",
        value: memoryContent,
        render: function (value) { return h("span", { className: "cell-wrap cell-primary", text: formatScalar(value), attrs: { title: formatScalar(value) } }); }
      },
      {
        label: "类型", width: "10%",
        value: function (row) { return firstValue(row, ["memory_type", "type", "category"], "--"); },
        render: function (value) { return makeBadge(value, String(value).toLowerCase() === "synthesis" ? "brand" : "neutral"); }
      },
      {
        label: "结构切片", width: "10%",
        value: function (row) { return firstValue(row, ["chunk_count", "chunking.chunk_count"], 0); },
        render: function (value, row) {
          var count = Number(value);
          var chunkStatus = String(firstValue(row, ["chunking.status"], "not_recorded"));
          if (chunkStatus === "invalid") {
            return makeBadge("校验失败", "danger");
          }
          if (chunkStatus === "available") {
            return makeBadge(formatNumber(count) + " 个", "success");
          }
          return makeBadge("未记录", "neutral");
        }
      },
      {
        label: "领域", width: "11%",
        value: function (row) { return firstValue(row, ["domain", "metadata.domain"], "--"); },
        render: function (value) { return h("span", { className: "cell-text", text: formatScalar(value) }); }
      },
      {
        label: "状态", width: "10%",
        value: memoryStatus,
        render: function (value) { return makeBadge(value); }
      },
      {
        label: "可见性", width: "10%",
        value: function (row) { return firstValue(row, ["visibility", "scope.visibility"], "--"); },
        render: function (value) { return makeBadge(value, "info"); }
      },
      {
        label: "价值", width: "7%",
        value: function (row) {
          var direct = firstValue(row, ["worth", "worth_score", "metadata.worth"], null);
          if (direct !== null) {
            return direct;
          }
          var success = firstValue(row, ["worth_success"], null);
          var failure = firstValue(row, ["worth_failure"], null);
          return success !== null || failure !== null
            ? formatNumber(success || 0) + "/" + formatNumber(failure || 0)
            : "--";
        },
        render: function (value) { return h("span", { className: "cell-text mono", text: formatScalar(value) }); }
      },
      {
        label: "ID", width: "7%",
        value: memoryId,
        render: function (value) { return h("code", { className: "cell-code", text: compactId(value), attrs: { title: formatScalar(value) } }); }
      }
    ];
    update = function () {
      var visible = filteredRows("memories", rows, memoryStatus, true);
      if (!visible.length) {
        var filters = state.filters.memories || {};
        var searching = Boolean(String(filters.query || "").trim());
        tableHost.replaceChildren(paginatedEmptyState(
          payload,
          "memories",
          searching || filters.status ? "没有匹配的记忆" : "暂无可见记忆",
          searching
            ? "当前范围内没有内容匹配的规范记忆，请调整搜索词。"
            : filters.status
              ? "当前搜索结果中没有该状态的记忆，请调整状态筛选。"
              : "当前页面没有可用的规范记忆记录。"
        ));
        return;
      }
      tableHost.replaceChildren(tableNode(columns, visible, {
        payload: payload,
        view: "memories",
        onRow: function (row, index, trigger) {
          openMemoryDetail(memoryId(row), trigger);
        },
        rowLabel: function (row) {
          return "打开记忆 " + compactId(memoryId(row));
        }
      }));
    };
    root.appendChild(toolbar);
    root.appendChild(tableHost);
    update();
    refs.viewRoot.replaceChildren(root);
    if (state.filters.memories && state.filters.memories.restoreFocus) {
      state.filters.memories.restoreFocus = false;
      var searchInput = toolbar.querySelector("input[type='search']");
      if (searchInput) {
        searchInput.focus({ preventScroll: true });
        searchInput.setSelectionRange(searchInput.value.length, searchInput.value.length);
      }
    }
  }

  function detailSection(title, content) {
    return h("section", { className: "detail-section" }, [
      h("h3", { text: title }),
      content
    ]);
  }

  function keyValueGrid(record, skipKeys) {
    var skipped = new Set(skipKeys || []);
    var entries = Object.keys(record || {}).filter(function (key) {
      return !skipped.has(key) && !isObject(record[key]) && !Array.isArray(record[key]);
    }).sort();
    return h("dl", { className: "key-value-grid" }, entries.map(function (key) {
      var value = SENSITIVE_KEY.test(key) ? "[已脱敏]" : formatScalar(record[key]);
      return h("div", { className: "key-value" }, [
        h("dt", { text: fieldLabel(key) }),
        h("dd", { text: value })
      ]);
    }));
  }

  function recordDetail(record) {
    var content = firstValue(record, ["content", "memory", "text", "summary", "description"], "");
    var children = [];
    if (content) {
      children.push(detailSection("内容", h("p", { className: "detail-content", text: formatScalar(content) })));
    }
    children.push(detailSection("字段", keyValueGrid(record, ["content", "memory", "text", "summary", "description", "metadata", "metadata_json", "chunks", "chunking"])));
    Object.keys(record || {}).filter(function (key) {
      return (isObject(record[key]) || Array.isArray(record[key]) || key === "metadata_json")
        && key !== "chunks" && key !== "chunking";
    }).sort().forEach(function (key) {
      var value = record[key];
      if (key === "metadata_json" && typeof value === "string") {
        try {
          value = JSON.parse(value);
        } catch (error) {
          value = { value: value };
        }
      }
      children.push(detailSection(fieldLabel(key), h("pre", {
        className: "json-view",
        text: safeJson(value)
      })));
    });
    return h("div", {}, children);
  }

  function chunkKindLabel(value) {
    var normalized = String(value || "unknown").toLowerCase();
    return STATUS_LABELS[normalized] || String(value || "未知");
  }

  function chunkHeaderLabel(chunk) {
    var path = firstValue(chunk, ["header_path", "heading_path"], []);
    return Array.isArray(path) && path.length ? path.join(" / ") : "无标题路径";
  }

  function chunkSpanLabel(chunk) {
    var start = Number(firstValue(chunk, ["source_start"], NaN));
    var end = Number(firstValue(chunk, ["source_end"], NaN));
    if (!Number.isFinite(start) || !Number.isFinite(end)) {
      return "来源跨度未记录";
    }
    return "字符 " + formatNumber(start) + "–" + formatNumber(end);
  }

  function chunkCard(chunk) {
    var chunkId = firstValue(chunk, ["chunk_id", "id"], "--");
    var text = firstValue(chunk, ["text", "text_preview"], "");
    var ordinal = firstValue(chunk, ["display_ordinal", "ordinal"], null);
    return h("details", { className: "chunk-card" }, [
      h("summary", { className: "chunk-card-summary" }, [
        h("span", { className: "chunk-ordinal", text: ordinal === null ? "#--" : "#" + formatNumber(ordinal) }),
        makeBadge(chunkKindLabel(firstValue(chunk, ["kind"], "unknown")), "info"),
        h("span", { className: "chunk-header", text: chunkHeaderLabel(chunk), attrs: { title: chunkHeaderLabel(chunk) } }),
        h("code", { className: "chunk-span", text: chunkSpanLabel(chunk) })
      ]),
      h("div", { className: "chunk-card-body" }, [
        h("div", { className: "chunk-detail-grid" }, [
          h("div", {}, [h("span", { className: "panel-kicker", text: "切片 ID" }), h("code", { text: formatScalar(chunkId), attrs: { title: formatScalar(chunkId) } })]),
          h("div", {}, [h("span", { className: "panel-kicker", text: "父记忆" }), h("code", { text: compactId(firstValue(chunk, ["parent_memory_id"], "--")) })]),
          h("div", {}, [h("span", { className: "panel-kicker", text: "来源哈希" }), h("code", { text: compactId(firstValue(chunk, ["source_hash"], "--")), attrs: { title: formatScalar(firstValue(chunk, ["source_hash"], "--")) } })]),
          h("div", {}, [h("span", { className: "panel-kicker", text: "文本哈希" }), h("code", { text: compactId(firstValue(chunk, ["text_hash"], "--")), attrs: { title: formatScalar(firstValue(chunk, ["text_hash"], "--")) } })])
        ]),
        text ? h("pre", { className: "chunk-text", text: text }) : makeNotice("info", "未返回切片正文", "当前投影只保留结构化锚点。")
      ])
    ]);
  }

  function structuredChunksPanel(record) {
    var chunks = asArray(record && record.chunks);
    var chunking = isObject(record && record.chunking) ? record.chunking : {};
    var status = String(firstValue(chunking, ["status"], chunks.length ? "available" : "not_recorded"));
    var summary = [
      { label: "总切片", value: firstValue(chunking, ["chunk_count"], chunks.length) },
      { label: "当前返回", value: firstValue(chunking, ["returned_count"], chunks.length) },
      { label: "投影上限", value: firstValue(chunking, ["projection_limit"], "--") },
      { label: "投影截断", value: parseBoolean(firstValue(chunking, ["projection_truncated"], false)) ? "是" : "否" },
      { label: "切分截断", value: parseBoolean(firstValue(chunking, ["truncated"], false)) ? "是" : "否" },
      { label: "资源受限", value: parseBoolean(firstValue(chunking, ["resource_limited"], false)) ? "是" : "否" },
      { label: "模式", value: firstValue(chunking, ["schema_version"], "未启用") },
      { label: "覆盖字符", value: firstValue(chunking, ["covered_source_chars"], "--") }
    ];
    var body = [
      h("div", { className: "chunk-summary-grid" }, summary.map(function (item) {
        return h("div", { className: "chunk-summary-item" }, [
          h("span", { className: "panel-kicker", text: item.label }),
          h("strong", { text: formatScalar(item.value) })
        ]);
      }))
    ];
    if (chunks.length) {
      body.push(h("div", { className: "chunk-list" }, chunks.map(chunkCard)));
    } else if (status === "invalid") {
      var invalidReason = String(firstValue(chunking, ["reason"], "manifest_invalid"));
      var invalidMessages = {
        manifest_hash_missing: "索引物料缺少完整性哈希，因此不会把这些切片作为谱系或检索证据展示。",
        manifest_hash_mismatch: "索引物料与完整性哈希不一致，因此不会把这些切片作为谱系或检索证据展示。",
        manifest_source_mismatch: "切片清单的来源哈希与当前记忆正文不一致，因此已拒绝展示该清单。",
        manifest_shape_invalid: "切片清单的数量或结构不符合 structure-v1 契约，因此已拒绝展示该清单。"
      };
      body.push(makeNotice(
        "warning",
        "切片元数据校验失败",
        invalidMessages[invalidReason] || "切片清单未通过完整性校验，因此不会作为谱系或检索证据展示。"
      ));
    } else if (status === "available") {
      body.push(makeNotice("info", "结构化切片清单为空", "清单已通过校验，但这条记忆没有可返回的切片。"));
    } else {
      body.push(makeNotice("info", "尚未记录结构化切片", "只有 full / rust-full 且成功写入索引物料的记忆会显示切片；这不影响父记忆本身的生命周期。"));
    }
    var statusLabel = status === "available"
      ? "已记录"
      : status === "invalid" ? "校验失败" : "未记录";
    return h("section", { className: "detail-section structured-chunks-section" }, [
      h("div", { className: "detail-section-heading" }, [
        h("h3", { text: "结构化切片" }),
        makeBadge(statusLabel, status === "available" ? "success" : "warning")
      ]),
      h("p", { className: "detail-section-copy", text: "这里展示确定性结构切片清单。当前向量索引按父记忆聚合，切片不是独立索引行，也不代表精确切片命中。" }),
      body
    ]);
  }

  function openDialog(title, eyebrow, body, trigger) {
    state.detailFocus = trigger || document.activeElement;
    refs.detailTitle.textContent = title;
    refs.detailEyebrow.textContent = eyebrow || "详情";
    refs.detailBody.replaceChildren(body);
    if (typeof refs.detailDialog.showModal === "function") {
      if (!refs.detailDialog.open) {
        refs.detailDialog.showModal();
      }
    } else {
      refs.detailDialog.setAttribute("open", "");
    }
    refs.detailClose.focus();
  }

  function closeDialog() {
    state.detailSequence += 1;
    if (typeof refs.detailDialog.close === "function" && refs.detailDialog.open) {
      refs.detailDialog.close();
    } else {
      refs.detailDialog.removeAttribute("open");
    }
    if (state.detailFocus && typeof state.detailFocus.focus === "function") {
      state.detailFocus.focus();
    }
    state.detailFocus = null;
  }

  function openRecordDetail(title, eyebrow, record, trigger) {
    openDialog(title, eyebrow, recordDetail(record), trigger);
  }

  function openMemoryDetail(id, trigger) {
    if (!id) {
      toast("此记忆记录没有规范 ID。", true);
      return;
    }
    state.detailSequence += 1;
    var sequence = state.detailSequence;
    var loading = statePanel("loading", "正在加载记忆", "正在读取当前范围内的 ID 直查投影。");
    openDialog("记忆详情", "规范记忆", loading, trigger);
    apiRequest("/memories/" + encodeURIComponent(id), {}, null).then(function (payload) {
      if (sequence !== state.detailSequence) {
        return;
      }
      var record = isObject(payload.data) ? payload.data : envelopeObject(payload);
      var body = recordDetail(record);
      var lineageButton = textButton("查看谱系", function () {
        closeDialog();
        navigate("lineage", { memory_id: id });
      }, { icon: "git-branch" });
      body.insertBefore(structuredChunksPanel(record), body.firstChild);
      body.insertBefore(h("div", { className: "view-header-actions" }, lineageButton), body.firstChild);
      refs.detailBody.replaceChildren(body);
    }).catch(function (error) {
      if (sequence !== state.detailSequence) {
        return;
      }
      refs.detailBody.replaceChildren(statePanel(
        "error",
        error.status === 404 ? "未找到记忆" : "无法加载记忆",
        error.message
      ));
    });
  }

  function nonnegativeCount(value, fallback) {
    if (value === null || value === undefined || value === "" || typeof value === "boolean") {
      return fallback;
    }
    var number = Number(value);
    return Number.isFinite(number) && number >= 0 ? number : fallback;
  }

  function chunkAnchorContract(items, summary, fallbackTotal) {
    var anchors = asArray(items);
    var contract = isObject(summary) ? summary : {};
    var returned = nonnegativeCount(firstValue(contract, ["returned"], null), anchors.length);
    var total = nonnegativeCount(firstValue(contract, ["total"], null), nonnegativeCount(fallbackTotal, returned));
    total = Math.max(total, returned);
    var explicitTruncated = firstValue(contract, ["truncated"], null);
    return {
      total: total,
      returned: returned,
      limit: nonnegativeCount(firstValue(contract, ["limit"], null), null),
      truncated: explicitTruncated === null ? returned < total : parseBoolean(explicitTruncated)
    };
  }

  function chunkAnchorContractLabel(contract) {
    return "返回 " + formatNumber(contract.returned)
      + " / 共 " + formatNumber(contract.total)
      + (contract.truncated ? " · 已截断" : " · 完整");
  }

  function nodeChunkingLabel(row) {
    var chunking = isObject(row && row.chunking) ? row.chunking : {};
    var status = String(firstValue(chunking, ["status"], "not_recorded"));
    if (status === "invalid") {
      return "校验失败";
    }
    if (status !== "available") {
      return "未记录";
    }
    return firstValue(chunking, ["schema_version"], "structure-v1")
      + " · " + formatNumber(firstValue(chunking, ["chunk_count"], 0)) + " 片";
  }

  function nodeAnchorContract(row) {
    var chunking = isObject(row && row.chunking) ? row.chunking : {};
    return chunkAnchorContract(
      row && row.chunk_anchors,
      row && row.chunk_anchor_summary,
      firstValue(chunking, ["chunk_count"], null)
    );
  }

  function edgeAnchorContract(row, side) {
    var anchors = isObject(row && row.chunk_anchors) ? row.chunk_anchors : {};
    return chunkAnchorContract(
      anchors[side],
      anchors[side + "_summary"],
      null
    );
  }

  function lineageAnchorPanel(anchor, summary) {
    if (!isObject(anchor)) {
      return null;
    }
    var legacyCount = Number(firstValue(summary, ["legacy_global_edges"], 0));
    var id = memoryId(anchor);
    var content = memoryContent(anchor);
    var relations = isObject(summary && summary.relations) ? summary.relations : {};
    var relationChips = Object.keys(relations).map(function (relation) {
      return h("span", { className: "relation-chip" }, [
        h("span", { text: statusLabel(relation) }),
        h("strong", { text: formatNumber(relations[relation]) })
      ]);
    });
    return h("section", { className: "panel lineage-anchor" }, [
      h("div", { className: "panel-header" }, [
        h("div", { className: "panel-title-group" }, [
          h("span", { className: "panel-kicker", text: "当前锚点" }),
          h("h3", { text: "规范记忆" })
        ]),
        makeBadge(memoryStatus(anchor))
      ]),
      h("div", { className: "panel-body" }, [
        h("p", { className: "lineage-anchor-content", text: content || "暂无记忆摘要" }),
        h("div", { className: "lineage-anchor-meta" }, [
          h("div", { className: "anchor-meta-item" }, [
            h("span", { text: "记忆 ID" }),
            h("code", { text: formatScalar(id), attrs: { title: formatScalar(id) } })
          ]),
          h("div", { className: "anchor-meta-item" }, [
            h("span", { text: "类型" }),
            h("strong", { text: statusLabel(firstValue(anchor, ["memory_type", "source_class"], "规范记忆")) })
          ]),
          h("div", { className: "anchor-meta-item" }, [
            h("span", { text: "可见性" }),
            h("strong", { text: statusLabel(firstValue(anchor, ["visibility", "scope"], "--")) })
          ]),
          h("div", { className: "anchor-meta-item" }, [
            h("span", { text: "创建时间" }),
            h("strong", { text: formatTimestamp(firstValue(anchor, ["created_at", "updated_at"], "")) })
          ])
        ]),
        relationChips.length ? h("div", { className: "relation-chips" }, relationChips) : null,
        legacyCount
          ? makeNotice("info", "已兼容全局谱系", "其中 " + legacyCount + " 条关系来自 legacy-global；仅展示两个端点均为全局可见的结构证据。")
          : null
      ])
    ]);
  }

  function lineageFlow(row) {
    var parent = firstValue(row, ["source", "parent_memory_id", "source_memory_id", "from_id"], "--");
    var child = firstValue(row, ["target", "memory_id", "target_memory_id", "to_id"], "--");
    return h("div", { className: "lineage-flow" }, [
      h("div", { className: "lineage-node" }, [
        h("span", { text: "父记忆" }),
        h("code", { text: compactId(parent), attrs: { title: formatScalar(parent) } })
      ]),
      h("span", { className: "lineage-flow-arrow", text: "→", attrs: { "aria-hidden": "true" } }),
      h("div", { className: "lineage-node" }, [
        h("span", { text: "子记忆" }),
        h("code", { text: compactId(child), attrs: { title: formatScalar(child) } })
      ])
    ]);
  }

  function lineageTimelineItem(row) {
    var relation = firstValue(row, ["relation", "type", "event"], "related");
    var callId = firstValue(row, ["call_id", "event_id"], "");
    var evidenceScope = firstValue(row, ["evidence_scope"], "project_evidence");
    if (evidenceScope === "project") {
      evidenceScope = "project_evidence";
    }
    var sourceContract = edgeAnchorContract(row, "source");
    var targetContract = edgeAnchorContract(row, "target");
    var call = isObject(row && row.call) ? row.call : {};
    return h("li", { className: "timeline-item lineage-timeline-item" }, [
      h("div", { className: "timeline-title-row" }, [
        h("div", { className: "timeline-title", text: statusLabel(relation) }),
        makeBadge(evidenceScope, evidenceScope === "legacy_global" ? "brand" : "neutral")
      ]),
      lineageFlow(row),
      h("div", { className: "timeline-meta", text: [
        callId ? "调用 " + compactId(callId) : "未记录调用 ID",
        formatTimestamp(firstValue(row, ["timestamp", "created_at"], "")),
        "端点切片清单：父 " + sourceContract.returned + "/" + sourceContract.total
          + "，子 " + targetContract.returned + "/" + targetContract.total
          + (sourceContract.truncated || targetContract.truncated ? "（已截断）" : ""),
        durationKind(call) === "measured"
          ? "耗时 " + formatDuration(firstValue(call, ["duration_ms", "latency_ms"], null), call)
          : durationKind(call) === "invalid" ? "耗时无效" : "耗时未采集"
      ].join(" · ") })
    ]);
  }

  function lineageNodePanel(nodes) {
    if (!nodes.length) {
      return null;
    }
    return h("section", { className: "section-block lineage-node-section" }, [
      sectionHeader("谱系节点", nodes.length + " 个有类型节点"),
      tableNode([
        {
          label: "节点", width: "20%", value: function (row) { return row.id; },
          render: function (value, row) {
            return h("div", { className: "lineage-node-identity" }, [
              h("code", { className: "cell-code", text: compactId(value), attrs: { title: formatScalar(value) } }),
              h("span", { className: "cell-subtext", text: statusLabel(firstValue(row, ["type"], "memory")) })
            ]);
          }
        },
        {
          label: "角色", width: "14%", value: function (row) { return asArray(row.roles); },
          render: function (value) {
            var labels = asArray(value).map(roleLabel);
            return h("span", { className: "cell-wrap", text: labels.join("、") || "关联记忆" });
          }
        },
        {
          label: "记忆类型", width: "12%", value: function (row) { return firstValue(row, ["memory_type", "source_class"], "--"); },
          render: function (value) { return makeBadge(value, "info"); }
        },
        {
          label: "可见性", width: "10%", value: function (row) { return firstValue(row, ["visibility"], "--"); },
          render: function (value) { return makeBadge(value); }
        },
        {
          label: "切片方式", width: "17%", value: nodeChunkingLabel,
          render: function (value, row) {
            var status = firstValue(row, ["chunking.status"], "not_recorded");
            return h("div", { className: "lineage-chunking-cell" }, [
              h("span", { className: "cell-wrap", text: value }),
              makeBadge(status === "invalid" ? "校验失败" : status, status === "invalid" ? "danger" : "neutral")
            ]);
          }
        },
        {
          label: "清单锚点", width: "15%", value: nodeAnchorContract,
          render: function (value) {
            return h("span", {
              className: "cell-wrap mono" + (value.truncated ? " is-warning" : ""),
              text: chunkAnchorContractLabel(value),
              attrs: { title: value.limit === null ? "" : "投影上限 " + formatNumber(value.limit) }
            });
          }
        },
        {
          label: "创建时间", width: "12%", value: function (row) { return row.created_at; },
          render: function (value) { return h("span", { className: "cell-text", text: formatTimestamp(value) }); }
        }
      ], nodes, {
        onRow: function (row, index, trigger) {
          openRecordDetail("谱系节点详情", "有类型节点", row, trigger);
        },
        rowLabel: function (row) { return "查看谱系节点 " + compactId(row.id); }
      })
    ]);
  }

  function lineageAnchorSummary(row) {
    var anchors = isObject(row && row.chunk_anchors) ? row.chunk_anchors : {};
    var status = firstValue(anchors, ["status"], "not_recorded");
    var source = edgeAnchorContract(row, "source");
    var target = edgeAnchorContract(row, "target");
    return "父 " + source.returned + "/" + source.total
      + " · 子 " + target.returned + "/" + target.total
      + (source.truncated || target.truncated ? " · 已截断" : "")
      + " · " + statusLabel(status);
  }

  function lineageChunkAnchorNotice(summary) {
    if (!isObject(summary)) {
      return null;
    }
    var total = nonnegativeCount(firstValue(summary, ["chunk_anchor_count"], null), null);
    var returned = nonnegativeCount(firstValue(summary, ["chunk_anchor_returned"], null), null);
    if (total === null && returned === null) {
      return null;
    }
    total = total === null ? returned : total;
    returned = returned === null ? total : returned;
    var truncated = parseBoolean(firstValue(summary, ["chunk_anchors_truncated"], returned < total));
    return makeNotice(
      truncated ? "warning" : "info",
      "谱系切片清单：返回 " + formatNumber(returned) + " / 共 " + formatNumber(total),
      truncated
        ? "为保持谱系响应有界，当前只返回部分结构切片锚点；总数仍来自已校验清单。关系边不会据此推断精确切片命中。"
        : "结构切片锚点已完整返回；它们描述关系两端的切片清单，不代表这条关系指向某个精确切片。"
    );
  }

  function synthesisGovernancePanel(payload, hasRows) {
    var governance = isObject(payload && payload.governance) ? payload.governance : {};
    var summary = isObject(payload && payload.summary) ? payload.summary : {};
    var counts = isObject(summary.status_counts) ? summary.status_counts : {};
    var mode = String(firstValue(governance, ["artifacts_mode"], "off")).toLowerCase();
    var message;
    if (mode === "off") {
      message = "综合记忆创建门控当前为 off，因此系统不会生成或展示新的综合记忆。现有表为空是受治理的结果，不是加载失败。";
    } else if (mode === "shadow") {
      message = "当前为 shadow 模式：系统只评估候选，不落受治理综合记忆；需要切换到 on 才允许创建草稿。";
    } else if (mode === "on") {
      message = "综合记忆创建已开启，但仍需满足来源、可见性、哈希和验证条件；未满足条件的候选不会进入这里。";
    } else {
      message = "综合记忆门控配置无效，系统按 fail-closed 处理，不会展示未经确认的记录。";
    }
    var gates = [
      { label: "创建门控", value: mode },
      { label: "检索门控", value: governance.retrieval_effective ? "有效" : "未生效" },
      { label: "提案门控", value: firstValue(governance, ["proposal_mode"], "off") },
      { label: "规范记录", value: firstValue(summary, ["artifact_count"], 0) }
    ];
    var statusCounts = ["draft", "verified", "stale", "contested"].map(function (status) {
      return h("div", { className: "lifecycle-stat" }, [
        makeBadge(status),
        h("strong", { text: formatNumber(firstValue(counts, [status], 0)) })
      ]);
    });
    return h("div", { className: "synthesis-empty-layout" }, [
      h("section", { className: "panel synthesis-empty-panel" }, [
        h("div", { className: "panel-header" }, [
          h("div", { className: "panel-title-group" }, [
            h("span", { className: "panel-kicker", text: "生命周期门控" }),
            h("h3", { text: hasRows ? "综合记忆治理状态" : "当前没有可显示的综合记忆" })
          ]),
          makeBadge(mode, mode === "on" ? "success" : "warning")
        ]),
        h("div", { className: "panel-body" }, [
          makeNotice(mode === "on" ? "info" : "warning", hasRows ? "仅展示已通过门控的记录" : "空态是有原因的", message),
          h("div", { className: "gate-grid" }, gates.map(function (gate) {
            return h("div", { className: "gate-item" }, [
              h("span", { className: "panel-kicker", text: gate.label }),
              h("strong", { text: typeof gate.value === "number" ? formatNumber(gate.value) : statusLabel(gate.value) })
            ]);
          })),
          h("div", { className: "lifecycle-strip" }, statusCounts)
        ])
      ]),
      h("section", { className: "panel synthesis-next-panel" }, [
        h("div", { className: "panel-header" }, h("h3", { text: "下一步" })),
        h("div", { className: "panel-body" }, [
          h("p", { className: "panel-copy", text: hasRows
            ? "每条记录都必须保留来源、验证者和调用证据；状态变化会产生下一修订，不会直接覆盖历史。"
            : "先从规范记忆和检索解释确认来源证据，再回到有效配置核对门控。" }),
          h("div", { className: "view-header-actions" }, [
            textButton("查看记忆", function () { navigate("memories"); }, { icon: "database" }),
            textButton("查看配置", function () { navigate("configuration"); }, { icon: "settings" })
          ])
        ])
      ])
    ]);
  }

  function renderLineage(payload, params) {
    var memories = envelopeRows(payload, ["memories", "results", "items"]);
    var root = h("div", {}, viewHeader(VIEWS.lineage.title, VIEWS.lineage.description));
    var memoryInput = h("input", {
      className: "input",
      attrs: {
        type: "text",
        value: params.memory_id || "",
        placeholder: "规范记忆 ID",
        "aria-label": "规范记忆 ID"
      }
    });
    var resultHost = h("div", { className: "section-block" });
    var form = h("form", {
      className: "toolbar",
      on: {
        submit: function (event) {
          event.preventDefault();
          var id = memoryInput.value.trim();
          if (!id) {
            toast("请输入规范记忆 ID。", true);
            return;
          }
          replaceRouteParams("lineage", { memory_id: id });
          loadLineageResult(id, resultHost);
        }
      }
    }, [
      memoryInput,
      textButton("查看谱系", null, { type: "submit", primary: true, icon: "git-branch" })
    ]);
    root.appendChild(form);
    root.appendChild(resultHost);

    if (memories.length) {
      var columns = [
        {
          label: "更新时间", width: "18%",
          value: function (row) { return firstValue(row, ["updated_at", "created_at"], ""); },
          render: function (value) { return h("span", { className: "cell-text", text: formatTimestamp(value) }); }
        },
        {
          label: "记忆", width: "52%",
          value: memoryContent,
          render: function (value) { return h("span", { className: "cell-wrap cell-primary", text: formatScalar(value) }); }
        },
        {
          label: "状态", width: "14%",
          value: memoryStatus,
          render: function (value) { return makeBadge(value); }
        },
        {
          label: "ID", width: "16%",
          value: memoryId,
          render: function (value) { return h("code", { className: "cell-code", text: compactId(value), attrs: { title: formatScalar(value) } }); }
        }
      ];
      root.appendChild(h("section", { className: "section-block lineage-recent" }, [
        sectionHeader("近期记忆", "选择一条记录"),
        tableNode(columns, memories, {
          payload: payload,
          view: "lineage",
          onRow: function (row) {
            var id = memoryId(row);
            memoryInput.value = id;
            replaceRouteParams("lineage", { memory_id: id });
            loadLineageResult(id, resultHost);
          },
          rowLabel: function (row) {
            return "查看记忆谱系 " + compactId(memoryId(row));
          }
        })
      ]));
    } else {
      root.appendChild(h("section", { className: "section-block" }, statePanel(
        "empty",
        "暂无近期记忆",
        "请在上方输入已授权的规范记忆 ID 以查看谱系。"
      )));
    }
    refs.viewRoot.replaceChildren(root);
    if (params.memory_id) {
      loadLineageResult(params.memory_id, resultHost);
    } else {
      resultHost.replaceChildren(statePanel("empty", "选择一条记忆", "请选择近期记录，或输入规范记忆 ID。"));
    }
  }

  function loadLineageResult(id, host) {
    state.lineageSequence += 1;
    var sequence = state.lineageSequence;
    host.replaceChildren(loadingState());
    apiRequest("/memories/" + encodeURIComponent(id) + "/lineage", {}, null).then(function (payload) {
      if (sequence !== state.lineageSequence || state.currentView !== "lineage") {
        return;
      }
      var rows = envelopeRows(payload, ["lineage", "edges", "relations", "data"]);
      var anchor = isObject(payload && payload.memory)
        ? payload.memory
        : envelopeObject(payload).memory;
      var nodes = asArray(firstValue(payload || {}, ["nodes", "typed_nodes"], []));
      var summary = isObject(payload && payload.summary) ? payload.summary : {};
      var anchorPanel = lineageAnchorPanel(anchor, summary);
      var chunkAnchorNotice = lineageChunkAnchorNotice(summary);
      if (!rows.length) {
        host.replaceChildren(h("section", { className: "lineage-result" }, [
          sectionHeader("谱系证据", "暂无已记录关系"),
          anchorPanel,
          lineageNodePanel(nodes),
          chunkAnchorNotice,
          statePanel("empty", "暂无谱系记录", "当前范围内的这条记忆没有已记录的谱系关系。")
        ]));
        return;
      }
      var timeline = h("ol", { className: "timeline" }, rows.map(lineageTimelineItem));
      var columns = [
        { label: "时间", width: "15%", value: function (row) { return row.created_at; }, render: function (value) { return h("span", { className: "cell-text", text: formatTimestamp(value) }); } },
        { label: "关系", width: "16%", value: function (row) { return firstValue(row, ["relation", "type"], "--"); }, render: function (value) { return makeBadge(value, "info"); } },
        { label: "父记忆", width: "17%", value: function (row) { return firstValue(row, ["source", "parent_memory_id", "source_memory_id", "from_id"], "--"); }, render: function (value) { return h("code", { className: "cell-code", text: compactId(value), attrs: { title: formatScalar(value) } }); } },
        { label: "子记忆", width: "17%", value: function (row) { return firstValue(row, ["target", "memory_id", "target_memory_id", "to_id"], "--"); }, render: function (value) { return h("code", { className: "cell-code", text: compactId(value), attrs: { title: formatScalar(value) } }); } },
        { label: "调用 ID", width: "15%", value: function (row) { return firstValue(row, ["call_id", "event_id"], "--"); }, render: function (value) { return h("code", { className: "cell-code", text: compactId(value), attrs: { title: formatScalar(value) } }); } },
        { label: "端点切片清单", width: "18%", value: lineageAnchorSummary, render: function (value) { return h("span", { className: "cell-wrap", text: value }); } },
        {
          label: "证据范围", width: "12%",
          value: function (row) { return firstValue(row, ["evidence_scope", "project_id"], "--"); },
          render: function (value) {
            return makeBadge(value === "project" ? "project_evidence" : value, value === "legacy_global" ? "brand" : "neutral");
          }
        }
      ];
      var content = [
        sectionHeader("谱系证据：" + compactId(id), rows.length + " 条关系"),
        anchorPanel,
        lineageNodePanel(nodes),
        chunkAnchorNotice,
        payload.degraded ? makeNotice("warning", "结果已降级。", "部分谱系证据可能不可用。") : null,
        summary.has_more ? makeNotice("info", "结果已限制。", "当前只展示最近 " + rows.length + " 条关系，更多证据仍在范围内。") : null,
        h("div", { className: "lineage-result-grid" }, [
          h("div", { className: "panel" }, [
            h("div", { className: "panel-header" }, [
              h("div", { className: "panel-title-group" }, [
                h("span", { className: "panel-kicker", text: "按时间倒序" }),
                h("h3", { text: "关系时间线" })
              ]),
              h("span", { className: "section-meta", text: formatNumber(rows.length) + " 条" })
            ]),
            h("div", { className: "panel-body" }, timeline)
          ]),
          h("div", { className: "lineage-evidence-table" }, [
            sectionHeader("结构证据", "可追溯字段"),
            tableNode(columns, rows, {
              onRow: function (row, index, trigger) {
                openRecordDetail("谱系关系详情", "结构证据", row, trigger);
              },
              rowLabel: function (row) {
                return "打开谱系关系 " + statusLabel(firstValue(row, ["relation", "type"], "related"));
              }
            })
          ])
        ])
      ];
      host.replaceChildren(h("section", { className: "lineage-result" }, content));
    }).catch(function (error) {
      if (sequence !== state.lineageSequence) {
        return;
      }
      host.replaceChildren(statePanel(
        "error",
        error.status === 404 ? "未找到记忆" : "无法加载谱系",
        error.message,
        function () { loadLineageResult(id, host); }
      ));
    });
  }

  function synthesisStatus(row) {
    return firstValue(row, ["status", "synthesis_status", "lifecycle_state"], "draft");
  }

  function renderSynthesis(payload) {
    var rows = envelopeRows(payload, ["synthesis", "artifacts", "items"]);
    var root = h("div", {}, viewHeader(VIEWS.synthesis.title, VIEWS.synthesis.description));
    if (!rows.length) {
      root.appendChild(synthesisGovernancePanel(payload, false));
      if ((payloadTotal(payload) || 0) > 0) {
        root.appendChild(h("div", { className: "section-block" }, paginatedEmptyState(
          payload,
          "synthesis",
          "当前页暂无综合记忆",
          "请浏览其他页面，或返回第一页查看受治理记录。"
        )));
      }
      refs.viewRoot.replaceChildren(root);
      return;
    }
    var tableHost = h("div");
    var update = function () {};
    var toolbar = filterToolbar("synthesis", rows, {
      placeholder: "筛选内容、综合键、验证者或 ID",
      statusGetter: synthesisStatus
    }, function () { update(); });
    var columns = [
      { label: "更新时间", width: "15%", value: function (row) { return firstValue(row, ["updated_at", "created_at"], ""); }, render: function (value) { return h("span", { className: "cell-text", text: formatTimestamp(value) }); } },
      { label: "综合内容", width: "28%", value: memoryContent, render: function (value) { return h("span", { className: "cell-wrap cell-primary", text: formatScalar(value), attrs: { title: formatScalar(value) } }); } },
      { label: "状态", width: "11%", value: synthesisStatus, render: function (value) { return makeBadge(value); } },
      { label: "证据数", width: "9%", value: function (row) { return firstValue(row, ["support_count", "evidence_count"], "--"); }, render: function (value) { return h("span", { className: "mono", text: formatNumber(value) }); } },
      { label: "修订", width: "9%", value: function (row) { return firstValue(row, ["revision", "synthesis_revision"], "--"); }, render: function (value) { return h("span", { className: "mono", text: formatNumber(value) }); } },
      { label: "验证者", width: "13%", value: function (row) { return firstValue(row, ["verified_by_actor", "verified_by", "verifier"], "--"); }, render: function (value) { return h("span", { className: "cell-text", text: formatScalar(value) }); } },
      { label: "ID", width: "15%", value: memoryId, render: function (value) { return h("code", { className: "cell-code", text: compactId(value), attrs: { title: formatScalar(value) } }); } }
    ];
    update = function () {
      var visible = filteredRows("synthesis", rows, synthesisStatus);
      if (!visible.length) {
        tableHost.replaceChildren(paginatedEmptyState(
          payload,
          "synthesis",
          "没有匹配的综合记忆",
          "请调整当前页筛选条件，或浏览其他页面。"
        ));
        return;
      }
      tableHost.replaceChildren(tableNode(columns, visible, {
        payload: payload,
        view: "synthesis",
        onRow: function (row, index, trigger) {
          openRecordDetail("综合记忆详情", "受治理内容", row, trigger);
        },
        rowLabel: function (row) {
          return "打开综合记忆 " + compactId(memoryId(row));
        }
      }));
    };
    root.appendChild(synthesisGovernancePanel(payload, true));
    root.appendChild(h("div", { className: "section-block" }, toolbar));
    root.appendChild(tableHost);
    update();
    refs.viewRoot.replaceChildren(root);
  }

  function operationKind(row) {
    var type = String(firstValue(row, ["record_type", "kind", "operation_type", "type"], "")).toLowerCase();
    if (type.indexOf("outbox") >= 0 || row.outbox_id) {
      return "outbox";
    }
    if (type.indexOf("degrad") >= 0 || row.event_id || row.fallback_used) {
      return "degradations";
    }
    if (type.indexOf("runtime") >= 0) {
      return "runtime";
    }
    return "";
  }

  function operationRows(payload) {
    var data = envelopeObject(payload);
    var all = envelopeRows(payload, ["operations", "items", "rows"]);
    var runtime = asArray(firstValue(data, ["runtime", "runtime_events"], []));
    var degradations = asArray(firstValue(data, ["degradations", "degradation_events"], []));
    var outbox = asArray(firstValue(data, ["outbox", "outbox_jobs", "store_outbox"], []));
    if (!runtime.length && !degradations.length && !outbox.length && all.length) {
      runtime = all.filter(function (row) { return operationKind(row) === "runtime"; });
      degradations = all.filter(function (row) { return operationKind(row) === "degradations"; });
      outbox = all.filter(function (row) { return operationKind(row) === "outbox"; });
    }
    return { runtime: runtime, degradations: degradations, outbox: outbox };
  }

  function segmented(items, active, onChange) {
    return h("div", { className: "segmented-control", attrs: { role: "tablist" } }, items.map(function (item) {
      return h("button", {
        className: "segment-button" + (item.value === active ? " is-active" : ""),
        text: item.label,
        attrs: {
          type: "button",
          role: "tab",
          "aria-selected": item.value === active
        },
        on: {
          click: function () { onChange(item.value); }
        }
      });
    }));
  }

  function renderOperations(payload) {
    var grouped = operationRows(payload);
    if (!grouped[state.operationTab].length) {
      state.operationTab = grouped.runtime.length
        ? "runtime"
        : grouped.degradations.length
          ? "degradations"
          : "outbox";
    }
    var root = h("div");
    var tableHost = h("div");
    var tabItems = [
      { value: "runtime", label: "运行事件（" + grouped.runtime.length + "）" },
      { value: "degradations", label: "降级事件（" + grouped.degradations.length + "）" },
      { value: "outbox", label: "发件箱（" + grouped.outbox.length + "）" }
    ];
    var tabControl;
    var update = function () {
      var rows = grouped[state.operationTab];
      var columns;
      if (state.operationTab === "runtime") {
        columns = [
          { label: "时间", width: "16%", value: function (row) { return row.created_at; }, render: function (value) { return h("span", { className: "cell-text", text: formatTimestamp(value) }); } },
          { label: "状态", width: "11%", value: function (row) { return firstValue(row, ["status", "state"], "--"); }, render: function (value) { return makeBadge(value); } },
          { label: "事件", width: "25%", value: function (row) { return firstValue(row, ["name", "event_name"], "--"); }, render: function (value) { return h("span", { className: "cell-wrap cell-primary", text: formatScalar(value) }); } },
          { label: "执行者", width: "14%", value: function (row) { return firstValue(row, ["actor", "caller"], "--"); }, render: function (value) { return h("span", { className: "cell-text", text: formatScalar(value) }); } },
          { label: "范围", width: "20%", value: function (row) { return firstValue(row, ["request_scope_id", "project_id"], "--"); }, render: function (value) { return h("code", { className: "cell-code", text: compactId(value), attrs: { title: formatScalar(value) } }); } },
          { label: "记录 ID", width: "14%", value: function (row) { return firstValue(row, ["record_id", "event_id", "id"], "--"); }, render: function (value) { return h("code", { className: "cell-code", text: compactId(value), attrs: { title: formatScalar(value) } }); } }
        ];
      } else if (state.operationTab === "degradations") {
        columns = [
          { label: "时间", width: "16%", value: function (row) { return row.created_at; }, render: function (value) { return h("span", { className: "cell-text", text: formatTimestamp(value) }); } },
          { label: "级别", width: "10%", value: function (row) { return firstValue(row, ["level", "status"], "degraded"); }, render: function (value) { return makeBadge(value, statusKind(value) === "neutral" ? "warning" : statusKind(value)); } },
          { label: "工具 / 链路", width: "20%", value: function (row) { return firstValue(row, ["name"], [row.tool_name, row.link_name].filter(Boolean).join(" / ") || "--"); }, render: function (value) { return h("span", { className: "cell-wrap cell-primary", text: value }); } },
          { label: "错误", width: "28%", value: function (row) { return firstValue(row, ["error_message", "error_class", "reason"], "--"); }, render: function (value) { return h("span", { className: "cell-wrap", text: formatScalar(value), attrs: { title: formatScalar(value) } }); } },
          { label: "调用 ID", width: "13%", value: function (row) { return firstValue(row, ["call_id"], "--"); }, render: function (value) { return h("code", { className: "cell-code", text: compactId(value), attrs: { title: formatScalar(value) } }); } },
          { label: "记录 ID", width: "13%", value: function (row) { return firstValue(row, ["record_id", "event_id"], "--"); }, render: function (value) { return h("code", { className: "cell-code", text: compactId(value), attrs: { title: formatScalar(value) } }); } }
        ];
      } else {
        columns = [
          { label: "更新时间", width: "16%", value: function (row) { return firstValue(row, ["updated_at", "created_at"], ""); }, render: function (value) { return h("span", { className: "cell-text", text: formatTimestamp(value) }); } },
          { label: "状态", width: "11%", value: function (row) { return firstValue(row, ["status", "state"], "pending"); }, render: function (value) { return makeBadge(value); } },
          { label: "工具", width: "17%", value: function (row) { return firstValue(row, ["name", "tool_name", "job_type"], "--"); }, render: function (value) { return h("span", { className: "cell-text cell-primary", text: formatScalar(value) }); } },
          { label: "尝试次数", width: "9%", value: function (row) { return firstValue(row, ["attempt_count", "attempts"], 0); }, render: function (value) { return h("span", { className: "mono", text: formatNumber(value) }); } },
          { label: "下次尝试", width: "17%", value: function (row) { return firstValue(row, ["next_attempt_at", "retry_at"], ""); }, render: function (value) { return h("span", { className: "cell-text", text: value ? formatTimestamp(value) : "--" }); } },
          { label: "错误", width: "20%", value: function (row) { return firstValue(row, ["error_message", "error_class"], "--"); }, render: function (value) { return h("span", { className: "cell-wrap", text: formatScalar(value) }); } },
          { label: "发件箱 ID", width: "10%", value: function (row) { return firstValue(row, ["record_id", "outbox_id", "id"], "--"); }, render: function (value) { return h("code", { className: "cell-code", text: compactId(value), attrs: { title: formatScalar(value) } }); } }
        ];
      }
      if (!rows.length) {
        tableHost.replaceChildren(paginatedEmptyState(
          payload,
          "operations",
          state.operationTab === "runtime"
            ? "暂无运行事件"
            : state.operationTab === "degradations"
              ? "暂无降级事件"
              : "暂无发件箱任务",
          state.operationTab === "degradations"
            ? "当前页面没有记录可恢复故障。"
            : state.operationTab === "outbox"
              ? "当前页面没有待处理、重试中或失败的任务。"
              : "当前页面没有运行事件记录。"
        ));
      } else {
        tableHost.replaceChildren(tableNode(columns, rows, {
          payload: payload,
          view: "operations",
          onRow: function (row, index, trigger) {
            var title = state.operationTab === "runtime"
              ? "运行事件详情"
              : state.operationTab === "degradations"
                ? "降级事件详情"
                : "发件箱详情";
            openRecordDetail(title, "运行运维", row, trigger);
          }
        }));
      }
      tabControl.replaceWith(segmented(tabItems, state.operationTab, function (value) {
        state.operationTab = value;
        renderOperations(payload);
      }));
    };
    tabControl = segmented(tabItems, state.operationTab, function (value) {
      state.operationTab = value;
      renderOperations(payload);
    });
    root.appendChild(viewHeader(VIEWS.operations.title, VIEWS.operations.description, [tabControl]));
    root.appendChild(tableHost);
    refs.viewRoot.replaceChildren(root);
    update();
  }

  function trustData(payload) {
    var data = envelopeObject(payload);
    var rows = envelopeRows(payload, ["items", "rows"]);
    var trust = isObject(data.trust)
      ? data.trust
      : isObject(data.defense)
        ? data.defense
        : null;
    var rawIssues = firstValue(data, ["issues", "active_issues"], []);
    var issues = asArray(rawIssues);
    var history = asArray(firstValue(data, ["history", "trust_history"], []));
    if (rows.length && !issues.length && !history.length) {
      issues = rows.filter(function (row) {
        return String(firstValue(row, ["kind", "record_type"], "")).toLowerCase().indexOf("issue") >= 0;
      });
      history = rows.filter(function (row) {
        return String(firstValue(row, ["kind", "record_type"], "")).toLowerCase().indexOf("trust") >= 0;
      });
    }
    return {
      trust: trust,
      issues: issues,
      history: history,
      issuesUnavailable: isObject(rawIssues) && String(rawIssues.status || "").toLowerCase() === "unavailable",
      issuesReason: isObject(rawIssues) ? firstValue(rawIssues, ["reason", "message"], "问题投影不可用") : ""
    };
  }

  function issueStatus(row) {
    return firstValue(row, ["status", "state"], "open");
  }

  function renderTrustIssues(payload) {
    var grouped = trustData(payload);
    var tabControl = segmented([
      { value: "trust", label: "信任" },
      { value: "issues", label: "问题（" + grouped.issues.length + "）" }
    ], state.trustTab, function (value) {
      state.trustTab = value;
      renderTrustIssues(payload);
    });
    var root = h("div", {}, viewHeader(VIEWS["trust-issues"].title, VIEWS["trust-issues"].description, [tabControl]));
    if (state.trustTab === "trust") {
      var trust = grouped.trust || {};
      if (!grouped.trust) {
        root.appendChild(statePanel(
          "empty",
          "信任目标不可用",
          "请求的只读目标没有持久化信任记录。查看此页面不会创建信任，也不会触发信任衰减。"
        ));
        refs.viewRoot.replaceChildren(root);
        return;
      }
      var score = firstValue(trust, ["score", "trust", "trust_score"], "--");
      var tier = firstValue(trust, ["tier", "trust_tier"], "--");
      var target = firstValue(trust, ["target", "agent", "actor"], "--");
      var authorityScope = firstValue(trust, ["authority_scope"], "system_global");
      root.appendChild(h("div", { className: "kpi-grid" }, [
        kpiCard("信任分", score, "系统级持久化分数"),
        kpiCard("等级", statusLabel(tier), "权限级别"),
        kpiCard("目标", target, "信任主体"),
        kpiCard("权限范围", statusLabel(authorityScope), "不归单个项目所有")
      ]));
      if (grouped.history.length) {
        var trustColumns = [
          { label: "时间", width: "20%", value: function (row) { return firstValue(row, ["created_at", "timestamp", "changed_at"], ""); }, render: function (value) { return h("span", { className: "cell-text", text: formatTimestamp(value) }); } },
          { label: "原分数", width: "12%", value: function (row) { return firstValue(row, ["old_score", "previous_score", "from"], "--"); }, render: function (value) { return h("span", { className: "mono", text: formatNumber(value) }); } },
          { label: "新分数", width: "12%", value: function (row) { return firstValue(row, ["new_score", "score", "to"], "--"); }, render: function (value) { return h("span", { className: "mono", text: formatNumber(value) }); } },
          { label: "变化", width: "10%", value: function (row) { return firstValue(row, ["delta", "change"], "--"); }, render: function (value) { return h("span", { className: "mono", text: formatNumber(value) }); } },
          { label: "原因", width: "32%", value: function (row) { return firstValue(row, ["reason", "event", "description"], "--"); }, render: function (value) { return h("span", { className: "cell-wrap", text: formatScalar(value) }); } },
          { label: "执行者", width: "14%", value: function (row) { return firstValue(row, ["actor", "source"], "--"); }, render: function (value) { return h("span", { className: "cell-text", text: formatScalar(value) }); } }
        ];
        root.appendChild(h("section", { className: "section-block" }, [
          sectionHeader("信任历史", "当前页 " + grouped.history.length + " 条记录"),
          tableNode(trustColumns, grouped.history, {
            payload: payload,
            view: "trust-issues",
            onRow: function (row, index, trigger) {
              openRecordDetail("信任事件", "信任历史", row, trigger);
            }
          })
        ]));
      } else {
        root.appendChild(h("section", { className: "section-block" }, statePanel(
          "empty",
          "暂无信任历史",
          "接口返回了当前分数，但没有当前范围内的历史记录。"
        )));
      }
    } else if (grouped.issuesUnavailable) {
      root.appendChild(statePanel(
        "empty",
        "问题投影不可用",
        grouped.issuesReason + "。在问题具备可强制执行的项目归属前，它们会保持隐藏。"
      ));
    } else if (!grouped.issues.length) {
      root.appendChild(statePanel("empty", "暂无活跃问题", "当前范围内没有需要干预的问题。"));
    } else {
      var issueColumns = [
        { label: "更新时间", width: "16%", value: function (row) { return firstValue(row, ["updated_at", "created_at"], ""); }, render: function (value) { return h("span", { className: "cell-text", text: formatTimestamp(value) }); } },
        { label: "状态", width: "11%", value: issueStatus, render: function (value) { return makeBadge(value); } },
        { label: "优先级", width: "10%", value: function (row) { return firstValue(row, ["priority", "severity"], "--"); }, render: function (value) { return makeBadge(value, statusKind(value) === "neutral" ? "warning" : statusKind(value)); } },
        { label: "问题", width: "36%", value: function (row) { return firstValue(row, ["title", "description", "summary"], "--"); }, render: function (value) { return h("span", { className: "cell-wrap cell-primary", text: formatScalar(value) }); } },
        { label: "负责人", width: "12%", value: function (row) { return firstValue(row, ["owner", "assignee"], "--"); }, render: function (value) { return h("span", { className: "cell-text", text: formatScalar(value) }); } },
        { label: "ID", width: "15%", value: function (row) { return firstValue(row, ["issue_id", "id"], "--"); }, render: function (value) { return h("code", { className: "cell-code", text: compactId(value), attrs: { title: formatScalar(value) } }); } }
      ];
      root.appendChild(tableNode(issueColumns, grouped.issues, {
        payload: payload,
        view: "trust-issues",
        onRow: function (row, index, trigger) {
          openRecordDetail("问题详情", "只读问题", row, trigger);
        }
      }));
    }
    refs.viewRoot.replaceChildren(root);
  }

  function flattenConfig(value, prefix, output, depth) {
    if (depth > 7) {
      output.push({ key: prefix, value: "[达到最大深度]", kind: "bounded" });
      return;
    }
    if (prefix && SENSITIVE_KEY.test(prefix.split(".").pop())) {
      output.push({ key: prefix, value: "[已脱敏]", kind: "redacted" });
      return;
    }
    if (isObject(value)) {
      var keys = Object.keys(value).sort();
      if (!keys.length && prefix) {
        output.push({ key: prefix, value: "{}", kind: "object" });
      }
      keys.forEach(function (key) {
        flattenConfig(value[key], prefix ? prefix + "." + key : key, output, depth + 1);
      });
      return;
    }
    if (Array.isArray(value)) {
      output.push({
        key: prefix,
        value: value.every(function (item) { return !isObject(item) && !Array.isArray(item); })
          ? value.join(", ")
          : safeJson(value),
        kind: "array"
      });
      return;
    }
    output.push({ key: prefix || "value", value: formatScalar(value), kind: typeof value });
  }

  function renderConfiguration(payload) {
    var data = envelopeObject(payload);
    var warnings = asArray(data.warnings).map(function (warning) {
      return isObject(warning)
        ? firstValue(warning, ["message", "reason", "code"], safeJson(warning))
        : formatScalar(warning);
    });
    var rows = [];
    flattenConfig(data, "", rows, 0);
    var root = h("div", {}, viewHeader(VIEWS.configuration.title, VIEWS.configuration.description));
    if (!rows.length) {
      root.appendChild(statePanel("empty", "暂无配置投影", "接口没有返回脱敏后的有效配置。"));
      refs.viewRoot.replaceChildren(root);
      return;
    }
    var tableHost = h("div");
    var query = h("input", {
      className: "input",
      attrs: {
        type: "search",
        placeholder: "筛选配置键或值",
        "aria-label": "筛选配置"
      }
    });
    var update = function () {
      var needle = query.value.trim().toLowerCase();
      var visible = rows.filter(function (row) {
        return !needle || (row.key + " " + row.value).toLowerCase().indexOf(needle) >= 0;
      });
      if (!visible.length) {
        tableHost.replaceChildren(statePanel("empty", "没有匹配的配置", "请调整筛选条件查看其他设置。"));
        return;
      }
      tableHost.replaceChildren(tableNode([
        { label: "配置键", width: "42%", value: function (row) { return row.key; }, render: function (value) { return h("code", { className: "cell-code cell-primary", text: value, attrs: { title: value } }); } },
        { label: "有效值", width: "45%", value: function (row) { return row.value; }, render: function (value) { return h("span", { className: "cell-wrap mono", text: value, attrs: { title: value } }); } },
        { label: "类型", width: "13%", value: function (row) { return row.kind; }, render: function (value) { return makeBadge(value, value === "redacted" ? "danger" : "neutral"); } }
      ], visible, {
        onRow: function (row, index, trigger) {
          openRecordDetail("配置值", "脱敏投影", row, trigger);
        },
        rowLabel: function (row) { return "打开配置 " + row.key; }
      }));
    };
    query.addEventListener("input", update);
    root.appendChild(h("div", { className: "toolbar" }, query));
    if (parseBoolean(data.degraded) || warnings.length) {
      root.appendChild(makeNotice(
        "warning",
        "配置已降级。",
        warnings.length
          ? warnings.join(", ")
          : "部分有效运行配置无法生成投影。"
      ));
    }
    root.appendChild(makeNotice("info", "仅供读取。", "浏览器会再次对疑似敏感字段脱敏，作为纵深防御展示规则。"));
    root.appendChild(h("div", { className: "section-block" }, tableHost));
    update();
    refs.viewRoot.replaceChildren(root);
  }

  function renderExplainLanding(payload, params) {
    var requests = envelopeRows(payload, ["requests", "call_spans", "spans"]).filter(function (row) {
      var tool = String(firstValue(row, ["tool_name", "tool"], "")).toLowerCase();
      return tool.indexOf("recall") >= 0 || tool.indexOf("context") >= 0;
    });
    var root = h("div", { className: "explain-page" }, viewHeader(VIEWS.explain.title, VIEWS.explain.description));
    var callInput = h("input", {
      className: "input",
      attrs: {
        type: "text",
        value: params.call_id || "",
        placeholder: "记忆检索或上下文调用 ID",
        "aria-label": "记忆检索或上下文调用 ID"
      }
    });
    var resultHost = h("div", { className: "section-block" });
    var form = h("form", {
      className: "toolbar explain-lookup",
      on: {
        submit: function (event) {
          event.preventDefault();
          var callId = callInput.value.trim();
          if (!callId) {
            toast("请输入记忆检索或上下文调用 ID。", true);
            return;
          }
          replaceRouteParams("explain", { call_id: callId });
          loadExplainResult(callId, resultHost);
        }
      }
    }, [
      callInput,
      textButton("查看解释", null, { type: "submit", primary: true, icon: "file-search" })
    ]);
    root.appendChild(form);
    root.appendChild(resultHost);

    if (requests.length) {
      var columns = [
        { label: "时间", width: "20%", value: function (row) { return firstValue(row, ["started_at", "created_at"], ""); }, render: function (value) { return h("span", { className: "cell-text", text: formatTimestamp(value) }); } },
        { label: "工具", width: "22%", value: function (row) { return firstValue(row, ["tool_name", "tool"], "--"); }, render: function (value) { return h("span", { className: "cell-text cell-primary", text: formatScalar(value) }); } },
        { label: "状态", width: "13%", value: requestStatus, render: function (value, row) { return makeBadge(parseBoolean(row.degraded) ? "degraded" : value); } },
        { label: "耗时", width: "13%", value: function (row) { return firstValue(row, ["duration_ms", "latency_ms"], null); }, render: function (value, row) { return h("span", { className: "cell-text duration-value duration-" + durationKind(row), text: formatDuration(value, row) }); } },
        { label: "调用 ID", width: "32%", value: function (row) { return firstValue(row, ["call_id", "id"], "--"); }, render: function (value) { return h("code", { className: "cell-code", text: compactId(value), attrs: { title: formatScalar(value) } }); } }
      ];
      root.appendChild(h("section", { className: "section-block explain-recent" }, [
        sectionHeader("近期可解释调用", "选择一条记录"),
        tableNode(columns, requests, {
          payload: payload,
          view: "explain",
          onRow: function (row) {
            var callId = firstValue(row, ["call_id", "id"], "");
            callInput.value = callId;
            replaceRouteParams("explain", { call_id: callId });
            loadExplainResult(callId, resultHost);
          },
          rowLabel: function (row) {
            return "查看检索解释 " + compactId(firstValue(row, ["call_id", "id"], ""));
          }
        })
      ]));
    } else {
      root.appendChild(h("section", { className: "section-block explain-recent" }, [
        sectionHeader("近期可解释调用", "浏览请求分页"),
        paginatedEmptyState(
          payload,
          "explain",
          "当前页没有可解释调用",
          "请浏览其他页面，或开启解释采集后运行 memory_recall / context_supply。"
        )
      ]));
    }
    refs.viewRoot.replaceChildren(root);
    if (params.call_id) {
      loadExplainResult(params.call_id, resultHost);
    } else {
      resultHost.replaceChildren(statePanel("empty", "选择一次检索调用", "请选择近期的检索/上下文调用，或输入调用 ID。"));
    }
  }

  function normalizeExplain(payload) {
    var data = envelopeObject(payload);
    if (isObject(data.explain)) {
      return data.explain;
    }
    if (isObject(data.retrieval_explain)) {
      return data.retrieval_explain;
    }
    if (isObject(data.snapshot)) {
      return data.snapshot;
    }
    return data;
  }

  function explainChannels(explain) {
    return normalizedNamedRows(firstValue(explain, ["channels", "channel_states"], []));
  }

  function explainItems(explain) {
    var items = firstValue(explain, ["items", "candidates", "results"], []);
    return asArray(items);
  }

  function explainIsTruncated(explain) {
    var truncated = explain && explain.truncated;
    if (isObject(truncated)) {
      return Object.keys(truncated).some(function (key) {
        return parseBoolean(truncated[key]);
      });
    }
    return parseBoolean(truncated);
  }

  function explainChannelState(row) {
    var channelState = isObject(row && row.state) ? row.state : {};
    if (channelState.available === false) { return "unavailable"; }
    if (channelState.enabled === false) { return "disabled"; }
    if (channelState.planned === false) { return "not planned"; }
    if (channelState.executed === false) { return "not executed"; }
    if (channelState.evidence_only === true) { return "evidence only"; }
    if (channelState.participating === true) { return "participating"; }
    if (channelState.executed === true) { return "executed"; }
    if (channelState.available === true) { return "available"; }
    if (channelState.enabled === true) { return "enabled"; }
    if (channelState.planned === true) { return "planned"; }
    return "--";
  }

  function pipelineStages(explain) {
    var pipeline = firstValue(explain, ["pipeline", "pipeline_stats", "counters"], {});
    if (Array.isArray(pipeline)) {
      return pipeline.filter(function (stage) {
        return !isObject(stage) || firstValue(stage, ["duration_ms", "elapsed_ms", "latency_ms"], null) === null;
      });
    }
    if (isObject(pipeline)) {
      return Object.keys(pipeline).filter(function (name) {
        return name !== "stage_timings";
      }).map(function (name) {
        var value = pipeline[name];
        if (isObject(value)) {
          return Object.assign({ name: name }, value);
        }
        return { name: name, value: value };
      });
    }
    return [];
  }

  function pipelineTimingStages(explain) {
    var pipeline = firstValue(explain, ["pipeline", "pipeline_stats", "counters"], {});
    var raw = isObject(pipeline)
      ? firstValue(pipeline, ["stage_timings"], null)
      : null;
    if (Array.isArray(raw)) {
      return raw.map(function (stage) {
        var value = firstValue(stage || {}, ["duration_ms", "elapsed_ms", "latency_ms", "value"], null);
        var numeric = value === null ? NaN : Number(value);
        return {
          name: firstValue(stage || {}, ["name", "stage"], "stage"),
          duration_ms: Number.isFinite(numeric) && numeric >= 0 ? numeric : null,
          duration_status: Number.isFinite(numeric) && numeric >= 0 ? "measured" : "not_captured"
        };
      });
    }
    if (isObject(raw)) {
      return Object.keys(raw).map(function (name) {
        var rawValue = raw[name];
        var numeric = rawValue === null || rawValue === undefined || rawValue === "" || typeof rawValue === "boolean"
          ? NaN
          : Number(rawValue);
        return {
          name: name,
          duration_ms: Number.isFinite(numeric) && numeric >= 0 ? numeric : null,
          duration_status: Number.isFinite(numeric) && numeric >= 0 ? "measured" : "not_captured"
        };
      });
    }
    if (Array.isArray(pipeline)) {
      return pipeline.filter(function (stage) {
        return isObject(stage) && firstValue(stage, ["duration_ms", "elapsed_ms", "latency_ms"], null) !== null;
      }).map(function (stage) {
        var numeric = Number(firstValue(stage, ["duration_ms", "elapsed_ms", "latency_ms"], NaN));
        return {
          name: firstValue(stage, ["name", "stage"], "stage"),
          duration_ms: Number.isFinite(numeric) && numeric >= 0 ? numeric : null,
          duration_status: Number.isFinite(numeric) && numeric >= 0 ? "measured" : "not_captured"
        };
      });
    }
    return [];
  }

  function callSummaryPanel(call, availability) {
    if (!isObject(call)) {
      return null;
    }
    var durationState = durationKind(call);
    var duration = formatDuration(firstValue(call, ["duration_ms", "latency_ms"], null), call);
    var status = parseBoolean(call.degraded) ? "degraded" : firstValue(call, ["status"], "unknown");
    var facts = [
      { label: "开始时间", value: formatTimestamp(firstValue(call, ["started_at"], "")) },
      { label: "调用耗时", value: duration, className: "duration-value duration-" + durationState },
      { label: "调用范围", value: compactId(firstValue(call, ["request_scope_id", "project_id"], "--")), title: firstValue(call, ["request_scope_id", "project_id"], "--") },
      { label: "调用 ID", value: compactId(firstValue(call, ["call_id"], "--")), title: firstValue(call, ["call_id"], "--") }
    ];
    return h("section", { className: "panel explain-call-summary" }, [
      h("div", { className: "panel-header" }, [
        h("div", { className: "panel-title-group" }, [
          h("span", { className: "panel-kicker", text: "调用概况" }),
          h("h3", { text: firstValue(call, ["tool_name"], "记忆检索") })
        ]),
        h("div", { className: "summary-badges" }, [
          makeBadge(status),
          makeBadge(availability === "available" ? "available" : "unavailable", availability === "available" ? "success" : "warning")
        ])
      ]),
      h("div", { className: "panel-body" }, [
        h("div", { className: "call-summary-grid" }, facts.map(function (fact) {
          return h("div", { className: "call-summary-item" }, [
            h("span", { className: "panel-kicker", text: fact.label }),
            h("strong", { className: fact.className || "", text: fact.value, attrs: { title: fact.title || fact.value } })
          ]);
        })),
        durationState === "missing"
          ? makeNotice("info", "耗时未采集", "这条旧调用没有可计算的起止时间；“未采集”不代表 0 ms。新调用会记录真实耗时。")
          : durationState === "invalid"
            ? makeNotice("warning", "耗时证据无效", "记录中的结束时间早于开始时间，或时间格式无法解析。")
            : null
      ])
    ]);
  }

  function channelIdentity(row) {
    var stateName = explainChannelState(row);
    var className = stateName.replace(/\s+/g, "-");
    return h("div", { className: "channel-identity" }, [
      h("span", { className: "channel-dot channel-dot-" + className, attrs: { "aria-hidden": "true" } }),
      h("div", {}, [
        h("strong", { text: channelLabel(firstValue(row, ["name", "channel"], "--")) }),
        h("span", { text: firstValue(row, ["state.participating"], false) ? "参与融合" : "未参与融合" })
      ])
    ]);
  }

  function channelTopScore(row) {
    var scores = asArray(row && row.items).map(function (item) {
      return Number(firstValue(item, ["score"], NaN));
    }).filter(Number.isFinite);
    return scores.length ? Math.max.apply(Math, scores) : null;
  }

  function explainChunkEvidence(row) {
    if (isObject(row && row.chunk_evidence)) {
      return row.chunk_evidence;
    }
    if (firstValue(row || {}, ["chunk_id"], "")) {
      return Object.assign({ status: "matched" }, row);
    }
    return { status: "not_recorded" };
  }

  function explainChunkLabel(row) {
    var evidence = explainChunkEvidence(row);
    var status = String(firstValue(evidence, ["status"], "not_recorded"));
    if (status === "matched") {
      return compactId(firstValue(evidence, ["chunk_id"], "--"));
    }
    if (status === "available_not_recorded") {
      return formatNumber(firstValue(evidence, ["available_count"], asArray(evidence.anchors).length)) + " 个清单锚点，无精确命中";
    }
    return "父记忆聚合候选";
  }

  function explainChunkSpan(row) {
    var evidence = explainChunkEvidence(row);
    if (String(firstValue(evidence, ["status"], "")) !== "matched") {
      return "--";
    }
    return chunkSpanLabel(evidence);
  }

  function explainChannelScores(row) {
    var scores = isObject(row && row.channel_scores) ? row.channel_scores : {};
    var names = Object.keys(scores).sort();
    if (!names.length) {
      return "--";
    }
    return names.map(function (name) {
      return channelLabel(name) + " " + formatNumber(scores[name]);
    }).join(" · ");
  }

  function explainChunkEvidencePanel(items) {
    var withEvidence = items.filter(function (item) {
      return String(firstValue(explainChunkEvidence(item), ["status"], "not_recorded")) !== "not_recorded";
    });
    if (!withEvidence.length) {
      return makeNotice("info", "本次没有精确切片命中证据", "当前向量索引按父记忆聚合，检索快照只记录父记忆候选。结构切片清单用于检查确定性边界，不会据此推断命中了某个切片。");
    }
    var matched = withEvidence.filter(function (item) {
      return String(firstValue(explainChunkEvidence(item), ["status"], "")) === "matched";
    }).length;
    return makeNotice(
      matched ? "success" : "info",
      matched ? "快照明确记录了 " + matched + " 个切片 ID" : "候选记忆已有结构切片清单",
      matched ? "下表只复述快照明确携带的切片 ID、标题路径与来源跨度。" : "当前快照未记录 chunk_id；所列锚点仅来自父记忆的结构切片清单，不推断精确命中位置。"
    );
  }

  function loadExplainResult(callId, host) {
    state.explainSequence += 1;
    var sequence = state.explainSequence;
    host.replaceChildren(loadingState());
    apiRequest("/retrieval-explain", { call_id: callId }, null).then(function (payload) {
      if (sequence !== state.explainSequence || state.currentView !== "explain") {
        return;
      }
      var explain = normalizeExplain(payload);
      var channels = explainChannels(explain);
      var items = explainItems(explain);
      var pipeline = pipelineStages(explain);
      var stageTimings = pipelineTimingStages(explain);
      var measuredTimingCount = stageTimings.filter(function (stage) {
        return durationKind(stage) === "measured";
      }).length;
      var schema = firstValue(explain, ["schema_version", "schema"], "");
      var truncated = explainIsTruncated(explain);
      if (String(explain.availability || "").toLowerCase() === "unavailable") {
        host.replaceChildren(h("section", { className: "explain-result" }, [
          sectionHeader("检索证据", "当前调用没有可用快照"),
          callSummaryPanel(explain.call, "unavailable"),
          statePanel(
            "empty",
            "解释不可用",
            explainReasonLabel(firstValue(explain, ["reason"], "snapshot_not_captured"))
          )
        ]));
        return;
      }
      if (!schema && !channels.length && !items.length && !pipeline.length && !stageTimings.length) {
        host.replaceChildren(h("section", { className: "explain-result" }, [
          sectionHeader("检索证据", "当前调用没有可用快照"),
          callSummaryPanel(explain.call, "unavailable"),
          statePanel("empty", "解释不可用", "旧调用没有保存受限的 retrieval_explain_v1 快照。")
        ]));
        return;
      }

      var headerMetrics = h("div", { className: "pipeline explain-metric-strip" }, [
        { name: "快照架构", value: schema || "retrieval_explain_v1" },
        { name: "检索模式", value: statusLabel(firstValue(explain, ["pipeline.retrieval_mode", "retrieval_mode"], "--")) },
        { name: "融合策略", value: statusLabel(firstValue(explain, ["pipeline.fusion_policy", "fusion_policy", "pipeline.fusion_mode"], "--")) },
        { name: "通道 / 候选", value: channels.length + " / " + items.length },
        { name: "阶段耗时", value: stageTimings.length ? "已采集 " + measuredTimingCount + " / " + stageTimings.length : "未采集" },
        { name: "快照状态", value: truncated ? "已截断" : "完整" }
      ].map(function (stage) {
        return h("div", { className: "pipeline-stage explain-metric" }, [
          h("div", { className: "pipeline-stage-name", text: stage.name }),
          h("div", { className: "pipeline-stage-value", text: formatScalar(stage.value) })
        ]);
      }));
      var content = [
        sectionHeader("检索证据：" + compactId(callId), truncated ? "受限 / 已截断" : "受限快照"),
        callSummaryPanel(explain.call, "available"),
        headerMetrics
      ];

      if (pipeline.length) {
        content.push(h("section", { className: "section-block explain-pipeline-section" }, [
          sectionHeader("处理管线指标", pipeline.length + " 项"),
          h("div", { className: "pipeline explain-pipeline" }, pipeline.map(function (stage) {
            var name = firstValue(stage, ["name", "stage"], "stage");
            return h("div", { className: "pipeline-stage" }, [
              h("div", { className: "pipeline-stage-name", text: pipelineLabel(name) }),
              h("div", { className: "pipeline-stage-value", text: formatScalar(firstValue(stage, ["value", "count", "output_count", "status"], "--")) })
            ]);
          }))
        ]));
      }

      content.push(h("section", { className: "section-block explain-timing-section" }, [
        sectionHeader("阶段耗时", stageTimings.length ? stageTimings.length + " 个阶段" : "未采集"),
        stageTimings.length
          ? h("div", { className: "pipeline explain-pipeline explain-timing" }, stageTimings.map(function (stage) {
              return h("div", { className: "pipeline-stage" }, [
                h("div", { className: "pipeline-stage-name", text: pipelineLabel(stage.name) }),
                h("div", {
                  className: "pipeline-stage-value duration-value duration-" + durationKind(stage),
                  text: formatDuration(stage.duration_ms, stage)
                })
              ]);
            }))
          : makeNotice("info", "阶段耗时未采集", "此解释快照没有阶段计时数据；未采集不代表 0 ms。")
      ]));

      if (channels.length) {
        content.push(h("section", { className: "section-block explain-channel-section" }, [
          sectionHeader("通道", channels.length + " 个通道"),
          tableNode([
            { label: "通道", width: "25%", value: function (row) { return firstValue(row, ["name", "channel"], "--"); }, render: function (value, row) { return channelIdentity(row); } },
            { label: "状态", width: "16%", value: explainChannelState, render: function (value) { return makeBadge(value, value === "unavailable" ? "warning" : statusKind(value)); } },
            { label: "候选数", width: "13%", value: function (row) { return asArray(row.items).length; }, render: function (value) { return h("span", { className: "mono", text: formatNumber(value) }); } },
            { label: "结果数", width: "13%", value: function (row) { return firstValue(row, ["state.result_count"], asArray(row.items).length); }, render: function (value) { return h("span", { className: "mono", text: formatNumber(value) }); } },
            { label: "最高分", width: "13%", value: channelTopScore, render: function (value) { return h("span", { className: "mono", text: formatNumber(value) }); } },
            { label: "原因", width: "20%", value: function (row) { return firstValue(row, ["state.reason"], "--"); }, render: function (value) { return h("span", { className: "cell-wrap", text: explainReasonLabel(value), attrs: { title: formatScalar(value) } }); } }
          ], channels, {
            onRow: function (row, index, trigger) {
              openRecordDetail("通道详情", "检索解释", row, trigger);
            }
          })
        ]));
      }

      if (items.length) {
        content.push(h("section", { className: "section-block explain-candidate-section" }, [
          sectionHeader("候选决策", items.length + " 条受限记录"),
          explainChunkEvidencePanel(items),
          tableNode([
            { label: "记忆 ID", width: "16%", value: function (row) { return firstValue(row, ["memory_id", "parent_memory_id", "id"], "--"); }, render: function (value) { return h("code", { className: "cell-code", text: compactId(value), attrs: { title: formatScalar(value) } }); } },
            { label: "来源", width: "11%", value: function (row) { return firstValue(row, ["retrieval_source"], "--"); }, render: function (value) { return h("span", { className: "cell-text", text: channelLabel(value) }); } },
            { label: "层级", width: "10%", value: function (row) { return firstValue(row, ["layer", "tier"], "--"); }, render: function (value) { return makeBadge(value, "info"); } },
            { label: "切片清单证据", width: "17%", value: explainChunkLabel, render: function (value, row) { var status = firstValue(explainChunkEvidence(row), ["status"], "not_recorded"); return h("span", { className: "cell-wrap", text: value, attrs: { title: statusLabel(status) } }); } },
            { label: "来源跨度", width: "10%", value: explainChunkSpan, render: function (value) { return h("span", { className: "mono", text: value }); } },
            { label: "通道分数", width: "16%", value: explainChannelScores, render: function (value) { return h("span", { className: "cell-wrap mono", text: value }); } },
            { label: "排名", width: "7%", value: function (row) { return firstValue(row, ["rank", "final_rank"], "--"); }, render: function (value) { return h("span", { className: "mono", text: formatNumber(value) }); } },
            {
              label: "分数", width: "13%",
              value: function (row) { return firstValue(row, ["final_score", "score", "initial_score"], "--"); },
              render: function (value) {
                var number = Number(value);
                var width = Number.isFinite(number) ? Math.max(0, Math.min(100, number <= 1 ? number * 100 : number)) : 0;
                return h("div", {}, [
                  h("span", { className: "mono", text: formatNumber(value) }),
                  h("meter", {
                    className: "score-meter",
                    attrs: { min: "0", max: "100", value: String(width) }
                  })
                ]);
              }
            },
            { label: "决策", width: "10%", value: function (row) { return firstValue(row, ["gate_decision", "filter_decision"], "--"); }, render: function (value) { return makeBadge(value); } },
            { label: "原因", width: "15%", value: function (row) { return firstValue(row, ["gate_reason", "filter_reason"], "--"); }, render: function (value) { return h("span", { className: "cell-wrap", text: formatScalar(value) }); } }
          ], items, {
            onRow: function (row, index, trigger) {
              openRecordDetail("候选决策", "检索解释", row, trigger);
            }
          })
        ]));
      }
      host.replaceChildren(h("section", { className: "explain-result" }, content));
    }).catch(function (error) {
      if (sequence !== state.explainSequence) {
        return;
      }
      var legacy = error.status === 404;
      host.replaceChildren(h("section", { className: "explain-result" }, [
        sectionHeader("检索证据", legacy ? "当前调用不可用" : "请求失败"),
        statePanel(
          legacy ? "empty" : "error",
          legacy ? "解释不可用" : "无法加载解释",
          legacy ? "检索解释功能可能未启用；也可能是此调用不在当前范围内，或没有保存解释快照。可在“有效配置”中核对开关。" : error.message,
          legacy ? null : function () { loadExplainResult(callId, host); }
        )
      ]));
    });
  }

  function renderPayload(view, payload, params) {
    switch (view) {
      case "overview":
        renderOverview(payload);
        break;
      case "requests":
        renderRequests(payload);
        break;
      case "memories":
        renderMemories(payload);
        break;
      case "lineage":
        renderLineage(payload, params);
        break;
      case "synthesis":
        renderSynthesis(payload);
        break;
      case "operations":
        renderOperations(payload);
        break;
      case "trust-issues":
        renderTrustIssues(payload);
        break;
      case "explain":
        renderExplainLanding(payload, params);
        break;
      case "configuration":
        renderConfiguration(payload);
        break;
      default:
        renderOverview(payload);
    }
  }

  function loadCurrentView() {
    var view = state.currentView;
    var definition = VIEWS[view];
    if (!definition) {
      navigate("overview");
      return;
    }
    state.requestSequence += 1;
    var sequence = state.requestSequence;
    if (state.controller) {
      state.controller.abort();
    }
    state.controller = new AbortController();
    showLoading();
    renderNotices(null);
    apiRequest(definition.endpoint, pageParams(view), state.controller.signal).then(function (payload) {
      if (sequence !== state.requestSequence) {
        return;
      }
      state.payloads[view] = payload;
      renderNotices(payload);
      updateEnvelopeChrome(payload);
      renderPayload(view, payload, state.routeParams);
      refs.viewRoot.setAttribute("aria-busy", "false");
    }).catch(function (error) {
      if (error && error.name === "AbortError") {
        return;
      }
      if (sequence !== state.requestSequence) {
        return;
      }
      showError(error);
      renderNotices(error && error.payload ? error.payload : null);
    }).finally(function () {
      if (sequence === state.requestSequence) {
        refs.refreshButton.classList.remove("is-loading");
        refs.refreshButton.disabled = false;
        refs.viewRoot.setAttribute("aria-busy", "false");
      }
    });
  }

  function parseRoute() {
    var raw = window.location.hash.replace(/^#\/?/, "");
    var parts = raw.split("?");
    var view = parts[0] || "overview";
    if (!VIEWS[view]) {
      view = "overview";
    }
    var params = {};
    var query = new URLSearchParams(parts[1] || "");
    query.forEach(function (value, key) {
      params[key] = value;
    });
    return { view: view, params: params };
  }

  function navigate(view, params) {
    var query = new URLSearchParams(params || {});
    var suffix = query.toString() ? "?" + query.toString() : "";
    var target = "#/" + view + suffix;
    if (window.location.hash === target) {
      handleRoute();
    } else {
      window.location.hash = target;
    }
  }

  function replaceRouteParams(view, params) {
    var query = new URLSearchParams(params || {});
    var suffix = query.toString() ? "?" + query.toString() : "";
    window.history.replaceState(null, "", "#/" + view + suffix);
    state.routeParams = Object.assign({}, params);
  }

  function applyFeatureGates() {
    var explainLink = refs.mainNav.querySelector('[data-feature="retrieval-explain"]');
    if (!explainLink) {
      return;
    }
    var enabled = state.featureFlags.retrievalExplain;
    var hidden = enabled === false;
    explainLink.hidden = hidden;
    explainLink.setAttribute("aria-hidden", String(hidden));
    explainLink.tabIndex = hidden ? -1 : 0;
    if (hidden && state.currentView === "explain") {
      navigate("overview");
    }
  }

  function loadFeatureFlags() {
    return apiRequest("/configuration", {}, null).then(function (payload) {
      var data = envelopeObject(payload);
      var dashboard = isObject(data.dashboard) ? data.dashboard : {};
      state.featureFlags.retrievalExplain = dashboard.retrieval_explain_enabled === true;
      applyFeatureGates();
    }).catch(function () {
      // Keep the link visible when configuration cannot be read. The route
      // itself remains the final authority and will return a diagnosed 404.
      state.featureFlags.retrievalExplain = null;
    });
  }

  function updateNavigation() {
    var definition = VIEWS[state.currentView];
    refs.topbarTitle.textContent = definition.title;
    document.title = definition.title + " | Plastic Promise 管理控制台";
    refs.mainNav.querySelectorAll("[data-view]").forEach(function (link) {
      var active = link.dataset.view === state.currentView;
      link.classList.toggle("is-active", active);
      if (active) {
        link.setAttribute("aria-current", "page");
      } else {
        link.removeAttribute("aria-current");
      }
      var label = link.querySelector("span");
      if (label) {
        link.title = label.textContent;
      }
    });
  }

  function syncMobileNavigation(open) {
    var mobile = window.innerWidth <= 760;
    var expanded = mobile && Boolean(open);
    var hidden = mobile && !expanded;
    refs.shell.classList.toggle("is-mobile-open", expanded);
    refs.mobileToggle.setAttribute("aria-expanded", String(expanded));
    refs.mobileToggle.title = expanded ? "关闭导航" : "打开导航";
    refs.mobileToggle.setAttribute("aria-label", refs.mobileToggle.title);
    refs.sidebar.toggleAttribute("inert", hidden);
    if (hidden) {
      refs.sidebar.setAttribute("aria-hidden", "true");
    } else {
      refs.sidebar.removeAttribute("aria-hidden");
    }
  }

  function closeMobileNavigation() {
    syncMobileNavigation(false);
  }

  function handleRoute() {
    var route = parseRoute();
    if (route.view === "explain" && state.featureFlags.retrievalExplain === false) {
      navigate("overview");
      return;
    }
    var changedView = route.view !== state.currentView;
    state.currentView = route.view;
    state.routeParams = route.params;
    if (changedView) {
      closeDialog();
    }
    closeMobileNavigation();
    updateNavigation();
    loadCurrentView();
    refs.viewRoot.focus({ preventScroll: true });
  }

  function initializeSidebar() {
    var collapsed = false;
    try {
      collapsed = window.localStorage.getItem("pp_dashboard_sidebar_collapsed") === "true";
    } catch (error) {
      collapsed = false;
    }
    refs.shell.classList.toggle("is-collapsed", collapsed);
    refs.sidebarToggle.title = collapsed ? "展开侧栏" : "收起侧栏";
    refs.sidebarToggle.setAttribute("aria-label", refs.sidebarToggle.title);

    refs.sidebarToggle.addEventListener("click", function () {
      var next = !refs.shell.classList.contains("is-collapsed");
      refs.shell.classList.toggle("is-collapsed", next);
      refs.sidebarToggle.title = next ? "展开侧栏" : "收起侧栏";
      refs.sidebarToggle.setAttribute("aria-label", refs.sidebarToggle.title);
      try {
        window.localStorage.setItem("pp_dashboard_sidebar_collapsed", String(next));
      } catch (error) {
        return;
      }
    });
    refs.mobileToggle.addEventListener("click", function () {
      var open = !refs.shell.classList.contains("is-mobile-open");
      syncMobileNavigation(open);
    });
    refs.mobileBackdrop.addEventListener("click", closeMobileNavigation);
    syncMobileNavigation(false);
  }

  function initialize() {
    initializeSidebar();
    refs.refreshButton.addEventListener("click", function () {
      loadCurrentView();
    });
    refs.detailClose.addEventListener("click", closeDialog);
    refs.detailDialog.addEventListener("click", function (event) {
      if (event.target === refs.detailDialog) {
        closeDialog();
      }
    });
    refs.detailDialog.addEventListener("close", function () {
      if (state.detailFocus && typeof state.detailFocus.focus === "function") {
        state.detailFocus.focus();
      }
      state.detailFocus = null;
    });
    refs.mainNav.addEventListener("click", function () {
      closeMobileNavigation();
    });
    window.addEventListener("hashchange", handleRoute);
    window.addEventListener("resize", function () {
      syncMobileNavigation(refs.shell.classList.contains("is-mobile-open"));
    });
    if (!window.location.hash) {
      window.history.replaceState(null, "", "#/overview");
    }
    loadFeatureFlags().then(function () {
      var route = parseRoute();
      state.currentView = route.view;
      state.routeParams = route.params;
      applyFeatureGates();
      updateNavigation();
      loadCurrentView();
    });
  }

  initialize();
}());
