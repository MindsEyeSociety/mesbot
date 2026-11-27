"""F-004 acceptance tests for the manual deploy.sh (AC-27, AC-28)."""
import f004_harness as h
from f004_harness import layout, sandbox, hold_lock  # noqa: F401  (fixtures)

REQUIRED = ("checkout", "venv_python", "state_dir")


def test_ac27_install_failure_resets_and_does_not_restart(sandbox, layout):
    h.script_roles(h.DEPLOY_SH, REQUIRED)
    p = sandbox.head()
    sandbox.new_commit("D")
    sandbox.fail_install("D")
    res = sandbox.run_script(h.DEPLOY_SH)
    assert res.rc != 0, res.out
    assert sandbox.head() == p
    assert sandbox.restarts() == [], sandbox.events()
    assert len(sandbox.installs()) >= 1
    assert "--require-hashes" in sandbox.installs()[0]


def test_ac27_success_restarts_prints_status_and_ignores_marker(sandbox, layout):
    h.script_roles(h.DEPLOY_SH, REQUIRED)
    d = sandbox.new_commit("D")
    sandbox.write_marker(layout, "a" * 40, "install", "failed")   # a rollback-failed marker
    snap = sandbox.snapshot(layout)
    res = sandbox.run_script(h.DEPLOY_SH)
    assert res.rc == 0, res.out
    assert sandbox.head() == d
    installs = sandbox.installs()
    assert len(installs) == 1 and "-m pip install" in installs[0] and "--require-hashes" in installs[0]
    assert "-r requirements.lock" in installs[0]
    assert [r.split()[2] for r in sandbox.restarts()] == ["discord_app", "flask_app"]
    assert "RUNNING" in res.out, "status must be printed"
    assert sandbox.snapshot(layout) == snap, "deploy.sh must not touch the marker"
    ev = sandbox.events()
    assert next(i for i, e in enumerate(ev) if e.startswith("pip ")) < next(
        i for i, e in enumerate(ev) if e.startswith("supervisorctl restart"))


def test_ac28_lock_held_refuses(sandbox, layout):
    h.script_roles(h.DEPLOY_SH, REQUIRED)
    sandbox.new_commit("D")
    p = sandbox.head()
    with hold_lock(sandbox.state / layout.lock_name):
        res = sandbox.run_script(h.DEPLOY_SH)
    assert res.rc != 0
    assert res.out.strip(), "a message must be printed"
    assert sandbox.head() == p
    assert sandbox.events() == []
