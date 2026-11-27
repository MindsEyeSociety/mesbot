"""Test harness for F-004 (automatic deploy of main).

Everything runs against a local bare git repo standing in for GitHub, a stub
``supervisorctl`` / ``sudo`` on PATH and a stub venv python. Nothing touches a real
server, network, database, Discord, SNS or supervisor.

The spec leaves three things for the implementation to settle (spec section 10): the
deploy script's name, the override variable names and the marker's file name/format.
The harness therefore discovers them from the deliverables themselves, never from the
developer's notes:

* the job is the ``scripts/*deploy*`` script (not the alert script);
* override variables are the ``${NAME:-default}`` (or ``environ.get``) declarations whose
  *defaults* are the values FR-1..FR-18 fix (``/home/mesbot``, ``/home/mesbot/.venv/bin/python``,
  ``/var/lib/mesbot-deploy``, 10/120/30 s, ...), with variable-name keywords as a fallback;
* the marker file name and format are taken from a marker the job itself writes (a
  ``precheck`` failure), and test markers are made by substituting field values in it.
"""
import contextlib
import datetime
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
ALERT = SCRIPTS / "mesbot-supervisor-alert.sh"
DEPLOY_SH = REPO / "deploy.sh"
BASE_COMMIT = "a2942f39259b"
BASH = shutil.which("bash") or "bash"
SHA_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-f]{40}(?![0-9a-fA-F])")
TS_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z| ?UTC|\+00:00)?|(?<!\d)\d{10}(?!\d)"
)
STEPS = ("precheck", "install", "restart", "healthcheck")
RESULTS = ("succeeded", "failed", "none")
SECRETS = ("S3CR3T-TOKEN-VALUE-0042", "S3CR3T-DB-PASSWORD-0043")

# Scaled health-check timings (FR-18): interval / timeout / stability, in seconds.
INTERVAL, TIMEOUT, STABILITY = 1, 5, 2

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def posix(p):
    return Path(p).as_posix()


def write_exec(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="\n") as f:
        f.write(text)
    path.chmod(0o755)


# --------------------------------------------------------------------------- discovery

def find_job() -> Path:
    cands = sorted(
        p for p in SCRIPTS.iterdir()
        if p.is_file() and p.suffix in (".sh", ".bash", ".py")
        and "deploy" in p.name.lower() and p.name != ALERT.name
    )
    assert cands, "no deploy job script (scripts/*deploy*) exists"
    cands.sort(key=lambda p: (p.suffix != ".sh", p.name))
    return cands[0]


def find_cron() -> Path:
    cands = sorted(p for p in SCRIPTS.glob("*.cron") if p.name != "mesbot-supervisor-alert.cron")
    assert cands, "no new cron file (scripts/*.cron) for the deploy job exists"
    return cands[0]


_P1 = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):?-([^}]*)\}")
_P2 = re.compile(r"(?:environ\.get|getenv)\(\s*[\"']([A-Za-z_]\w*)[\"']\s*,\s*[\"']([^\"']*)[\"']")


_CONST = re.compile(r"^([A-Z][A-Z0-9_]*)\s*=\s*(\"[^\"\n]*\"|'[^'\n]*'|[0-9][0-9._]*)\s*(?:#.*)?$", re.M)
# Python forms: env.get("NAME") or DEFAULT_X   /   helper(env, "NAME", DEFAULT_X)
_P3 = re.compile(r"\.get\(\s*[\"']([A-Z][A-Z0-9_]*)[\"']\s*\)\s*or\s+([A-Z][A-Z0-9_]*)\b")
_P4 = re.compile(r"\(\s*\w+\s*,\s*[\"']([A-Z][A-Z0-9_]*)[\"']\s*,\s*([A-Z][A-Z0-9_]*)\s*\)")


def _norm_default(v: str) -> str:
    v = v.strip().strip("\"'")
    if re.fullmatch(r"\d+\.0+", v):
        v = v.split(".")[0]
    return v


def declared_vars(text: str) -> dict:
    out = {}
    for rx in (_P1, _P2):
        for name, default in rx.findall(text):
            out.setdefault(name, default.strip().strip("\"'"))
    consts = {n: _norm_default(v) for n, v in _CONST.findall(text)}
    for rx in (_P3, _P4):
        for name, const in rx.findall(text):
            if "_" in name and const in consts:
                out.setdefault(name, consts[const])
    return out


def classify(vars_: dict) -> dict:
    roles = {}
    for n, d in vars_.items():
        nu = n.upper()
        dl = d.rstrip("/")
        if dl == "/home/mesbot":
            roles.setdefault("checkout", n)
        elif dl.endswith(".venv/bin/python"):
            roles.setdefault("venv_python", n)
        elif dl == "/var/lib/mesbot-deploy":
            roles.setdefault("state_dir", n)
        elif "github.com" in d or any(k in nu for k in ("REMOTE", "REPO_URL", "GIT_URL")):
            roles.setdefault("remote", n)
        elif "supervisorctl" in d or "SUPERVISORCTL" in nu:
            roles.setdefault("supervisorctl", n)
        elif any(k in nu for k in ("INTERVAL", "POLL")):
            roles.setdefault("interval", n)
        elif any(k in nu for k in ("TIMEOUT", "LIMIT", "DEADLINE")):
            roles.setdefault("timeout", n)
        elif any(k in nu for k in ("STABIL", "STABLE", "SETTLE", "HOLD", "CONFIRM")):
            roles.setdefault("stability", n)
        elif any(k in nu for k in ("CHECKOUT", "REPO_DIR", "REPO_PATH")):
            roles.setdefault("checkout", n)
        elif "VENV" in nu or "PYTHON" in nu:
            roles.setdefault("venv_python", n)
        elif "STATE" in nu:
            roles.setdefault("state_dir", n)
        elif d in ("10", "120", "30"):
            roles.setdefault({"10": "interval", "120": "timeout", "30": "stability"}[d], n)
    return roles


JOB_REQUIRED = ("checkout", "remote", "venv_python", "state_dir", "supervisorctl",
                "interval", "timeout", "stability")


def script_roles(script: Path, required=()):
    vars_ = declared_vars(script.read_text(errors="replace"))
    roles = classify(vars_)
    missing = [r for r in required if r not in roles]
    assert not missing, (
        f"{script.name} has no discoverable environment override for {missing} "
        f"(FR-18); declared overrides: {vars_}"
    )
    return roles, vars_


# --------------------------------------------------------------------------- git helpers

def git(*args, cwd, check=True):
    env = dict(os.environ, **GIT_ENV)
    r = subprocess.run(["git", *args], cwd=str(cwd), env=env, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {args} failed: {r.stderr}")
    return r.stdout.strip()


@dataclass
class Result:
    rc: int
    out: str
    log: str   # stdout + stderr + any *.log in the state dir


@dataclass
class Layout:
    marker_name: str
    template: str
    template_sha: str
    lock_name: str


# --------------------------------------------------------------------------- sandbox

SUPERVISORCTL_STUB = r"""#!/usr/bin/env bash
CTL="__CTL__"; CK="__CK__"
ver="$(tr -d '[:space:]' < "$CK/version.txt" 2>/dev/null)"
now="$(date +%s.%N)"
echo "supervisorctl $* ver=$ver" >> "$CTL/events.log"
run() { printf '%-20s RUNNING   pid 100, uptime 0:00:05\n' "$1"; }
case "${1:-}" in
restart)
  rm -f "$CTL/status_first"
  if [ -e "$CTL/restart_fail.$ver" ]; then echo "${2:-}: ERROR (spawn error)"; exit 1; fi
  echo "${2:-}: stopped"; echo "${2:-}: started"; exit 0 ;;
status)
  echo "$now $ver" >> "$CTL/status_times.log"
  [ -e "$CTL/status_first" ] || echo "$now" > "$CTL/status_first"
  first="$(cat "$CTL/status_first")"
  mode="$(cat "$CTL/status_mode.$ver" 2>/dev/null || echo ok)"
  early="$(awk -v a="$now" -v b="$first" 'BEGIN{print (a-b<1.2)?1:0}')"
  case "$mode" in
    discord_backoff)
      echo "discord_app          BACKOFF   Exited too quickly (process log may have details)"
      run flask_app; exit 3 ;;
    flask_exit_second)
      if [ "$early" = 1 ]; then run discord_app; run flask_app; exit 0; fi
      run discord_app; echo "flask_app            EXITED    Nov 27 10:00 AM"; exit 3 ;;
    down)
      echo "unix:///var/run/supervisor.sock no such file"; exit 3 ;;
    *) run discord_app; run flask_app; exit 0 ;;
  esac ;;
*) exit 0 ;;
esac
"""

SUDO_STUB = r"""#!/usr/bin/env bash
echo "sudo $*" >> "__CTL__/events.log"
while [ "${1:-}" = "-n" ]; do shift; done
exec "$@"
"""

PYTHON_STUB = r"""#!/usr/bin/env bash
CTL="__CTL__"; CK="__CK__"
ver="$(tr -d '[:space:]' < "$CK/version.txt" 2>/dev/null)"
lock="$(grep -m1 '^# lock' "$CK/requirements.lock" 2>/dev/null)"
echo "pip $* ver=$ver lock=[$lock]" >> "$CTL/events.log"
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "pip" ] && [ -e "$CTL/install_fail.$ver" ]; then
  echo "ERROR: THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE." >&2
  exit 1
fi
exit 0
"""


class Sandbox:
    """A fake server: bare remote, seeded work clone, deployed checkout, stubs, state dir."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.remote = root / "remote.git"
        self.work = root / "work"
        self.checkout = root / "checkout"
        self.state = root / "state"
        self.ctl = root / "ctl"
        self.bin = root / "bin"
        self.venv_python = root / "venv" / "bin" / "python"
        self.home = root / "home"
        for d in (self.state, self.ctl, self.home):
            d.mkdir(parents=True, exist_ok=True)
        git("init", "--bare", str(self.remote), cwd=root)
        git("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.remote)
        git("init", str(self.work), cwd=root)
        git("checkout", "-b", "main", cwd=self.work)
        git("remote", "add", "origin", posix(self.remote), cwd=self.work)
        (self.work / "README.txt").write_text("readme\n")
        self.root_sha = self._commit("R", message="root")
        self.p_sha = self._commit("P")
        git("push", "origin", "main", cwd=self.work)
        git("clone", "-b", "main", posix(self.remote), str(self.checkout), cwd=root)
        (self.checkout / ".env").write_text(
            f"TOKEN={SECRETS[0]}\nDB_PASSWORD={SECRETS[1]}\n")
        subs = {"__CTL__": posix(self.ctl), "__CK__": posix(self.checkout)}
        for name, text in (("supervisorctl", SUPERVISORCTL_STUB), ("sudo", SUDO_STUB)):
            for k, v in subs.items():
                text = text.replace(k, v)
            write_exec(self.bin / name, text)
        text = PYTHON_STUB
        for k, v in subs.items():
            text = text.replace(k, v)
        write_exec(self.venv_python, text)

    # ---- repo manipulation
    def _commit(self, version, message=None):
        (self.work / "version.txt").write_text(version + "\n")
        (self.work / "requirements.lock").write_text(f"# lock {version}\nfoo==1.0 --hash=sha256:00\n")
        git("add", "-A", cwd=self.work)
        git("commit", "-m", message or f"version {version}", cwd=self.work)
        return git("rev-parse", "HEAD", cwd=self.work)

    def new_commit(self, version):
        sha = self._commit(version)
        git("push", "origin", "main", cwd=self.work)
        return sha

    def force_rewrite(self, version="X"):
        """Rewrite remote main so the checkout's HEAD (P) is not an ancestor."""
        git("checkout", "-B", "alt", self.root_sha, cwd=self.work)
        sha = self._commit(version)
        git("push", "-f", "origin", "alt:main", cwd=self.work)
        return sha

    def head(self):
        return git("rev-parse", "HEAD", cwd=self.checkout)

    # ---- stub control
    def set_status_mode(self, ver, mode):
        (self.ctl / f"status_mode.{ver}").write_text(mode)

    def fail_install(self, ver):
        (self.ctl / f"install_fail.{ver}").write_text("1")

    def fail_restart(self, ver):
        (self.ctl / f"restart_fail.{ver}").write_text("1")

    def events(self):
        p = self.ctl / "events.log"
        return p.read_text().splitlines() if p.exists() else []

    def installs(self):
        return [e for e in self.events() if e.startswith("pip ") and "install" in e]

    def restarts(self):
        return [e for e in self.events() if e.startswith("supervisorctl restart")]

    def sudo_calls(self):
        return [e[len("sudo "):] for e in self.events() if e.startswith("sudo ")]

    def status_times(self, ver):
        p = self.ctl / "status_times.log"
        if not p.exists():
            return []
        out = []
        for line in p.read_text().splitlines():
            t, v = line.split()
            if v == ver:
                out.append(float(t))
        return out

    # ---- state dir
    def state_files(self):
        return sorted(f for f in self.state.iterdir())

    def marker_files(self):
        return [f for f in self.state_files()
                if not re.search(r"lock|lck", f.name, re.I)
                and not f.name.endswith((".log", ".tmp"))]

    def snapshot(self, layout):
        p = self.state / layout.marker_name
        if p.is_symlink():
            return ("symlink", os.readlink(p))
        if p.is_dir():
            return ("dir", sorted(x.name for x in p.iterdir()))
        if p.exists():
            return ("file", p.read_bytes())
        return None

    def write_marker(self, layout, sha, step, result):
        text = layout.template.replace(layout.template_sha, sha)
        text = text.replace("precheck", step).replace("none", result)
        (self.state / layout.marker_name).write_text(text)
        return text

    # ---- running
    def env_for(self, script: Path, extra=None, timings=True):
        roles, vars_ = script_roles(script)
        env = dict(os.environ)
        env.update(GIT_ENV)
        env["HOME"] = posix(self.home)
        env["PATH"] = posix(self.bin) + os.pathsep + env.get("PATH", "")
        env["TOKEN"] = SECRETS[0]
        env["DB_PASSWORD"] = SECRETS[1]
        env["LC_ALL"] = "C"
        m = {
            "checkout": posix(self.checkout),
            "remote": posix(self.remote),
            "venv_python": posix(self.venv_python),
            "state_dir": posix(self.state),
        }
        sup_default = vars_.get(roles.get("supervisorctl", ""), "supervisorctl")
        m["supervisorctl"] = "sudo -n supervisorctl" if "sudo" in sup_default else "supervisorctl"
        if timings:
            m.update(interval=str(INTERVAL), timeout=str(TIMEOUT), stability=str(STABILITY))
        for role, value in m.items():
            if role in roles:
                env[roles[role]] = value
        env.update(extra or {})
        return env

    def run_script(self, script: Path, extra=None, timeout=120):
        env = self.env_for(script, extra)
        cmd = [sys.executable, str(script)] if script.suffix == ".py" else [BASH, str(script)]
        r = subprocess.run(cmd, cwd=str(self.root), env=env, capture_output=True,
                           text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        out = (r.stdout or "") + (r.stderr or "")
        log = out
        for f in self.state.glob("*.log"):
            log += "\n" + f.read_text(errors="replace")
        return Result(r.returncode, out, log)

    def run_job(self, extra=None, timeout=120):
        job = find_job()
        script_roles(job, JOB_REQUIRED)
        return self.run_script(job, extra, timeout)


@contextlib.contextmanager
def hold_lock(path: Path):
    try:
        import fcntl
    except ImportError:  # pragma: no cover - environment limitation, not a product failure
        pytest.skip("environment: fcntl/flock not available on this platform")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        yield
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- layout fixture

@pytest.fixture(scope="session")
def layout(tmp_path_factory):
    root = tmp_path_factory.mktemp("f004-layout")
    sb = Sandbox(root / "a")
    d = sb.new_commit("D")
    (sb.checkout / "README.txt").write_text("locally edited\n")
    res = sb.run_job()
    assert res.rc != 0, f"precheck failure must exit non-zero; got {res.rc}\n{res.log}"
    files = sb.marker_files()
    assert len(files) == 1, f"expected exactly one marker file in state dir, found {files}\n{res.log}"
    text = files[0].read_text()
    assert d in text and "precheck" in text and "none" in text, (
        f"precheck marker lacks fetched sha / step / result: {text!r}")
    sb2 = Sandbox(root / "b")
    res2 = sb2.run_job()
    locks = [f.name for f in sb2.state_files() if re.search(r"lock|lck", f.name, re.I)]
    assert len(locks) >= 1, (
        f"job left no lock file in the state dir after a run: {[f.name for f in sb2.state_files()]}\n{res2.log}")
    return Layout(files[0].name, text, d, locks[0])


@pytest.fixture
def sandbox(tmp_path):
    return Sandbox(tmp_path / "sb")


# --------------------------------------------------------------------------- marker tooling

def parse_marker(text: str):
    shas = set(SHA_RE.findall(text))
    steps = [s for s in STEPS if re.search(rf"\b{s}\b", text)]
    results = [r for r in RESULTS if re.search(rf"\b{r}\b", text)]
    ts = TS_RE.findall(text)
    return shas, steps, results, ts


def invalid_variants(layout: Layout, sandbox_root: Path, good_sha: str):
    """name -> function(path) that creates an invalid marker at path.

    The symlink's target holds a perfectly valid marker, so a reader that follows the link
    would behave differently from one that rejects it."""
    t = layout.template

    def sub(old, new):
        return t.replace(old, new)

    ts_match = TS_RE.search(layout.template)

    def symlink(path):
        target = sandbox_root / "symlink-target-SENTINEL.txt"
        target.write_text(t.replace(layout.template_sha, good_sha).replace("precheck", "install")
                          .replace("none", "succeeded"))
        try:
            os.symlink(target, path)
        except (OSError, NotImplementedError):
            pytest.skip("environment: cannot create symlinks here")
        if not path.is_symlink():
            pytest.skip("environment: symlink creation not honoured here")

    def directory(path):
        path.mkdir()
        (path / "inner").write_text(t)

    def write(text):
        return lambda path: path.write_text(text)

    variants = {
        "symlink": symlink,
        "directory": directory,
        "bad_sha": write(sub(layout.template_sha, "BOGUS_SENTINEL_77")),
        "short_sha": write(sub(layout.template_sha, layout.template_sha[:20])),
        "bad_step": write(sub("precheck", "BOGUS_STEP_77")),
        "bad_result": write(sub("none", "BOGUS_RESULT_77")),
        "missing_field": write("\n".join(l for l in t.splitlines() if "precheck" not in l) + "\n"),
        "injection": write(sub(layout.template_sha,
                               f"$(touch {posix(sandbox_root)}/PWNED)`touch {posix(sandbox_root)}/PWNED2`")),
    }
    if ts_match:
        variants["bad_timestamp"] = write(t.replace(ts_match.group(0), "BOGUS_TIME_77"))
    return variants


# --------------------------------------------------------------------------- alert script

def hostname():
    return subprocess.run(["hostname"], capture_output=True, text=True).stdout.strip()


class AlertBox:
    """Runs the root alert script as an unprivileged user with --dry-run."""

    def __init__(self, root: Path, layout: Layout):
        self.layout = layout
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.state = root / "alert-state"          # MSA_STATE_DIR (supervisor + deploy dedup)
        self.deploy_dir = root / "deploy-state"    # holds the marker
        self.state.mkdir(exist_ok=True)
        self.deploy_dir.mkdir(exist_ok=True)
        self.status_file = root / "status.txt"
        self.set_status("discord_app RUNNING pid 1, uptime 0:01:00\nflask_app RUNNING pid 2, uptime 0:01:00\n")
        write_exec(root / "supctl",
                   f"#!/usr/bin/env bash\ncat \"{posix(self.status_file)}\"\nexit 0\n")
        self.marker = self.deploy_dir / layout.marker_name
        self.host = hostname()

    def set_status(self, text):
        self.status_file.write_text(text)

    def write_marker(self, sha, step, result):
        text = self.layout.template.replace(self.layout.template_sha, sha)
        text = text.replace("precheck", step).replace("none", result)
        self.marker.write_text(text)
        return text

    def run(self, extra=None):
        vars_ = declared_vars(ALERT.read_text(errors="replace"))
        marker_vars = {n: d for n, d in vars_.items() if "mesbot-deploy" in d}
        assert marker_vars, (
            f"alert script has no environment override for the marker path (FR-19); declared: {vars_}")
        env = dict(os.environ)
        env["MSA_STATE_DIR"] = posix(self.state)
        env["MSA_SUPERVISORCTL"] = posix(self.root / "supctl")
        for n, d in marker_vars.items():
            env[n] = posix(self.marker) if Path(d).name == self.layout.marker_name else posix(self.deploy_dir)
        env.update(extra or {})
        r = subprocess.run([BASH, str(ALERT), "--dry-run"], cwd=str(self.root), env=env,
                           capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        out = (r.stdout or "") + (r.stderr or "")
        return AlertRun(r.returncode, out)


class AlertRun:
    def __init__(self, rc, out):
        self.rc = rc
        self.out = out
        self.publishes = []
        for block in re.split(r"--- DRY RUN: would publish[^\n]*---\n", out)[1:]:
            m = re.search(r"^Subject: (.*)$", block, re.M)
            self.publishes.append({"subject": m.group(1).strip() if m else "", "body": block})

    def subjects(self):
        return [p["subject"] for p in self.publishes]

    def deploy_publishes(self):
        return [p for p in self.publishes if "deploy" in p["subject"]]
