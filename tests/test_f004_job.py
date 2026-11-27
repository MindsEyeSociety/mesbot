"""F-004 acceptance tests for the daily deploy job (AC-2..AC-20, AC-32, AC-33)."""
import datetime
import re

import pytest

import f004_harness as h
from f004_harness import layout, sandbox, hold_lock  # noqa: F401  (fixtures)

ALLOWED_SUDO = {
    "-n supervisorctl restart discord_app",
    "-n supervisorctl restart flask_app",
    "-n supervisorctl status",
}


def no_side_effects(sb):
    assert sb.installs() == [], f"unexpected install calls: {sb.installs()}"
    assert sb.restarts() == [], f"unexpected restart calls: {sb.restarts()}"


def idx(events, prefix, start=0):
    for i in range(start, len(events)):
        if events[i].startswith(prefix):
            return i
    return -1


# ------------------------------------------------------------------ AC-2
def test_ac2_lock_held_skips(sandbox, layout):
    sandbox.new_commit("D")
    before = sandbox.head()
    with hold_lock(sandbox.state / layout.lock_name):
        res = sandbox.run_job()
    assert res.rc == 0, res.log
    assert "skipped: another deploy is running" in res.log
    assert sandbox.head() == before
    assert sandbox.events() == []
    assert sandbox.marker_files() == []


# ------------------------------------------------------------------ AC-3
def test_ac3_rollback_failed_marker_skips(sandbox, layout):
    sandbox.new_commit("D")
    sandbox.write_marker(layout, "a" * 40, "install", "failed")
    snap = sandbox.snapshot(layout)
    before = sandbox.head()
    res = sandbox.run_job()
    assert res.rc == 0, res.log
    assert "skipped: rollback-failed marker present" in res.log
    assert sandbox.head() == before
    no_side_effects(sandbox)
    assert sandbox.snapshot(layout) == snap


# ------------------------------------------------------------------ AC-4
def test_ac4_fetch_failure(sandbox, layout):
    before = sandbox.head()
    res = sandbox.run_job(extra={h.script_roles(h.find_job(), h.JOB_REQUIRED)[0]["remote"]:
                                 h.posix(sandbox.root / "no-such-remote.git")})
    assert res.rc != 0
    assert "failed: fetch" in res.log
    assert sandbox.marker_files() == []
    assert sandbox.head() == before
    no_side_effects(sandbox)


# ------------------------------------------------------------------ AC-5
def test_ac5_no_new_commit(sandbox, layout):
    res = sandbox.run_job()
    assert res.rc == 0, res.log
    assert "skipped: no new commit" in res.log
    no_side_effects(sandbox)
    assert sandbox.marker_files() == []


# ------------------------------------------------------------------ AC-6
@pytest.mark.parametrize("result", ["succeeded", "none"])
def test_ac6_failed_commit_not_retried(sandbox, layout, result):
    c = sandbox.new_commit("C")
    sandbox.write_marker(layout, c, "install", result)
    snap = sandbox.snapshot(layout)
    before = sandbox.head()
    res = sandbox.run_job()
    assert res.rc == 0, res.log
    assert "skipped: failed commit not retried" in res.log
    assert sandbox.head() == before
    no_side_effects(sandbox)
    assert sandbox.snapshot(layout) == snap


# ------------------------------------------------------------------ AC-7
def test_ac7_new_commit_after_failed_one_deploys_and_clears_marker(sandbox, layout):
    c = sandbox.new_commit("C")
    sandbox.write_marker(layout, c, "install", "succeeded")
    d = sandbox.new_commit("D")
    res = sandbox.run_job()
    assert res.rc == 0, res.log
    assert sandbox.head() == d
    assert not (sandbox.state / layout.marker_name).exists()
    assert f"deployed {d}" in res.log


# ------------------------------------------------------------------ AC-8
@pytest.mark.parametrize("how", ["modified", "staged"])
def test_ac8_local_changes_fail_precheck(sandbox, layout, how):
    d = sandbox.new_commit("D")
    before = sandbox.head()
    f = sandbox.checkout / "README.txt"
    f.write_text("locally edited\n")
    if how == "staged":
        h.git("add", "README.txt", cwd=sandbox.checkout)
    res = sandbox.run_job()
    assert res.rc != 0
    assert "failed: precheck" in res.log
    assert sandbox.head() == before
    assert f.read_text() == "locally edited\n"
    no_side_effects(sandbox)
    text = (sandbox.state / layout.marker_name).read_text()
    shas, steps, results, _ = h.parse_marker(text)
    assert shas == {d} and steps == ["precheck"] and results == ["none"], text


# ------------------------------------------------------------------ AC-9
def test_ac9_rewritten_main_fails_precheck(sandbox, layout):
    x = sandbox.force_rewrite("X")
    before = sandbox.head()
    res = sandbox.run_job()
    assert res.rc != 0
    assert "failed: precheck" in res.log
    assert sandbox.head() == before
    no_side_effects(sandbox)
    text = (sandbox.state / layout.marker_name).read_text()
    shas, steps, results, _ = h.parse_marker(text)
    assert shas == {x} and steps == ["precheck"] and results == ["none"], text


# ------------------------------------------------------------------ AC-10
def test_ac10_successful_deploy(sandbox, layout):
    d = sandbox.new_commit("D")
    res = sandbox.run_job()
    assert res.rc == 0, res.log
    assert sandbox.head() == d
    assert f"deployed {d}" in res.log
    installs = sandbox.installs()
    assert len(installs) == 1, installs
    call = installs[0]
    assert "-m pip install" in call and "--require-hashes" in call
    assert re.search(r"-r (\./)?requirements\.lock\b", call), call
    assert "--break-system-packages" not in call and "requirements.txt" not in call
    assert "lock=[# lock D]" in call, "install must run after the checkout moved to D"
    restarts = sandbox.restarts()
    assert [r.split()[2] for r in restarts] == ["discord_app", "flask_app"], restarts
    sudo = sandbox.sudo_calls()
    restart_sudo = [s for s in sudo if " restart " in s]
    assert restart_sudo == ["-n supervisorctl restart discord_app",
                            "-n supervisorctl restart flask_app"]
    ev = sandbox.events()
    assert idx(ev, "pip ") < idx(ev, "supervisorctl restart discord_app") < idx(ev, "supervisorctl restart flask_app")
    assert not (sandbox.state / layout.marker_name).exists()


# ------------------------------------------------------------------ AC-11
@pytest.mark.parametrize("scenario", ["success", "install_fails", "rollback_fails", "no_new_commit"])
def test_ac11_only_three_sudo_commands(sandbox, layout, scenario):
    if scenario != "no_new_commit":
        sandbox.new_commit("D")
    if scenario == "install_fails":
        sandbox.fail_install("D")
    if scenario == "rollback_fails":
        sandbox.fail_install("D")
        sandbox.fail_install("P")
    sandbox.run_job()
    sudo = sandbox.sudo_calls()
    assert set(sudo) <= ALLOWED_SUDO, sudo
    sup_calls = [e for e in sandbox.events() if e.startswith("supervisorctl ")]
    assert len(sup_calls) == len(sudo), "every supervisorctl call must go through `sudo -n`"
    # static: sudo is never used with anything but supervisorctl, never for git or pip
    text = h.find_job().read_text(errors="replace")
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#") or "sudo" not in s:
            continue
        assert "sudo -n" in s or "SUDO" in s.upper() and "-n" in s, f"sudo without -n: {s}"
        assert not re.search(r"sudo\s+(-n\s+)?(git|pip|python)", s), s


# ------------------------------------------------------------------ AC-12
def test_ac12_health_check_needs_stable_second_check(sandbox, layout):
    d = sandbox.new_commit("D")
    res = sandbox.run_job()
    assert res.rc == 0, res.log
    times = sandbox.status_times("D")
    assert len(times) >= 2, times
    assert times[-1] - times[0] >= h.STABILITY - 0.3, (
        f"passed {times[-1] - times[0]:.2f}s after first check; stability is {h.STABILITY}s")
    gaps = [b - a for a, b in zip(times, times[1:])]
    for g in gaps:
        assert 0.7 <= g <= h.INTERVAL + 1.5, f"poll gaps {gaps}, interval is {h.INTERVAL}s"


# ------------------------------------------------------------------ AC-13
def test_ac13_backoff_fails_at_timeout_and_rolls_back(sandbox, layout):
    p = sandbox.head()
    d = sandbox.new_commit("D")
    sandbox.set_status_mode("D", "discord_backoff")
    res = sandbox.run_job()
    assert res.rc != 0
    times = sandbox.status_times("D")
    assert times[-1] - times[0] >= h.TIMEOUT - 1.2, (
        f"gave up after {times[-1] - times[0]:.2f}s, timeout is {h.TIMEOUT}s: {times}")
    assert times[-1] - times[0] <= h.TIMEOUT + 3
    assert sandbox.head() == p
    text = (sandbox.state / layout.marker_name).read_text()
    shas, steps, results, _ = h.parse_marker(text)
    assert shas == {d} and steps == ["healthcheck"] and results == ["succeeded"], text
    assert f"failed: healthcheck; rolled back to {p}" in res.log
    assert "lock=[# lock P]" in sandbox.installs()[-1]


# ------------------------------------------------------------------ AC-14
def test_ac14_second_check_failure_does_not_pass(sandbox, layout):
    p = sandbox.head()
    d = sandbox.new_commit("D")
    sandbox.set_status_mode("D", "flask_exit_second")
    res = sandbox.run_job()
    assert res.rc != 0
    assert f"deployed {d}" not in res.log
    text = (sandbox.state / layout.marker_name).read_text()
    shas, steps, results, _ = h.parse_marker(text)
    assert shas == {d} and steps == ["healthcheck"], text
    assert sandbox.head() == p


# ------------------------------------------------------------------ AC-15
def test_ac15_supervisord_down_fails_health_check(sandbox, layout):
    p = sandbox.head()
    d = sandbox.new_commit("D")
    sandbox.set_status_mode("D", "down")
    res = sandbox.run_job()
    assert res.rc != 0
    assert f"deployed {d}" not in res.log
    text = (sandbox.state / layout.marker_name).read_text()
    shas, steps, results, _ = h.parse_marker(text)
    assert shas == {d} and steps == ["healthcheck"], text
    assert sandbox.head() == p


# ------------------------------------------------------------------ AC-16
def test_ac16_install_failure_rolls_back(sandbox, layout):
    p = sandbox.head()
    d = sandbox.new_commit("D")
    sandbox.fail_install("D")
    res = sandbox.run_job()
    assert res.rc != 0
    assert sandbox.head() == p
    installs = sandbox.installs()
    assert len(installs) == 2, installs
    assert "lock=[# lock D]" in installs[0]
    assert "lock=[# lock P]" in installs[1] and "--require-hashes" in installs[1]
    ev = sandbox.events()
    second_install = [i for i, e in enumerate(ev) if e.startswith("pip ") and "install" in e][1]
    restarts = [i for i, e in enumerate(ev) if e.startswith("supervisorctl restart")]
    assert len(restarts) == 2 and min(restarts) > second_install, ev
    assert idx(ev, "supervisorctl status", second_install) > 0, "rollback must run the health check"
    text = (sandbox.state / layout.marker_name).read_text()
    shas, steps, results, _ = h.parse_marker(text)
    assert shas == {d} and steps == ["install"] and results == ["succeeded"], text
    assert f"failed: install; rolled back to {p}" in res.log


# ------------------------------------------------------------------ AC-17
@pytest.mark.parametrize("step", ["restart", "healthcheck"])
def test_ac17_restart_and_healthcheck_failures_roll_back(sandbox, layout, step):
    p = sandbox.head()
    d = sandbox.new_commit("D")
    if step == "restart":
        sandbox.fail_restart("D")
    else:
        sandbox.set_status_mode("D", "discord_backoff")
    res = sandbox.run_job()
    assert res.rc != 0
    assert sandbox.head() == p
    installs = sandbox.installs()
    assert len(installs) == 2 and "lock=[# lock P]" in installs[1], installs
    text = (sandbox.state / layout.marker_name).read_text()
    shas, steps, results, _ = h.parse_marker(text)
    assert shas == {d} and steps == [step] and results == ["succeeded"], text
    assert f"failed: {step}; rolled back to {p}" in res.log
    # after the rollback the previous commit's programs were restarted too
    assert any("ver=P" in r for r in sandbox.restarts()), sandbox.restarts()


# ------------------------------------------------------------------ AC-18
def test_ac18_rollback_reinstall_failure_stops(sandbox, layout):
    d = sandbox.new_commit("D")
    sandbox.set_status_mode("D", "discord_backoff")
    sandbox.fail_install("P")
    res = sandbox.run_job()
    assert res.rc != 0
    ev = sandbox.events()
    failing = [i for i, e in enumerate(ev) if e.startswith("pip ") and "ver=P" in e and "install" in e]
    assert len(failing) == 1, ev
    after = ev[failing[0] + 1:]
    assert not [e for e in after if e.startswith("pip ") and "install" in e], after
    assert not [e for e in after if e.startswith("supervisorctl restart")], after
    text = (sandbox.state / layout.marker_name).read_text()
    shas, steps, results, _ = h.parse_marker(text)
    assert shas == {d} and steps == ["healthcheck"] and results == ["failed"], text
    assert "failed: healthcheck; rollback failed" in res.log


# ------------------------------------------------------------------ AC-19
def test_ac19_failed_rollback_blocks_next_run(sandbox, layout):
    d = sandbox.new_commit("D")
    sandbox.set_status_mode("D", "discord_backoff")
    sandbox.set_status_mode("P", "discord_backoff")
    res = sandbox.run_job()
    assert res.rc != 0
    text = (sandbox.state / layout.marker_name).read_text()
    shas, steps, results, _ = h.parse_marker(text)
    assert shas == {d} and results == ["failed"], text
    assert "rollback failed" in res.log
    sandbox.new_commit("E")
    head = sandbox.head()
    events_before = len(sandbox.events())
    snap = sandbox.snapshot(layout)
    res2 = sandbox.run_job()
    assert res2.rc == 0, res2.log
    assert "skipped: rollback-failed marker present" in res2.log
    assert sandbox.head() == head
    assert len(sandbox.events()) == events_before
    assert sandbox.snapshot(layout) == snap


# ------------------------------------------------------------------ AC-20
@pytest.mark.parametrize("scenario", ["precheck", "install", "rollback_failed"])
def test_ac20_marker_content_and_no_secrets(sandbox, layout, scenario):
    d = sandbox.new_commit("D")
    if scenario == "precheck":
        (sandbox.checkout / "README.txt").write_text("edited\n")
    elif scenario == "install":
        sandbox.fail_install("D")
    else:
        sandbox.fail_install("D")
        sandbox.fail_install("P")
    res = sandbox.run_job()
    assert res.rc != 0
    files = sandbox.marker_files()
    assert [f.name for f in files] == [layout.marker_name], files
    text = files[0].read_text()
    shas, steps, results, ts = h.parse_marker(text)
    assert shas == {d}
    expected_step = "precheck" if scenario == "precheck" else "install"
    assert steps == [expected_step], text
    assert results == [{"precheck": "none", "install": "succeeded", "rollback_failed": "failed"}[scenario]], text
    assert len(ts) == 1, f"expected exactly one UTC timestamp in {text!r}"
    nonblank = [l for l in text.splitlines() if l.strip()]
    assert len(nonblank) <= 4, f"marker holds more than the four FR-17 fields: {text!r}"
    iso = re.match(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})", ts[0])
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    if iso:
        t = datetime.datetime(*map(int, iso.groups()))
    else:
        t = datetime.datetime.fromtimestamp(int(ts[0]), datetime.timezone.utc).replace(tzinfo=None)
    assert abs((now - t).total_seconds()) < 600, f"timestamp {ts[0]} is not current UTC"
    for secret in h.SECRETS:
        assert secret not in text
        assert secret not in res.log
    # written atomically: no stray temp file left beside the marker
    assert not [f for f in sandbox.state_files() if "tmp" in f.name.lower()], sandbox.state_files()


# ------------------------------------------------------------------ AC-32
def test_ac32_defaults_without_overrides():
    job = h.find_job()
    roles, vars_ = h.script_roles(job, h.JOB_REQUIRED)
    assert vars_[roles["checkout"]].rstrip("/") == "/home/mesbot"
    assert vars_[roles["venv_python"]] == "/home/mesbot/.venv/bin/python"
    assert vars_[roles["state_dir"]].rstrip("/") == "/var/lib/mesbot-deploy"
    assert float(vars_[roles["interval"]]) == 10
    assert float(vars_[roles["timeout"]]) == 120
    assert float(vars_[roles["stability"]]) == 30
    assert "github.com" in vars_[roles["remote"]] and vars_[roles["remote"]].startswith("https://")
    assert "@" not in vars_[roles["remote"]], "remote URL must carry no credential"


# ------------------------------------------------------------------ AC-33
INVALID = ["symlink", "directory", "bad_sha", "short_sha", "bad_step", "bad_result",
           "missing_field", "bad_timestamp", "injection"]


@pytest.mark.parametrize("variant", INVALID)
def test_ac33_invalid_marker_skips_job(sandbox, layout, variant):
    d = sandbox.new_commit("D")
    variants = h.invalid_variants(layout, sandbox.root, d)
    if variant not in variants:
        pytest.skip("environment: marker timestamp not recognisable")
    variants[variant](sandbox.state / layout.marker_name)
    snap = sandbox.snapshot(layout)
    before = sandbox.head()
    for _ in range(2):
        res = sandbox.run_job()
        assert res.rc == 0, res.log
        assert "skipped: invalid marker" in res.log
        assert sandbox.head() == before
        no_side_effects(sandbox)
        assert sandbox.snapshot(layout) == snap
    assert not (sandbox.root / "PWNED").exists() and not (sandbox.root / "PWNED2").exists()
