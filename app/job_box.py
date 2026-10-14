"""신뢰할 수 없는 바이트를 해석할 '상자' — Daytona 샌드박스(기본)와 서버 안 격리 프로세스(대체).

남의 HTML·이미지·카메라 프레임을 디코딩하는 일은 서버 본체에서 절대 하지 않는다.
Daytona 샌드박스를 못 만들면(키 만료·장애) 같은 스크립트를 서버 안 격리 프로세스로 돌린다.

대체 상자 — 'sealed' 방식(리눅스·root, Cloud Run 기준)
  - 상자마다 용량 제한 메모리 디스크(tmpfs)를 새로 붙인다. 자식이 쓸 수 있는 곳은 여기와 작은 /tmp 뿐이다.
  - 자식은 새 네트워크·마운트·프로세스 네임스페이스 안에서 돈다: 인터넷 없음, 루트 파일시스템 읽기 전용,
    /tmp·/var/tmp·/dev/shm 은 상자 전용 작은 tmpfs, /proc 에는 자기 프로세스만 보인다.
  - 그 안에서 상자마다 처음 쓰는 uid 로 내려가고(권한·능력 전부 제거), 우선순위를 낮춘다.
  - 작업이 끝나면 프로세스 네임스페이스째 죽이고, 서버는 그다음에만 결과를 읽는다.
    일반 파일만, 상자 안·그 uid 소유·링크 없는 경로만, 정해진 크기까지만.
  - 끝나면 상자 tmpfs 를 떼어 내므로 남는 것이 없다.
sealed 를 못 쓰면 'netns-setpriv'(네트워크 차단 + uid)로 내려가고, 그것도 안 되면 대체 상자를 열지 않는다.
윈도우 개발 PC 에서는 'process-only' 로만 돈다(운영 대상 아님).
"""
import os
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from daytona import CreateSandboxFromSnapshotParams, Daytona, DaytonaConfig

BOX_BASE = "/srv/boxes"                 # Dockerfile 이 만든다(리눅스)
BOX_TMPFS_SIZE = "160m"                 # 상자 하나가 쓸 수 있는 최대 용량(입력 포함)
LOCAL_MEMORY_BYTES = 400_000_000   # 실행 2개 × 400MB + 상자 3 × 184MB + 서버 본체가 2GiB 안에
LOCAL_CPU_SECONDS = 60
LOCAL_FILE_BYTES = 8_000_000            # 자식이 만드는 파일 1개 상한(타일·결과·프레임 사본)
LOCAL_NPROC = 4
LOCAL_DIR_BYTES = 160_000_000           # sealed 가 아닐 때만 쓰는 감시 상한
LOCAL_DIR_ENTRIES = 2_000
OUTPUT_READ_BYTES = 64_000
LOCAL_SLOTS = 2                         # 동시에 '실행' 중인 자식 수
BOX_LIMIT = 3                           # 동시에 '살아 있는' 상자 수 — 3 × 최대 약 184MB 가 2GiB 안에 넉넉히
SLOT_WAIT_SECONDS = 3
BOX_INODES = 4096                       # 빈 파일·폴더를 끝없이 만들어 메모리를 채우지 못하게
DAYTONA_COOLDOWN_SECONDS = 60
ERROR_TTL_SECONDS = 300
PROBE_RETRY_SECONDS = 300
UID_RANGE = (20000, 60000)
SHARED_TMP_DIRS = ("/tmp", "/var/tmp", "/dev/shm")
# Daytona 쪽 안전망: 우리가 지우지 못해도(재시작·삭제 실패) 15분 쉬면 멈추고, 멈추면 바로 지워진다. [미검증: 키 만료로 실측 못 함]
DAYTONA_BACKSTOP = {"auto_stop_interval": 15, "auto_delete_interval": 0}

LAUNCHER = (
    "import os, sys\n"
    "try:\n"
    " import resource as r\n"
    " for k, v in ((r.RLIMIT_AS, %d), (r.RLIMIT_CPU, %d), (r.RLIMIT_FSIZE, %d),"
    " (r.RLIMIT_NPROC, %d), (r.RLIMIT_NOFILE, 64), (r.RLIMIT_CORE, 0)):\n"
    "  r.setrlimit(k, (v, v))\n"
    " os.nice(10)\n"
    "except (ImportError, AttributeError):\n"
    " pass\n"
    "import runpy\n"
    "sys.argv = sys.argv[1:]\n"
    "runpy.run_path(sys.argv[0], run_name='__main__')\n"
) % (LOCAL_MEMORY_BYTES, LOCAL_CPU_SECONDS, LOCAL_FILE_BYTES, LOCAL_NPROC)

# sealed: 새 마운트 네임스페이스 안에서 루트를 읽기 전용으로 바꾸고, 쓰기 가능한 임시 폴더를 작게 새로 붙인 뒤 uid 로 내려간다.
SEAL_SCRIPT = (
    "set -e\n"
    "mount -o remount,bind,ro /\n"
    "mount -t tmpfs -o size=16m,nr_inodes=256,mode=1777,nosuid,nodev tmpfs /tmp\n"
    "mount -t tmpfs -o size=4m,nr_inodes=64,mode=1777,nosuid,nodev tmpfs /var/tmp\n"
    "mount -t tmpfs -o size=4m,nr_inodes=64,mode=1777,nosuid,nodev tmpfs /dev/shm\n"
    "uid=$1; box=$2; shift 2\n"
    # 다른 상자의 tmpfs 는 이 네임스페이스에서 떼어 낸다 — 서버가 지운 상자의 메모리를 붙잡지 않게
    "for m in /srv/boxes/box_*; do [ \"$m\" = \"$box\" ] || umount -l \"$m\" 2>/dev/null || true; done\n"
    "exec setpriv --reuid=\"$uid\" --regid=\"$uid\" --clear-groups --no-new-privs "
    "--inh-caps=-all --bounding-set=-all \"$@\"\n"
)

# 그 uid 의 프로세스를 모두 끝낸다. 셸 내장 kill 이라 파이썬 기동(부하 시 수 초)을 기다리지 않는다.
KILL_ALL_OF_MY_UID = ["sh", "-c", "kill -9 -1 2>/dev/null; exit 0"]


class LocalBoxUnavailable(RuntimeError):
    """이 서버에서는 대체 상자를 안전하게 열 수 없다."""


class Busy(RuntimeError):
    """대체 상자가 모두 사용 중이다."""


class Tainted(RuntimeError):
    """자식 프로세스를 확실히 끝내지 못했다 — 결과를 읽지 않는다."""


_slots = threading.BoundedSemaphore(LOCAL_SLOTS)
_boxes = threading.BoundedSemaphore(BOX_LIMIT)
_state_lock = threading.Lock()
_error: tuple[float, str] = (0.0, "")
_daytona_skip_until = 0.0
_live_uids: set[int] = set()
_mode: tuple[float, str | None] | None = None


def make_client() -> Daytona:
    api_key = os.getenv("DAYTONA_API_KEY")
    if not api_key:
        raise RuntimeError("DAYTONA_API_KEY 가 없습니다. .env 파일에 키를 넣어 주세요.")
    return Daytona(DaytonaConfig(api_key=api_key))


def create_sandbox():
    """Daytona 샌드박스는 항상 인터넷을 막고 만든다. 해석에 필요한 pillow 는 기본 이미지에 있다."""
    return make_client().create(CreateSandboxFromSnapshotParams(network_block_all=True, **DAYTONA_BACKSTOP))


def record_error(exc: Exception | None) -> None:
    global _error
    with _state_lock:
        _error = (time.time(), type(exc).__name__) if exc else (0.0, "")


def public_status() -> str:
    """바깥에 보이는 것은 'daytona' 또는 'fallback' 하나뿐이다. 세부는 서버 로그에만."""
    with _state_lock:
        at, kind = _error
    healthy = bool(os.getenv("DAYTONA_API_KEY")) and not (kind and time.time() - at < ERROR_TTL_SECONDS)
    return "daytona" if healthy else "fallback"


def daytona_allowed() -> bool:
    return bool(os.getenv("DAYTONA_API_KEY")) and time.time() >= _daytona_skip_until


def daytona_failed(exc: Exception) -> None:
    global _daytona_skip_until
    record_error(exc)
    _daytona_skip_until = time.time() + DAYTONA_COOLDOWN_SECONDS


@dataclass
class RunResult:
    exit_code: int
    result: str


class DaytonaBox:
    where = "daytona"

    def __init__(self, sandbox):
        self._sandbox = sandbox
        try:
            self.id = sandbox.id
            self.home = sandbox.get_user_home_dir().rstrip("/") + "/sori"
        except Exception:
            self.close()
            raise

    def mkdirs(self, *paths: str) -> None:
        self._sandbox.process.exec("mkdir -p " + " ".join(shlex.quote(p) for p in paths))

    def upload(self, data: bytes, path: str) -> None:
        self._sandbox.fs.upload_file(data, path)

    def download(self, path: str, max_bytes: int) -> bytes:
        info = self._sandbox.fs.get_file_info(path)      # 크기부터 본다 — 받은 뒤에 재면 이미 늦다
        if getattr(info, "is_dir", False) or int(getattr(info, "size", max_bytes + 1)) > max_bytes:
            raise ValueError("결과 파일이 올바르지 않습니다.")
        data = self._sandbox.fs.download_file(path)
        if len(data) > max_bytes:
            raise ValueError("결과 파일이 너무 큽니다.")
        return data

    def run_job(self, args: list[str], timeout: int) -> RunResult:
        inner = ("python " + " ".join(shlex.quote(a) for a in args) +
                 f" > /tmp/.job_out 2>&1; c=$?; head -c {OUTPUT_READ_BYTES} /tmp/.job_out; exit $c")
        run = self._sandbox.process.exec("sh -c " + shlex.quote(inner), timeout=timeout)
        return RunResult(run.exit_code, (run.result or "")[:OUTPUT_READ_BYTES])

    def close(self) -> None:
        delete_quietly(self._sandbox)


class LocalBox:
    where = "local"

    def __init__(self):
        mode = isolation_mode()
        if mode is None:
            raise LocalBoxUnavailable("이 서버에서는 격리 프로세스를 만들 수 없습니다.")
        if not _boxes.acquire(timeout=SLOT_WAIT_SECONDS):
            raise Busy("지금 요청이 많습니다. 잠시 뒤 다시 시도해 주세요.")
        self._holds_box = True
        self._mode = mode
        self._uid = _claim_uid() if mode != "process-only" else None
        self._mount_attempted = False
        try:
            if mode == "sealed":
                self._root = Path(tempfile.mkdtemp(prefix="box_", dir=BOX_BASE))
                self._mount_attempted = True
                subprocess.run(["mount", "-t", "tmpfs", "-o",
                                f"size={BOX_TMPFS_SIZE},nr_inodes={BOX_INODES},mode=0711,nosuid,nodev",
                                "tmpfs", str(self._root)], check=True, capture_output=True, timeout=10)
            else:
                self._root = Path(tempfile.mkdtemp(prefix="sori_job_"))
                if self._uid is not None:
                    os.chmod(self._root, 0o711)
            self.home = str(self._root / "sori")
            self._make_dir(Path(self.home))
        except Exception:
            self.close()
            raise
        self.id = "local-" + secrets.token_hex(6)

    @property
    def root(self) -> Path:
        return self._root

    def mkdirs(self, *paths: str) -> None:
        for p in paths:
            self._make_dir(Path(p))

    def upload(self, data: bytes, path: str) -> None:
        """root 가 자식 폴더에 쓰므로 링크를 절대 따라가지 않는다. 새 파일로만 만든다."""
        target = Path(path)
        self._check_inside(target.parent, allow_missing=True)
        self._make_dir(target.parent)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(target, flags, 0o600)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            if self._uid is not None:
                os.fchown(fd, self._uid, self._uid)
        finally:
            os.close(fd)

    def download(self, path: str, max_bytes: int) -> bytes:
        """자식이 만든 파일을 읽는다. 상자 밖·링크·FIFO·장치·하드링크·남의 소유·과대 파일은 거부한다."""
        target = Path(path)
        self._check_inside(target)
        return _safe_read(target, max_bytes, self._uid)

    def run_job(self, args: list[str], timeout: int) -> RunResult:
        if not _slots.acquire(timeout=SLOT_WAIT_SECONDS):
            raise Busy("지금 요청이 많습니다. 잠시 뒤 다시 시도해 주세요.")
        try:
            return self._run(args, timeout)
        finally:
            _slots.release()

    def _run(self, args: list[str], timeout: int) -> RunResult:
        out_path = self._root / ".job_output"        # 자식이 쓸 수 없는 root 소유 폴더에 둔다(바꿔치기 불가)
        cmd, kwargs = _command(self._mode, self._uid, [sys.executable, "-I", "-c", LAUNCHER, *args], str(self._root))
        deadline, code, reason = time.time() + timeout, None, ""
        fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            proc = subprocess.Popen(cmd, cwd=self.home, env=_clean_env(), stdin=subprocess.DEVNULL,
                                    stdout=fd, stderr=subprocess.STDOUT, start_new_session=True, **kwargs)
        finally:
            os.close(fd)
        try:
            while code is None:
                try:
                    code = proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    if time.time() > deadline:
                        reason, code = "시간 초과", 124
                    elif self._mode != "sealed" and _over_budget(self._root):
                        reason, code = "작업 폴더 용량 초과", 125
        finally:
            _kill_group(proc)
            self._kill_uid(strict=True)       # 확실히 끝났을 때만 결과를 읽는다
        if reason:
            return RunResult(code, "JOB_FAIL " + reason)
        return RunResult(code, _safe_read(out_path, OUTPUT_READ_BYTES, None, truncate=True).decode("utf-8", "replace"))

    def close(self) -> None:
        """어느 단계가 실패해도 상자 자리(semaphore)와 uid 는 반드시 돌려준다."""
        try:
            for step in (lambda: self._kill_uid(strict=False), self._release_storage, self._release_uid):
                try:
                    step()
                except Exception as exc:
                    print(f"[job_box] 상자 정리 중 오류(계속 진행): {type(exc).__name__}: {exc}")
        finally:
            if getattr(self, "_holds_box", False):
                self._holds_box = False
                _boxes.release()

    def _release_storage(self) -> None:
        root = getattr(self, "_root", None)
        if root is None:
            return
        if getattr(self, "_mount_attempted", False):   # mount 가 시간 초과로 끝났어도 붙어 있을 수 있다
            try:
                subprocess.run(["umount", "-l", str(root)], capture_output=True, timeout=10)
            except (OSError, subprocess.SubprocessError) as exc:
                print(f"[job_box] umount 실패: {exc}")
        shutil.rmtree(root, ignore_errors=True)
        if root.exists():
            print(f"[job_box] 임시 폴더를 다 지우지 못함: {root}")

    def _release_uid(self) -> None:
        if self._uid is None:
            return
        if self._mode != "sealed":
            _remove_owned_by(self._uid)   # sealed 는 자식의 /tmp 가 따로라 남는 것이 없다
        with _state_lock:
            _live_uids.discard(self._uid)

    def _make_dir(self, path: Path) -> None:
        self._check_inside(path, allow_missing=True)
        if not path.exists():
            path.mkdir(mode=0o700)
            if self._uid is not None:
                os.chown(path, self._uid, self._uid, follow_symlinks=False)

    def _check_inside(self, target: Path, allow_missing: bool = False) -> None:
        current = self._root
        for part in target.relative_to(self._root).parts:   # 상자 밖 경로면 여기서 ValueError
            current = current / part
            if current.is_symlink():
                raise ValueError("상자 안 경로가 올바르지 않습니다.")
            if not current.exists() and not allow_missing:
                raise ValueError("결과 파일이 없습니다.")

    def _kill_uid(self, strict: bool) -> None:
        if self._uid is None:
            return
        last_error = None
        for _attempt in range(2):
            try:
                subprocess.run(KILL_ALL_OF_MY_UID, env=_clean_env(), timeout=15, user=self._uid, group=self._uid,
                               extra_groups=[], capture_output=True, check=False)
                return
            except (OSError, subprocess.SubprocessError) as exc:
                last_error = exc
        print(f"[job_box] uid {self._uid} 프로세스 정리 실패: {last_error}")
        if strict:
            raise Tainted("작업을 안전하게 끝내지 못했습니다.") from last_error


def delete_quietly(sandbox) -> None:
    try:
        sandbox.delete()
    except Exception as exc:  # 정리 실패가 결과·원래 오류를 가리지 않게. 남은 것은 DAYTONA_BACKSTOP 이 지운다
        print(f"[job_box] 샌드박스 삭제 실패: {type(exc).__name__}: {exc}")


def open_box():
    """Daytona 샌드박스를 먼저 시도하고, 못 만들면(또는 최근 실패 직후면) 격리 프로세스 상자를 연다."""
    if daytona_allowed():
        try:
            box = DaytonaBox(create_sandbox())
            record_error(None)
            return box
        except Exception as exc:
            daytona_failed(exc)
            print(f"[job_box] 샌드박스 생성 실패 → 서버 안 격리 프로세스로 대체: {type(exc).__name__}: {exc}")
    return LocalBox()


def isolation_mode() -> str | None:
    """대체 상자 격리 방식. None 이면 열지 않는다. 실패는 잠시 뒤 다시 확인한다."""
    global _mode
    if _mode is None or (_mode[1] is None and time.time() - _mode[0] > PROBE_RETRY_SECONDS):
        _mode = (time.time(), _probe_mode())
    return _mode[1]


def _probe_mode() -> str | None:
    if not sys.platform.startswith("linux"):
        return "process-only"                     # 윈도우 개발 PC
    if os.geteuid() != 0:
        return "process-only" if os.getenv("ALLOW_UNSAFE_LOCAL") == "1" else None
    if _probe_sealed():
        return "sealed"
    cmd, kwargs = _command("netns-setpriv", UID_RANGE[0] - 1, ["true"])
    try:
        if subprocess.run(cmd, env=_clean_env(), capture_output=True, timeout=10, **kwargs).returncode == 0:
            return "netns-setpriv"
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _probe_sealed() -> bool:
    if not os.path.isdir(BOX_BASE):
        return False
    probe = Path(tempfile.mkdtemp(prefix="probe_", dir=BOX_BASE))
    mounted = False
    try:
        mounted = subprocess.run(["mount", "-t", "tmpfs", "-o", "size=1m,mode=0711", "tmpfs", str(probe)],
                                 capture_output=True, timeout=10).returncode == 0
        if not mounted:
            return False
        cmd, kwargs = _command("sealed", UID_RANGE[0] - 1, ["true"], str(probe))
        return subprocess.run(cmd, env=_clean_env(), capture_output=True, timeout=10, **kwargs).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        if mounted:
            subprocess.run(["umount", "-l", str(probe)], capture_output=True, timeout=10)
        shutil.rmtree(probe, ignore_errors=True)


def _command(mode: str, uid: int | None, argv: list[str], box: str = "") -> tuple[list[str], dict]:
    if mode == "sealed":
        return ["unshare", "-n", "-m", "-p", "--fork", "--mount-proc", "--propagation", "private",
                "sh", "-c", SEAL_SCRIPT, "seal", str(uid), box, *argv], {}
    if mode == "netns-setpriv":
        return ["unshare", "-n", "setpriv", f"--reuid={uid}", f"--regid={uid}", "--clear-groups",
                "--no-new-privs", "--inh-caps=-all", "--bounding-set=-all", *argv], {}
    return argv, {}


def self_check() -> str:
    """대체 상자 안에서 막혀야 할 것이 실제로 막히는지 확인한다. 결과는 로그로만."""
    if not sys.platform.startswith("linux"):
        return "skipped"
    script = (
        "import os, socket\n"
        "def tried(f):\n"
        "    try:\n        f(); return 'OPEN'\n"
        "    except Exception:\n        return 'blocked'\n"
        "def net():\n    s = socket.socket(); s.settimeout(2); s.connect(('169.254.169.254', 80))\n"
        "def root_write():\n    open('/srv/app_write_probe', 'w').write('x')\n"
        "def big_tmp():\n    open('/tmp/fill', 'wb').write(b'0' * 40_000_000)\n"
        "import sys\n"
        "server = int(sys.argv[1])\n"
        "def many_files():\n    [open('/tmp/f%d' % i, 'w').close() for i in range(5000)]\n"
        # 서버 환경이 보이는지는 '서버만 가진 변수 이름'이 어느 /proc/*/environ 에든 나타나는지로 판정한다.
        # (서버가 1번 프로세스라 새 프로세스 공간의 1번 = 자식 자신이므로 pid 로 열어 보는 것은 의미가 없다)
        "def server_env():\n"
        "    import glob\n"
        "    for path in glob.glob('/proc/[0-9]*/environ'):\n"
        "        try:\n"
        "            if b'GEMINI_API_KEY=' in open(path, 'rb').read():\n"
        "                return\n"
        "        except Exception:\n"
        "            pass\n"
        "    raise PermissionError('not visible')\n"
        "print('CHECK server_env=%s net=%s rootfs=%s tmp40m=%s files5000=%s' % (\n"
        "    tried(server_env), tried(net),\n"
        "    tried(root_write), tried(big_tmp), tried(many_files)))\n"
    )
    try:
        box = LocalBox()
    except LocalBoxUnavailable:
        return "unavailable"
    try:
        box.upload(script.encode(), f"{box.home}/check.py")
        out = box.run_job([f"{box.home}/check.py", str(os.getpid())], 20).result
        return out.split("CHECK ", 1)[1].strip()[:120] if "CHECK " in out else "failed"
    finally:
        box.close()


def _safe_read(path: Path, max_bytes: int, owner: int | None, truncate: bool = False) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("결과 파일이 일반 파일이 아닙니다.")
        if owner is not None and (info.st_uid != owner or info.st_nlink != 1):
            raise ValueError("결과 파일의 주인이 올바르지 않습니다.")
        with os.fdopen(fd, "rb", closefd=False) as f:
            data = f.read(max_bytes + 1)
    finally:
        os.close(fd)
    if len(data) > max_bytes:
        if truncate:
            return data[:max_bytes]
        raise ValueError("결과 파일이 너무 큽니다.")
    return data


def _claim_uid() -> int:
    with _state_lock:
        while True:
            uid = UID_RANGE[0] + secrets.randbelow(UID_RANGE[1] - UID_RANGE[0])
            if uid not in _live_uids:
                _live_uids.add(uid)
                return uid


def _clean_env() -> dict:
    """자식 프로세스에 넘기는 환경. 키·토큰은 하나도 넘기지 않는다."""
    keep = {"SYSTEMROOT", "TEMP", "TMP", "PATH", "LANG", "LC_ALL"}   # SYSTEMROOT·TEMP 는 윈도우 파이썬 기동에 필요
    env = {k: v for k, v in os.environ.items() if k.upper() in keep}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)   # sealed 는 이것으로 프로세스 네임스페이스 전체가 끝난다
        elif proc.poll() is None:
            proc.kill()
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _remove_owned_by(uid: int) -> None:
    for top in SHARED_TMP_DIRS:
        try:
            entries = list(os.scandir(top))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.stat(follow_symlinks=False).st_uid != uid:
                    continue
                if entry.is_dir(follow_symlinks=False):
                    shutil.rmtree(entry.path, ignore_errors=True)
                else:
                    os.unlink(entry.path)
            except OSError:
                pass


def _over_budget(root: Path) -> bool:
    total, count = 0, 0
    for dirpath, dirs, files in os.walk(root):
        count += len(dirs) + len(files)
        if count > LOCAL_DIR_ENTRIES:
            return True
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total > LOCAL_DIR_BYTES
