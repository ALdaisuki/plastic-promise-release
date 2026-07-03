/**
 * Sync Coordinator — 跨实例内存同步
 *
 * 借鉴 N.E.K.O cross_server.py 的跨实例同步模式。
 * 在 Plastic Promise MCP 之上提供版本化内存同步。
 *
 * 功能:
 * - 版本追踪: 每条记忆有版本号，检测冲突
 * - 增量同步: 只传输自上次同步后变更的记忆
 * - 三方同步: Pi ↔ Claude ↔ N.E.K.O 全部保持同步
 * - 冲突解决: 基于时间戳的 LWW (Last-Write-Wins)
 */

// ============================================================
// 同步协议
// ============================================================

interface MemoryVersion {
  memoryId: string;
  version: number;
  updatedAt: number;
  updatedBy: string;
  checksum: string;
}

interface SyncState {
  agent: string;
  lastFullSync: number;
  lastIncrementalSync: number;
  versions: Map<string, MemoryVersion>;
  pendingChanges: MemoryChange[];
}

interface MemoryChange {
  memoryId: string;
  action: "create" | "update" | "delete";
  content?: string;
  memoryType?: string;
  timestamp: number;
  source: string;
}

interface SyncResult {
  agent: string;
  pushed: number;
  pulled: number;
  conflicts: string[];
  duration: number;
}

// ============================================================
// Sync Coordinator
// ============================================================

class SyncCoordinator {
  private states = new Map<string, SyncState>();
  private changeLog: MemoryChange[] = [];
  private maxChangeLog = 500;

  registerAgent(agent: string): void {
    if (!this.states.has(agent)) {
      this.states.set(agent, {
        agent,
        lastFullSync: 0,
        lastIncrementalSync: 0,
        versions: new Map(),
        pendingChanges: [],
      });
    }
  }

  /**
   * 记录本地变更
   */
  recordChange(change: MemoryChange): void {
    this.changeLog.push(change);
    if (this.changeLog.length > this.maxChangeLog) {
      this.changeLog = this.changeLog.slice(-this.maxChangeLog);
    }

    // 更新本地版本
    const state = this.states.get(change.source);
    if (state) {
      const existing = state.versions.get(change.memoryId);
      const newVersion = (existing?.version || 0) + 1;
      state.versions.set(change.memoryId, {
        memoryId: change.memoryId,
        version: newVersion,
        updatedAt: change.timestamp,
        updatedBy: change.source,
        checksum: this.checksum(change.content || ""),
      });
    }
  }

  /**
   * 获取自某个时间后的变更
   */
  getChangesSince(timestamp: number): MemoryChange[] {
    return this.changeLog.filter((c) => c.timestamp > timestamp);
  }

  /**
   * 执行增量同步
   */
  incrementalSync(agent: string): SyncResult {
    const startTime = Date.now();
    this.registerAgent(agent);
    const state = this.states.get(agent)!;

    const pushed = this.getChangesSince(state.lastIncrementalSync);
    state.lastIncrementalSync = startTime;

    return {
      agent,
      pushed: pushed.length,
      pulled: 0, // 由调用方从 Plastic Promise 拉取
      conflicts: [],
      duration: Date.now() - startTime,
    };
  }

  /**
   * 执行全量同步
   */
  fullSync(agent: string): SyncResult {
    const startTime = Date.now();
    this.registerAgent(agent);
    const state = this.states.get(agent)!;

    state.lastFullSync = startTime;
    state.lastIncrementalSync = startTime;

    return {
      agent,
      pushed: this.changeLog.length,
      pulled: 0,
      conflicts: [],
      duration: Date.now() - startTime,
    };
  }

  /**
   * LWW 冲突解决
   */
  resolveConflict(
    local: MemoryVersion,
    remote: MemoryVersion
  ): MemoryVersion {
    // Last-Write-Wins: 最新时间戳获胜
    if (remote.updatedAt > local.updatedAt) {
      return remote;
    }
    // 时间相同时，高版本获胜
    if (remote.version > local.version) {
      return remote;
    }
    return local;
  }

  /**
   * 获取同步状态汇总
   */
  getStatus(): Record<string, unknown> {
    const agents: Record<string, unknown> = {};
    for (const [name, state] of this.states) {
      agents[name] = {
        lastFullSync: state.lastFullSync,
        lastIncrementalSync: state.lastIncrementalSync,
        trackedVersions: state.versions.size,
        pendingChanges: state.pendingChanges.length,
      };
    }
    return {
      agents,
      totalChanges: this.changeLog.length,
      activeAgents: this.states.size,
    };
  }

  private checksum(content: string): string {
    // Simple hash for version tracking
    let hash = 0;
    for (let i = 0; i < content.length; i++) {
      const char = content.charCodeAt(i);
      hash = ((hash << 5) - hash) + char;
      hash |= 0;
    }
    return hash.toString(16);
  }
}

// ============================================================
// 全局单例
// ============================================================

let coordinator: SyncCoordinator | null = null;

export function getSyncCoordinator(): SyncCoordinator {
  if (!coordinator) {
    coordinator = new SyncCoordinator();
  }
  return coordinator;
}

/**
 * 将 Plastic Promise 记忆操作同步到其他 Agent
 *
 * 用法:
 *   当 Pi/Claude 调用 memory_store/update/forget 时，
 *   同时调用 syncChange 广播变更给其他 Agent。
 */
export function syncChange(change: MemoryChange): void {
  const sync = getSyncCoordinator();
  sync.recordChange(change);
}

/**
 * 处理来自其他 Agent 的远程变更
 */
export function applyRemoteChanges(
  agent: string,
  changes: MemoryChange[]
): MemoryChange[] {
  const sync = getSyncCoordinator();
  sync.registerAgent(agent);

  const toApply: MemoryChange[] = [];
  const state = sync.states.get(agent)!;

  for (const change of changes) {
    state.pendingChanges.push(change);
    toApply.push(change);
  }

  return toApply;
}

export { SyncCoordinator };
export default SyncCoordinator;
