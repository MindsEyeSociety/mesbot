"""F-004 acceptance and contract tests for the root alert script's deploy alerts
(AC-21..AC-26) and the job -> alert marker interface."""
import sys

import pytest

import f004_harness as h
from f004_harness import layout, sandbox  # noqa: F401  (fixtures)

# Spec FR-3 / A-007: running the job needs POSIX flock (Linux host).
needs_flock = pytest.mark.skipif(sys.platform != "linux", reason="needs flock; runs in CI on Ubuntu")

C = "c" * 40
E = "e" * 40


@pytest.fixture
def alert(tmp_path, layout):
    return h.AlertBox(tmp_path / "alert", layout)


def subj(alert, tail):
    return f"[mesbot] {alert.host} {tail}"


# ------------------------------------------------------------------ AC-21
@pytest.mark.parametrize("result", ["succeeded", "none"])
def test_ac21_deploy_failed_alert_once(alert, result):
    text = alert.write_marker(C, "install", result)
    r1 = alert.run()
    assert r1.rc == 0, r1.out
    dep = r1.deploy_publishes()
    assert len(dep) == 1, r1.out
    assert dep[0]["subject"] == subj(alert, "deploy failed")
    # FR-19: the message gives the commit, step, rollback result and timestamp
    _, _, _, ts = h.parse_marker(text)
    assert C in dep[0]["body"] and "install" in dep[0]["body"]
    assert result in dep[0]["body"]
    assert ts and ts[0] in dep[0]["body"], dep[0]["body"]
    r2 = alert.run()
    assert r2.deploy_publishes() == [], r2.out


# ------------------------------------------------------------------ AC-22
def test_ac22_new_marker_new_alert(alert):
    alert.write_marker(C, "install", "succeeded")
    assert len(alert.run().deploy_publishes()) == 1
    alert.write_marker(E, "restart", "succeeded")
    r = alert.run()
    dep = r.deploy_publishes()
    assert len(dep) == 1, r.out
    assert dep[0]["subject"] == subj(alert, "deploy failed")
    assert E[:7] in dep[0]["body"] and C[:7] not in dep[0]["body"]
    assert alert.run().deploy_publishes() == []


# ------------------------------------------------------------------ AC-23
def test_ac23_rollback_failed_critical_alert(alert):
    text = alert.write_marker(C, "healthcheck", "failed")
    r1 = alert.run()
    assert r1.rc == 0, r1.out
    assert r1.subjects().count(subj(alert, "deploy and rollback failed, manual action needed")) == 1, r1.out
    assert subj(alert, "deploy failed") not in r1.subjects()
    body = r1.deploy_publishes()[0]["body"]
    # FR-20: the message gives the commit, step and timestamp
    _, _, _, ts = h.parse_marker(text)
    assert C in body and "healthcheck" in body
    assert ts and ts[0] in body, body
    assert alert.run().deploy_publishes() == []


# ------------------------------------------------------------------ AC-24
@pytest.mark.parametrize("result", ["succeeded", "failed"])
def test_ac24_recovery_message_once(alert, result):
    alert.write_marker(C, "install", result)
    assert len(alert.run().deploy_publishes()) == 1
    alert.marker.unlink()
    r = alert.run()
    assert r.subjects().count(subj(alert, "deploy recovered")) == 1, r.out
    assert len(r.deploy_publishes()) == 1
    assert alert.run().deploy_publishes() == []


def test_ac24_no_recovery_without_prior_alert(alert):
    r = alert.run()
    assert r.rc == 0
    assert r.deploy_publishes() == [], r.out
    assert r.publishes == []


# ------------------------------------------------------------------ AC-25
def test_ac25_unchanged_when_healthy_and_no_marker(alert):
    r = alert.run()
    assert r.rc == 0
    assert r.out.strip() == "", r.out
    assert not (alert.state / "state").exists(), "no supervisor state file when healthy"


def test_ac25_supervisor_and_deploy_alerts_are_independent(alert):
    alert.set_status("discord_app FATAL Exited too quickly\nflask_app RUNNING pid 2, uptime 0:01:00\n")
    alert.write_marker(C, "install", "succeeded")
    r1 = alert.run()
    assert subj(alert, "supervisor program not running") in r1.subjects(), r1.out
    assert subj(alert, "deploy failed") in r1.subjects(), r1.out
    assert len(r1.publishes) == 2
    state = (alert.state / "state").read_text().splitlines()
    assert len(state) == 3 and "discord_app FATAL" in state[0], state
    r2 = alert.run()
    assert r2.publishes == [], r2.out
    # supervisor recovers: its own recovery message, deploy alert state untouched
    alert.set_status("discord_app RUNNING pid 1, uptime 0:01:00\nflask_app RUNNING pid 2, uptime 0:01:00\n")
    r3 = alert.run()
    assert r3.subjects() == [subj(alert, "supervisor recovered")], r3.out
    # deploy marker removed: deploy recovery only
    alert.marker.unlink()
    r4 = alert.run()
    assert r4.subjects() == [subj(alert, "deploy recovered")], r4.out


# ------------------------------------------------------------------ AC-26
@pytest.mark.parametrize("variant", [
    "symlink", "directory", "bad_sha", "short_sha", "bad_step", "bad_result",
    "missing_field", "bad_timestamp", "injection"])
def test_ac26_invalid_marker_alert(alert, layout, variant):
    variants = h.invalid_variants(layout, alert.root, C)
    if variant not in variants:
        pytest.skip("environment: marker timestamp not recognisable")
    variants[variant](alert.marker)
    r1 = alert.run()
    assert r1.rc == 0, r1.out
    dep = r1.deploy_publishes()
    assert len(dep) == 1, r1.out
    assert dep[0]["subject"] == subj(alert, "deploy marker invalid"), r1.out
    body = dep[0]["body"]
    for forbidden in ("BOGUS_", "symlink-target", C, layout.template_sha, "PWNED", "touch "):
        assert forbidden not in body, f"alert leaks marker content {forbidden!r}: {body}"
    assert "deploy failed" not in " ".join(r1.subjects()), "symlink target must not be followed"
    assert not (alert.root / "PWNED").exists() and not (alert.root / "PWNED2").exists()
    r2 = alert.run()
    assert r2.deploy_publishes() == [], r2.out
    if alert.marker.is_dir() and not alert.marker.is_symlink():
        import shutil
        shutil.rmtree(alert.marker)
    else:
        alert.marker.unlink()
    r3 = alert.run()
    assert r3.subjects() == [subj(alert, "deploy recovered")], r3.out
    assert alert.run().deploy_publishes() == []


# ------------------------------------------------------------------ contract: job -> alert
@needs_flock
def test_contract_job_marker_is_read_by_alert(sandbox, layout, tmp_path):
    d = sandbox.new_commit("D")
    (sandbox.checkout / "README.txt").write_text("edited\n")
    res = sandbox.run_job()
    assert res.rc != 0
    marker = sandbox.state / layout.marker_name
    alert = h.AlertBox(tmp_path / "alert", layout)
    alert.marker.write_bytes(marker.read_bytes())
    r = alert.run()
    dep = r.deploy_publishes()
    assert len(dep) == 1 and dep[0]["subject"] == subj(alert, "deploy failed"), r.out
    assert d[:7] in dep[0]["body"] and "precheck" in dep[0]["body"]


@needs_flock
def test_contract_job_rollback_failed_marker_gives_critical_alert(sandbox, layout, tmp_path):
    d = sandbox.new_commit("D")
    sandbox.fail_install("D")
    sandbox.fail_install("P")
    res = sandbox.run_job()
    assert res.rc != 0
    alert = h.AlertBox(tmp_path / "alert", layout)
    alert.marker.write_bytes((sandbox.state / layout.marker_name).read_bytes())
    r = alert.run()
    assert r.subjects().count(subj(alert, "deploy and rollback failed, manual action needed")) == 1, r.out
