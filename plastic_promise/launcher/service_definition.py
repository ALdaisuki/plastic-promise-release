"""Service definition dataclasses for the One-Click Launcher."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ServiceStatus(Enum):
    PENDING = "pending"
    STARTING = "starting"
    HEALTHY = "healthy"
    FAILED = "failed"
    UNRECOVERABLE = "unrecoverable"
    STOPPED = "stopped"


@dataclass
class RestartPolicy:
    max_retries: int = 5
    window_seconds: float = 60.0
    backoff_base: float = 1.0
    backoff_multiplier: float = 2.0
    max_backoff: float = 30.0


@dataclass
class ServiceDefinition:
    name: str
    command: list[str]
    health_url: Optional[str] = None
    startup_timeout: float = 30.0
    health_check_interval: float = 5.0
    depends_on: list[str] = field(default_factory=list)
    pre_start: list[str] = field(default_factory=list)
    restart_policy: RestartPolicy = field(default_factory=RestartPolicy)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = "."
