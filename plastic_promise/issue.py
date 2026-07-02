"""Issue 生命周期 + 依赖关系管理 — 约定→任务→追踪

Serves 实践层: 每个约定对应可追踪的 Issue，Issue 之间有依赖关系。
"""

import datetime
import uuid
from typing import Any, Dict, List, Optional, Set


class IssueManager:
    """Issue 生命周期管理器 — 约定工程的任务追踪引擎。

    State machine: open → in_progress → review → resolved → closed
    Dependency check: prevents closing issues that block unresolved ones.
    """

    VALID_STATES = {"open", "in_progress", "review", "resolved", "closed"}
    VALID_TRANSITIONS = {
        "open": {"in_progress"},
        "in_progress": {"review", "resolved", "open"},
        "review": {"resolved", "in_progress"},
        "resolved": {"closed", "in_progress"},
        "closed": set(),
    }

    def __init__(self):
        self._issues: Dict[str, dict] = {}

    # ================================================================
    # CRUD
    # ================================================================

    def create(
        self,
        title: str,
        description: str = "",
        principle_id: int = None,
        memory_ids: List[str] = None,
        blocks: List[str] = None,
        blocked_by: List[str] = None,
        owner: str = "",
    ) -> str:
        """Create a new Issue and return its ID.

        Args:
            title: Short descriptive title.
            description: Detailed description.
            principle_id: Associated principle (1-12), if any.
            memory_ids: Related memory IDs.
            blocks: Issue IDs this issue blocks.
            blocked_by: Issue IDs that block this issue.
            owner: Agent owner.
        """
        iid = f"issue_{uuid.uuid4().hex[:12]}"
        now = datetime.datetime.now().isoformat()
        self._issues[iid] = {
            "id": iid,
            "title": title,
            "description": description,
            "principle_id": principle_id,
            "state": "open",
            "memory_ids": memory_ids or [],
            "blocks": blocks or [],
            "blocked_by": blocked_by or [],
            "owner": owner,
            "created_at": now,
            "updated_at": now,
            "history": [{"state": "open", "timestamp": now, "reason": "created"}],
        }
        return iid

    def transition(self, iid: str, new_state: str, reason: str = "") -> dict:
        """Transition an Issue to a new state.

        Returns:
            {"success": bool, "message": str, "issue": dict|None}
        """
        issue = self._issues.get(iid)
        if issue is None:
            return {"success": False, "message": f"Issue {iid} not found", "issue": None}

        current = issue["state"]
        if new_state not in self.VALID_STATES:
            return {"success": False, "message": f"Invalid state: {new_state}", "issue": issue}

        if new_state not in self.VALID_TRANSITIONS.get(current, set()):
            return {
                "success": False,
                "message": f"Cannot transition {current} → {new_state}",
                "issue": issue,
            }

        # Dependency check: cannot resolve/close if blocked issues are open
        if new_state in ("resolved", "closed"):
            for bid in issue.get("blocked_by", []):
                blocked_issue = self._issues.get(bid)
                if blocked_issue and blocked_issue["state"] not in ("resolved", "closed"):
                    return {
                        "success": False,
                        "message": f"Cannot {new_state}: blocked by {bid} (state={blocked_issue['state']})",
                        "issue": issue,
                    }

        now = datetime.datetime.now().isoformat()
        issue["state"] = new_state
        issue["updated_at"] = now
        issue["history"].append({"state": new_state, "timestamp": now, "reason": reason})
        result = {"success": True, "message": f"{current} → {new_state}", "issue": issue}
        # Push SSE notification for real-time multi-agent awareness
        try:
            from plastic_promise.mcp.server import notify_issue_change

            notify_issue_change(
                {
                    "type": "issue_transition",
                    "issue_id": iid,
                    "title": issue.get("title", ""),
                    "owner": issue.get("owner", ""),
                    "from_state": current,
                    "to_state": new_state,
                    "reason": reason,
                    "timestamp": now,
                    "summary": f"[{issue.get('owner', '')}] {current}→{new_state}: {reason[:100]}",
                }
            )
        except Exception:
            pass  # notification is best-effort; don't block transition
        return result

    def get(self, iid: str) -> dict | None:
        return self._issues.get(iid)

    def list(self, state: str = None, owner: str = None) -> List[dict]:
        """List issues, optionally filtered by state or owner."""
        result = list(self._issues.values())
        if state:
            result = [i for i in result if i["state"] == state]
        if owner:
            result = [i for i in result if i["owner"] == owner]
        return sorted(result, key=lambda i: i["created_at"], reverse=True)

    def stats(self) -> dict:
        """Return issue statistics by state."""
        by_state = {s: 0 for s in self.VALID_STATES}
        for i in self._issues.values():
            by_state[i["state"]] = by_state.get(i["state"], 0) + 1
        return {
            "total": len(self._issues),
            "by_state": by_state,
        }

    # ================================================================
    # 依赖关系管理
    # ================================================================

    def add_block(self, iid: str, blocks_id: str) -> dict:
        """Declare that iid blocks blocks_id."""
        issue = self._issues.get(iid)
        blocked = self._issues.get(blocks_id)
        if not issue:
            return {"success": False, "message": f"Issue {iid} not found"}
        if not blocked:
            return {"success": False, "message": f"Issue {blocks_id} not found"}

        if blocks_id not in issue.get("blocks", []):
            issue.setdefault("blocks", []).append(blocks_id)
        if iid not in blocked.setdefault("blocked_by", []):
            blocked["blocked_by"].append(iid)

        # Cycle detection
        if self._has_cycle(iid):
            # Revert
            issue["blocks"].remove(blocks_id)
            blocked["blocked_by"].remove(iid)
            return {
                "success": False,
                "message": "Adding this block would create a dependency cycle",
            }

        return {"success": True, "message": f"{iid} now blocks {blocks_id}"}

    def _has_cycle(self, start: str) -> bool:
        """DFS cycle detection in the dependency graph."""
        visited: Set[str] = set()
        path: Set[str] = set()

        def dfs(node: str) -> bool:
            visited.add(node)
            path.add(node)
            for dep in self._issues.get(node, {}).get("blocks", []):
                if dep not in visited:
                    if dfs(dep):
                        return True
                elif dep in path:
                    return True
            path.discard(node)
            return False

        return dfs(start)

    def get_chain(self, iid: str) -> List[dict]:
        """Return the full dependency chain (what this blocks and what blocks this)."""
        issue = self._issues.get(iid)
        if not issue:
            return []
        chain = [{"id": iid, "title": issue["title"], "state": issue["state"]}]
        for bid in issue.get("blocks", []):
            b = self._issues.get(bid)
            if b:
                chain.append(
                    {"id": bid, "title": b["title"], "state": b["state"], "relation": "blocks"}
                )
        for bid in issue.get("blocked_by", []):
            b = self._issues.get(bid)
            if b:
                chain.append(
                    {"id": bid, "title": b["title"], "state": b["state"], "relation": "blocked_by"}
                )
        return chain
