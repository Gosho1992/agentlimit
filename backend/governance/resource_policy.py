"""
Single source of truth for how a session's memory budget maps onto host and
guest controls. Used by the session manager, the interventions, the evidence
verifier and the infrastructure gate, so they cannot drift apart.

The memory budget is HOST-ACCOUNTED microVM memory (guest RAM + VMM
overhead), because that is what the governor measures (cgroup memory.*).
"""

from backend.config import settings

MIN_GUEST_MB = 128
VMM_PIDS_MAX = 512          # host tasks of the VMM (threads), NOT guest processes
BASE_CPU_QUOTA_USEC = 100_000
THROTTLE_CPU_QUOTA_USEC = 50_000
MEMORY_RECOMMEND_FRACTION = 0.80
MEMORY_HIGH_FRACTION = 0.90


def memory_plan(budget_bytes: int) -> dict:
    budget_mb = budget_bytes // (1024 * 1024)
    guest_mb = budget_mb - settings.vmm_memory_headroom_mb
    if guest_mb < MIN_GUEST_MB:
        raise ValueError(f"memory budget must be at least {MIN_GUEST_MB + settings.vmm_memory_headroom_mb} MB "
                         f"(guest {MIN_GUEST_MB} MB + VMM headroom {settings.vmm_memory_headroom_mb} MB)")
    return {
        "boundary": "host-accounted microVM memory (guest RAM + VMM overhead)",
        "memory_budget_bytes": budget_bytes,
        "host_memory_max_bytes": budget_bytes,                       # hard ceiling (kernel OOM)
        "memory_high_bytes": int(MEMORY_HIGH_FRACTION * budget_bytes),  # soft reclaim pressure
        "recommend_threshold_bytes": int(MEMORY_RECOMMEND_FRACTION * budget_bytes),
        "guest_ram_mb": guest_mb,
        "vmm_headroom_mb": settings.vmm_memory_headroom_mb,
    }
