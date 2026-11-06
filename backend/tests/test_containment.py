import pytest

from backend.governance.containment import REQUIRED_CHECKS, evaluate_containment

GOOD = {"egress_blocked": True, "dns_blocked": True, "secret_env_vars": [], "no_secrets_in_env": True,
        "host_paths_visible": [], "uid": 65534, "workspace_mounted": True, "workspace_files": ["orders.csv"],
        "inputs_readable": True, "inputs_read_only": True}
FILES = ["orders.csv"]


def test_all_checks_pass():
    r = evaluate_containment(GOOD, FILES)
    assert r["passed"] and r["failed_checks"] == [] and set(r["checks"]) == set(REQUIRED_CHECKS)


@pytest.mark.parametrize("override,failed", [
    ({"uid": 0}, "unprivileged_execution"),
    ({"uid": True}, "unprivileged_execution"),
    ({"inputs_read_only": False}, "inputs_read_only"),
    ({"egress_blocked": False}, "network_egress_blocked"),
    ({"dns_blocked": False}, "dns_blocked"),
    ({"no_secrets_in_env": False, "secret_env_vars": ["VULTR_INFERENCE_KEY"]}, "no_secrets_in_environment"),
    ({"secret_env_vars": ["API_TOKEN"]}, "no_secrets_in_environment"),
    ({"host_paths_visible": ["/sys/fs/cgroup/agentlimit"]}, "no_host_paths_visible"),
    ({"workspace_mounted": False}, "workspace_mounted"),
    ({"inputs_readable": False}, "inputs_readable"),
    ({"workspace_files": ["orders.csv", "extra.sh"]}, "inputs_match_manifest"),
])
def test_each_failure_is_named(override, failed):
    r = evaluate_containment({**GOOD, **override}, FILES)
    assert not r["passed"] and failed in r["failed_checks"]


@pytest.mark.parametrize("missing", ["egress_blocked", "dns_blocked", "uid", "host_paths_visible",
                                     "inputs_read_only", "workspace_files", "secret_env_vars"])
def test_missing_field_fails(missing):
    p = dict(GOOD)
    p.pop(missing)
    assert not evaluate_containment(p, FILES)["passed"]


def test_probe_error_or_none_fails():
    assert not evaluate_containment(None, FILES)["passed"]
    assert not evaluate_containment({"probe_error": "boom"}, FILES)["passed"]
