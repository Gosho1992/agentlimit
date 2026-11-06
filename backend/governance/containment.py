"""
Canonical containment policy.

ONE function decides whether a sandbox is contained. It is used when a
session starts (fail-closed: any failed check refuses the session) and again
by the evidence verifier, which re-evaluates the raw probe recorded in the
signed ledger instead of trusting a stored verdict.

A missing or malformed probe field is a FAILURE, never a pass.
"""

REQUIRED_CHECKS = (
    "network_egress_blocked",
    "dns_blocked",
    "no_secrets_in_environment",
    "no_host_paths_visible",
    "workspace_mounted",
    "inputs_readable",
    "inputs_read_only",
    "unprivileged_execution",
    "inputs_match_manifest",
)


def evaluate_containment(probe: dict | None, expected_files: list[str] | None = None) -> dict:
    """expected_files: names from the signed input manifest; the guest must see
    exactly these files in /workspace (None = no inputs expected)."""
    p = probe if isinstance(probe, dict) else {}
    host_paths = p.get("host_paths_visible")
    secret_vars = p.get("secret_env_vars")
    uid = p.get("uid")
    checks = {
        "network_egress_blocked": p.get("egress_blocked") is True,
        "dns_blocked": p.get("dns_blocked") is True,
        "no_secrets_in_environment": p.get("no_secrets_in_env") is True
                                     and isinstance(secret_vars, list) and secret_vars == [],
        "no_host_paths_visible": isinstance(host_paths, list) and host_paths == [],
        "workspace_mounted": p.get("workspace_mounted") is True,
        "inputs_readable": p.get("inputs_readable") is True,
        "inputs_read_only": p.get("inputs_read_only") is True,
        "unprivileged_execution": isinstance(uid, int) and not isinstance(uid, bool) and uid != 0,
        "inputs_match_manifest": isinstance(p.get("workspace_files"), list)
                                 and sorted(p["workspace_files"]) == sorted(expected_files or []),
    }
    failed = [k for k in REQUIRED_CHECKS if not checks[k]]
    return {"checks": checks, "passed": not failed, "failed_checks": failed,
            "probe_error": p.get("probe_error")}
