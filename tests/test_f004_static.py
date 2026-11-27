"""F-004 static/contract tests on repo files (AC-1, AC-29, AC-30, AC-31) and SEC checks."""
import re
import subprocess

import f004_harness as h


def test_ac1_cron_file():
    cron = h.find_cron()
    lines = cron.read_text().splitlines()
    comments = "\n".join(l for l in lines if l.lstrip().startswith("#"))
    jobs = [l for l in lines if l.strip() and not l.lstrip().startswith("#")
            and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", l.strip())]
    assert len(jobs) == 1, jobs
    f = jobs[0].split(None, 6)
    assert f[:5] == ["45", "17", "*", "*", "*"], jobs[0]
    assert f[5] == "mesbot", jobs[0]
    cmd = f[6]
    assert h.find_job().name in cmd, cmd
    assert re.search(r">>\s*/var/lib/mesbot-deploy/\S+", cmd), cmd
    assert "/etc/cron.d/" in comments
    assert "root" in comments.lower()
    assert "0644" in comments
    assert "beta" in comments.lower()
    assert "execute" in comments.lower()


def test_ac29_deploy_sh_installs_only_from_lock_with_hashes():
    text = h.DEPLOY_SH.read_text()
    code = [l for l in text.splitlines() if not l.strip().startswith("#")]
    installs = [l for l in code if re.search(r"\bpip3?\b.*\binstall\b|\binstall\b.*\bpip\b", l)]
    assert installs, "deploy.sh has no pip install"
    for l in installs:
        assert "--require-hashes" in l and re.search(r"-r[\"',\s]+(\./)?requirements\.lock\b", l), l
        assert "/home/mesbot/.venv/bin/python" in text or ".venv/bin/python" in l
        assert "--break-system-packages" not in l
    joined = "\n".join(code)
    assert "requirements.txt" not in joined
    assert "/home/mesbot/.venv/bin/python" in text


def test_sec2_job_installs_only_from_lock_with_hashes():
    text = h.find_job().read_text()
    code = [l for l in text.splitlines() if not l.strip().startswith("#")]
    installs = [l for l in code if re.search(r"\binstall\b", l) and "pip" in l]
    assert installs, "job has no pip install"
    for l in installs:
        assert "--require-hashes" in l and re.search(r"-r[\"',\s]+(\./)?requirements\.lock\b", l), l
        assert "--break-system-packages" not in l
    assert "requirements.txt" not in "\n".join(code)


def test_sec1_sec5_job_uses_no_token_and_no_root_git():
    text = h.find_job().read_text()
    code = "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))
    assert not re.search(r"GITHUB_TOKEN|GH_TOKEN|x-access-token|https://[^/\s]*:[^/\s]*@", code)
    assert not re.search(r"sudo\s+(-\S+\s+)*git\b", code)


def _git_show(path):
    r = subprocess.run(["git", "show", f"{h.BASE_COMMIT}:{path}"], cwd=str(h.REPO),
                       capture_output=True, text=True)
    assert r.returncode == 0, f"cannot read {path} at {h.BASE_COMMIT}: {r.stderr}"
    return r.stdout.replace("\r\n", "\n")


def test_ac30_supervisor_confs_only_command_changes():
    for name in ("mesbot.conf", "flask.conf"):
        old = _git_show(f"supervisor/{name}")
        new = (h.REPO / "supervisor" / name).read_text().replace("\r\n", "\n")
        old_lines, new_lines = old.splitlines(), new.splitlines()
        assert len(old_lines) == len(new_lines), name
        changed = [(o, n) for o, n in zip(old_lines, new_lines) if o != n]
        assert len(changed) == 1, (name, changed)
        o, n = changed[0]
        assert o.startswith("command=python3 -u "), o
        assert n == o.replace("command=python3 -u ", "command=/home/mesbot/.venv/bin/python -u ", 1), n


def test_ac31_architecture_deployment_section():
    text = (h.REPO / "docs" / "ARCHITECTURE.md").read_text()
    m = re.search(r"^## Deployment\b.*?(?=^## |\Z)", text, re.S | re.M)
    assert m, "no Deployment section"
    s = m.group(0)
    low = s.lower()
    for needle in ("17:45", "/var/lib/mesbot-deploy", "/home/mesbot/.venv", "requirements.lock",
                   "/usr/local/sbin", "0750", "/etc/cron.d", "marker", "deploy.sh"):
        assert needle in s, f"Deployment section lacks {needle!r}"
    assert "python 3.12" in low
    assert "supervisor" in low and ("reload" in low or "reread" in low or "supervisorctl update" in low)
    assert "beta" in low
    assert re.search(r"(delete|remove)[^.\n]*marker|marker[^.\n]*(delet|remov)", low), "no marker-clearing instructions"
    assert re.search(r"alert", low)
    assert re.search(r"as (user )?`?mesbot`?", low)
