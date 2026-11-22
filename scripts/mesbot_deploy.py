#!/usr/bin/env python3
"""mesbot daily deploy job (F-004): fast-forward /home/mesbot to the newest `main` and restart.

Run by cron as `mesbot` at 17:45 UTC (scripts/mesbot-deploy.cron), never as root. It takes no
arguments. The server pulls: `main` is fetched from the public GitHub repo over HTTPS with no
credential, so nothing outside connects in and nothing on GitHub can reach the server.

The job does not check CI itself. The CI gate is `main`'s branch protection requiring the
factory-ci checks, so the cron file must not be installed before mesbot is beta.

One run, in order:
  1. take the deploy lock (shared with deploy.sh), or skip;
  2. skip on a "rollback failed" marker or an invalid marker (left for the owner to delete);
  3. fetch `main`; on failure change nothing and wait for tomorrow (no marker, so no page);
  4. skip if there is no new commit, or `main` still points at the commit a marker says failed;
  5. precheck: tracked local changes or a non-fast-forward fail before anything changes;
  6. fast-forward, `pip install --require-hashes -r requirements.lock` into the venv, restart
     discord_app then flask_app, health check. On failure roll back to the previous commit and
     its lock file, restart, re-check, and record the outcome in the marker.

Exit 0 on deploy or skip, 1 on any failure. Each run prints exactly one line, which cron appends
to /var/lib/mesbot-deploy/deploy.log. Subprocess output is discarded on purpose: pip and git
can echo environment or URL details, and the log must hold no tokens or .env values.

Files in the state directory (default /var/lib/mesbot-deploy, owner mesbot, mode 0750):
  deploy.lock  flock(2) lock; deploy.sh takes the same lock with `flock -n`.
  deploy.log   the cron output.
  marker       written atomically (temp file + rename), exactly these four lines:
                   commit=<40 lowercase hex>
                   step=precheck|install|restart|healthcheck
                   rollback=succeeded|failed|none
                   timestamp=YYYY-MM-DDTHH:MM:SSZ
               The root alert script reads it as untrusted input. Anything else in the file,
               a symlink, or a non-regular file makes the marker invalid.

Overrides, for tests that run the job unprivileged with a local remote and stubs:
  MESBOT_DEPLOY_REPO_DIR        checkout             (default /home/mesbot)
  MESBOT_DEPLOY_REMOTE_URL      git remote to fetch  (default the public HTTPS repo URL)
  MESBOT_DEPLOY_VENV_PYTHON     venv interpreter     (default /home/mesbot/.venv/bin/python)
  MESBOT_DEPLOY_STATE_DIR       marker, lock, log    (default /var/lib/mesbot-deploy)
  MESBOT_DEPLOY_SUPERVISORCTL   supervisorctl command, split on whitespace
                                                     (default "sudo -n supervisorctl")
  MESBOT_DEPLOY_POLL_SECS       health-check poll interval   (default 10)
  MESBOT_DEPLOY_TIMEOUT_SECS    health-check limit           (default 120)
  MESBOT_DEPLOY_STABLE_SECS     health-check stability time  (default 30)
"""

import datetime
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time

try:
    import fcntl
except ImportError:  # Windows: the job only runs on Linux, but keep the module importable.
    fcntl = None

DEFAULT_REPO_DIR = "/home/mesbot"
DEFAULT_REMOTE_URL = "https://github.com/MindsEyeSociety/mesbot.git"
DEFAULT_VENV_PYTHON = "/home/mesbot/.venv/bin/python"
DEFAULT_STATE_DIR = "/var/lib/mesbot-deploy"
DEFAULT_SUPERVISORCTL = "sudo -n supervisorctl"
DEFAULT_POLL_SECS = 10.0
DEFAULT_TIMEOUT_SECS = 120.0
DEFAULT_STABLE_SECS = 30.0

PROGRAMS = ("discord_app", "flask_app")
LOCK_NAME = "deploy.lock"
MARKER_NAME = "marker"

STEPS = ("precheck", "install", "restart", "healthcheck")
ROLLBACK_RESULTS = ("succeeded", "failed", "none")
MARKER_KEYS = ("commit", "step", "rollback", "timestamp")
MARKER_MAX_BYTES = 512

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_STATUS_STATES = frozenset(
    ("RUNNING", "STARTING", "BACKOFF", "STOPPING", "STOPPED", "EXITED", "FATAL", "UNKNOWN")
)

# Generous enough for a slow fetch or a full reinstall, but a hung child must not hold the
# deploy lock forever.
GIT_TIMEOUT_SECS = 300
PIP_TIMEOUT_SECS = 1800
RESTART_TIMEOUT_SECS = 120

EXIT_OK = 0
EXIT_FAILED = 1


class Config:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.repo_dir = env.get("MESBOT_DEPLOY_REPO_DIR") or DEFAULT_REPO_DIR
        self.remote_url = env.get("MESBOT_DEPLOY_REMOTE_URL") or DEFAULT_REMOTE_URL
        self.venv_python = env.get("MESBOT_DEPLOY_VENV_PYTHON") or DEFAULT_VENV_PYTHON
        self.state_dir = env.get("MESBOT_DEPLOY_STATE_DIR") or DEFAULT_STATE_DIR
        self.supervisorctl = shlex.split(
            env.get("MESBOT_DEPLOY_SUPERVISORCTL") or DEFAULT_SUPERVISORCTL
        )
        self.poll_secs = _float_env(env, "MESBOT_DEPLOY_POLL_SECS", DEFAULT_POLL_SECS)
        self.timeout_secs = _float_env(env, "MESBOT_DEPLOY_TIMEOUT_SECS", DEFAULT_TIMEOUT_SECS)
        self.stable_secs = _float_env(env, "MESBOT_DEPLOY_STABLE_SECS", DEFAULT_STABLE_SECS)

    @property
    def lock_path(self):
        return os.path.join(self.state_dir, LOCK_NAME)

    @property
    def marker_path(self):
        return os.path.join(self.state_dir, MARKER_NAME)


def _float_env(env, name, default):
    raw = env.get(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(message):
    print(f"{utc_now()} {message}", flush=True)


# --- marker ---------------------------------------------------------------------------------

class InvalidMarker(Exception):
    """The marker exists but is a symlink, not a regular file, or not in the fixed format."""


def parse_marker(text):
    """Return the marker's fields, or raise InvalidMarker. Accepts only the exact format."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    if len(lines) != len(MARKER_KEYS):
        raise InvalidMarker()
    fields = {}
    for line in lines:
        key, sep, value = line.partition("=")
        if not sep or key not in MARKER_KEYS or key in fields:
            raise InvalidMarker()
        fields[key] = value
    if not _SHA_RE.match(fields["commit"]):
        raise InvalidMarker()
    if fields["step"] not in STEPS or fields["rollback"] not in ROLLBACK_RESULTS:
        raise InvalidMarker()
    if not _TIMESTAMP_RE.match(fields["timestamp"]):
        raise InvalidMarker()
    try:
        datetime.datetime.strptime(fields["timestamp"], "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise InvalidMarker() from None
    return fields


def read_marker(path):
    """Return None when there is no marker, the fields when valid, else raise InvalidMarker.

    The file is checked with lstat and opened with O_NOFOLLOW, so a symlink's target is never
    read, whatever it points at.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode):
        raise InvalidMarker()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError:
        raise InvalidMarker() from None
    try:
        # Re-check on the open descriptor: the path could have been swapped after lstat.
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise InvalidMarker()
        data = os.read(fd, MARKER_MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MARKER_MAX_BYTES:
        raise InvalidMarker()
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise InvalidMarker() from None
    return parse_marker(text)


def format_marker(commit, step, rollback, timestamp):
    fields = parse_marker(
        f"commit={commit}\nstep={step}\nrollback={rollback}\ntimestamp={timestamp}\n"
    )
    return "".join(f"{key}={fields[key]}\n" for key in MARKER_KEYS)


def write_marker(path, commit, step, rollback):
    """Write the marker atomically, so the alert script never reads a half-written file."""
    content = format_marker(commit, step, rollback, utc_now())
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".marker.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o640)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def remove_marker(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


# --- subprocesses ---------------------------------------------------------------------------

def run(argv, cwd=None, timeout=None, env=None):
    """Run a command with its output discarded; True when it exits 0."""
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def git_env():
    env = dict(os.environ)
    # No prompt and no credential helper: the fetch is anonymous, and a prompt would hang cron.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    env["GIT_ASKPASS"] = "true"
    env.pop("SSH_ASKPASS", None)
    return env


def git(cfg, *args, timeout=GIT_TIMEOUT_SECS):
    return ["git", "-c", "credential.helper=", "-C", cfg.repo_dir, *args], timeout


def git_ok(cfg, *args):
    argv, timeout = git(cfg, *args)
    return run(argv, timeout=timeout, env=git_env())


def git_output(cfg, *args):
    argv, timeout = git(cfg, *args)
    try:
        result = subprocess.run(
            argv,
            env=git_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def head_sha(cfg):
    sha = git_output(cfg, "rev-parse", "--verify", "HEAD^{commit}")
    return sha if sha and _SHA_RE.match(sha) else None


def fetch_main(cfg):
    """Fetch `main` from the remote; return its full SHA, or None when the fetch fails."""
    if not git_ok(cfg, "fetch", "--quiet", "--no-tags", "--", cfg.remote_url, "refs/heads/main"):
        return None
    sha = git_output(cfg, "rev-parse", "--verify", "FETCH_HEAD^{commit}")
    return sha if sha and _SHA_RE.match(sha) else None


def has_local_changes(cfg):
    out = git_output(cfg, "status", "--porcelain", "--untracked-files=no")
    # A status that cannot be read is treated as dirty: never deploy over an unknown tree.
    return out is None or out != ""


def is_ancestor(cfg, ancestor, descendant):
    return git_ok(cfg, "merge-base", "--is-ancestor", ancestor, descendant)


def install_lock(cfg):
    return run(
        [cfg.venv_python, "-m", "pip", "install", "--require-hashes", "-r", "requirements.lock"],
        cwd=cfg.repo_dir,
        timeout=PIP_TIMEOUT_SECS,
        env={**os.environ, "PIP_NO_INPUT": "1"},
    )


def restart_programs(cfg):
    for program in PROGRAMS:
        if not run([*cfg.supervisorctl, "restart", program], timeout=RESTART_TIMEOUT_SECS):
            return False
    return True


# --- health check ---------------------------------------------------------------------------

def parse_status(output):
    """Map program name to state, from `supervisorctl status` lines that carry a known state."""
    states = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] in _STATUS_STATES and parts[0] not in states:
            states[parts[0]] = parts[1]
    return states


def all_running(cfg):
    # supervisorctl exits 3 when a program is not RUNNING, so only the parsed lines count.
    try:
        result = subprocess.run(
            [*cfg.supervisorctl, "status"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=max(cfg.poll_secs, 1.0),
            check=False,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    states = parse_status(result.stdout or "")
    return all(states.get(program) == "RUNNING" for program in PROGRAMS)


def _sleep_until(target, clock, sleep):
    while True:
        remaining = target - clock()
        if remaining <= 0:
            return
        sleep(remaining)


def health_check(cfg, check=None, clock=time.monotonic, sleep=time.sleep):
    """Pass once both programs are RUNNING on two checks at least stable_secs apart.

    Polls every poll_secs, measured from the start of the previous poll, until timeout_secs
    after the first poll. A non-RUNNING check resets the stability window, so a program that
    crashes a few seconds after starting is caught.
    """
    check = check or (lambda: all_running(cfg))
    epsilon = 1e-6
    start = clock()
    running_since = None
    poll_at = start
    while True:
        _sleep_until(poll_at, clock, sleep)
        polled = clock()
        if check():
            if running_since is None:
                running_since = polled
            elif polled - running_since >= cfg.stable_secs - epsilon:
                return True
        else:
            running_since = None
        poll_at = polled + cfg.poll_secs
        if poll_at - start > cfg.timeout_secs + epsilon:
            return False


# --- the job --------------------------------------------------------------------------------

def deploy(cfg):
    """One run of the job with the lock held. Returns the exit code; logs exactly one line."""
    try:
        marker = read_marker(cfg.marker_path)
    except InvalidMarker:
        log("skipped: invalid marker")
        return EXIT_OK
    if marker and marker["rollback"] == "failed":
        log("skipped: rollback-failed marker present")
        return EXIT_OK

    target = fetch_main(cfg)
    if target is None:
        log("failed: fetch")
        return EXIT_FAILED

    previous = head_sha(cfg)
    if previous is None:
        log("failed: cannot read HEAD")
        return EXIT_FAILED
    if target == previous:
        log("skipped: no new commit")
        return EXIT_OK
    if marker and marker["commit"] == target:
        log("skipped: failed commit not retried")
        return EXIT_OK

    if has_local_changes(cfg) or not is_ancestor(cfg, previous, target):
        write_marker(cfg.marker_path, target, "precheck", "none")
        log("failed: precheck")
        return EXIT_FAILED

    failed_step = None
    if not git_ok(cfg, "merge", "--ff-only", "--quiet", target) or head_sha(cfg) != target:
        # The precheck proved a fast-forward is possible, so this is unexpected; the rollback
        # reset still restores the previous commit.
        failed_step = "install"
    elif not install_lock(cfg):
        failed_step = "install"
    elif not restart_programs(cfg):
        failed_step = "restart"
    elif not health_check(cfg):
        failed_step = "healthcheck"

    if failed_step is None:
        remove_marker(cfg.marker_path)
        log(f"deployed {target}")
        return EXIT_OK

    # Each rollback step runs only if the one before it succeeded: a failed rollback stops
    # where it is and pages the owner rather than restarting over a half-restored state.
    rolled_back = (
        git_ok(cfg, "reset", "--hard", "--quiet", previous)
        and head_sha(cfg) == previous
        and install_lock(cfg)
        and restart_programs(cfg)
        and health_check(cfg)
    )
    if rolled_back:
        write_marker(cfg.marker_path, target, failed_step, "succeeded")
        log(f"failed: {failed_step}; rolled back to {previous}")
    else:
        write_marker(cfg.marker_path, target, failed_step, "failed")
        log(f"failed: {failed_step}; rollback failed")
    return EXIT_FAILED


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        log("failed: the deploy job takes no arguments")
        return EXIT_FAILED
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None and geteuid() == 0:
        # git would run repo hooks and config with root rights; the job is mesbot-only.
        log("failed: refusing to run as root")
        return EXIT_FAILED
    if fcntl is None:
        log("failed: no flock support on this platform")
        return EXIT_FAILED

    cfg = Config()
    try:
        lock_fd = os.open(
            cfg.lock_path,
            os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            0o640,
        )
    except OSError:
        log("failed: cannot open the deploy lock")
        return EXIT_FAILED
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log("skipped: another deploy is running")
            return EXIT_OK
        try:
            return deploy(cfg)
        except OSError:
            log("failed: state directory not writable")
            return EXIT_FAILED
    finally:
        os.close(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
