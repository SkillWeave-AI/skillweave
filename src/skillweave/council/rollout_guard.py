"""Host-config rollout guard (SW-MCP-001, criterion 5).

A pure function that inspects active consumers and deployment lock state,
returning ``("GO", None)`` when the rollout can proceed or ``("HELD", reason)``
when it must wait. No actual host config is ever touched by this module.

The guard checks two conditions:
1. Is the *target consumer* currently running (in *active_consumers*)?
2. Is a *deployment lock* present?

If the target consumer is running, the rollout is HELD with a reason naming
the consumer. If a deployment lock exists, the rollout is HELD with a reason
naming the lock. Consumer check takes priority (fail-fast: a running consumer
cannot be safely reconfigured).

This is a stateless pure function so it can be tested without any host state.
"""

from __future__ import annotations

from typing import Optional

__all__ = [
    "RolloutDecision",
    "rollout_guard",
]

#: Type alias for the (status, reason) tuple.
RolloutDecision = tuple[str, Optional[str]]


def rollout_guard(
    *,
    active_consumers: set[str],
    deployment_lock: Optional[str] = None,
    target_consumer: str,
) -> RolloutDecision:
    """Determine whether a host-config rollout can proceed.

    A pure function: inspects the supplied *active_consumers* and
    *deployment_lock* arguments and returns the decision. No actual host
    state is read or modified.

    Args:
        active_consumers: Set of consumer IDs currently running on the host.
        deployment_lock: Optional lock identifier (e.g. ``"rollout-2026-10-04"``).
            ``None`` means no lock is held.
        target_consumer: The consumer being deployed to (e.g. ``"claude-desktop"``).

    Returns:
        ``("GO", None)`` if the rollout can proceed.
        ``("HELD", reason)`` if the rollout must wait, with a named reason.
    """
    # Condition 1: Is the target consumer currently running?
    # This check comes first because a running consumer cannot be safely
    # reconfigured. Rolling out while the consumer is active would risk
    # picking up a partial config or crashing the running process.
    if target_consumer in active_consumers:
        return ("HELD", f"consumer {target_consumer!r} is currently running")

    # Condition 2: Is a deployment lock present?
    # A lock means another rollout or maintenance operation is in progress.
    if deployment_lock is not None:
        return ("HELD", f"deployment lock held: {deployment_lock}")

    # No blocking conditions: the rollout can proceed.
    return ("GO", None)
