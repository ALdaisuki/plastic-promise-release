# P2 Batch 2: Defense + Cognition — soul_enforcer + soul_audit + soul_scarf + soul_proprioception

> Date: 2026-06-28 | Scope: 4 modules, core methods only, parallel-safe

## Core Methods

### soul_enforcer.py
| Method | Logic |
|--------|-------|
| TrustManager.__init__ | initial_trust, history list |
| TrustManager.boost(delta, reason) | trust += delta, clamp [MIN,MAX], log history |
| TrustManager.decay(delta, reason) | trust -= delta, clamp |
| TrustManager.get() | return current trust |
| TrustManager.tier | high/medium/low/critical based on thresholds |
| SoulEnforcer.pre_check(action, type) | L0: pattern match dangerous ops; L1: trust check |
| SoulEnforcer.get_defense_status() | Return 3-layer status from DEFENSE_LAYERS |

### soul_audit.py
| Method | Logic |
|--------|-------|
| AuditReport.to_dict/to_json/to_markdown | Serialize dimensions + findings + overall_score |
| SoulAuditor.run_audit(scope, time_range) | 7-dim weighted scoring, flag <0.60 |
| SoulAuditor.pre_check(action, type) | Quick compliance check, alert if <50% |
| SoulAuditor.get_report() | Return latest report in requested format |

### soul_scarf.py
| Method | Logic |
|--------|-------|
| SCARFReflector.reflect(context, dimensions) | 5-dim keyword + heuristic scoring per dimension |
| SCARFReflector.get_status_summary() | overall_score + weakest/strongest dimension |

### soul_proprioception.py
| Method | Logic |
|--------|-------|
| ProprioceptionManager.check_inertia(tasks) | Compute similarity between recent tasks, detect inertia |
| ProprioceptionManager.record_task(task) | Push to recent_tasks queue, trim to window |
| ProprioceptionManager.get_pattern_analysis() | Analyze dominant patterns |

## Out of Scope
- TrustManager.history() detailed query
- SoulEnforcer.log_violation() persistence
- SoulEnforcer.get_violation_stats()
- SoulAuditor.get_compliance_rate/get_alert_status
- SCARFReflector.compare_with_history()
- Auxiliary methods in all modules
