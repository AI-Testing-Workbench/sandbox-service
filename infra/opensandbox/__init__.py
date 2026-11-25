"""
OpenSandbox 集成层（v4 §8）。
"""

from __future__ import annotations

from infra.opensandbox.client import (
    OpenSandboxClient,
    OpenSandboxError,
    SandboxFailedError,
    SandboxNotFoundError,
    rewrite_loopback_endpoint,
)
from infra.opensandbox.types import (
    CreatedSandbox,
    SandboxEndpoint,
    SandboxMetrics,
    SandboxStatus,
    SandboxVolume,
)

__all__ = [
    "OpenSandboxClient",
    "OpenSandboxError",
    "SandboxFailedError",
    "SandboxNotFoundError",
    "rewrite_loopback_endpoint",
    "CreatedSandbox",
    "SandboxEndpoint",
    "SandboxMetrics",
    "SandboxStatus",
    "SandboxVolume",
]
