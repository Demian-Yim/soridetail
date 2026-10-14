"""신뢰할 수 없는 바이트를 해석할 '상자' — Daytona 샌드박스(기본)와 서버 안 격리 프로세스(대체).

남의 HTML·이미지·카메라 프레임을 디코딩하는 일은 서버 본체에서 절대 하지 않는다.
Daytona 샌드박스를 못 만들면(키 만료·장애) 같은 스크립트를 서버 안 격리 프로세스로 돌린다.

대체 상자(리눅스 = Cloud Run, 서버가 root 로 돌 때)
  - 상자마다 처음 쓰는 uid 로 실행한다 → 서버 환경변수(/proc/<pid>/environ)도, 다른 요청의 상자도 못 읽는다.
  - 네트워크 네임스페이스를 끊는다. 끊을 수 없으면 대체 상자를 열지 않는다(fail-closed).
  - 환경변수 전부 제거, 메모리·CPU·파일 크기·프로세스 수 상한, 출력은 파일로만, 작업 폴더 용량을 실행 중 감시.
  - 작업이 끝나면 그 uid 의 프로세스를 전부 죽이고, 그 uid 가 /tmp 등에 남긴 파일까지 지운다.
  - 서버는 자식이 죽은 뒤에만 결과 파일을 읽는다. 일반 파일만, 상자 안 경로만, 정해진 크기까지만.
윈도우 개발 PC 에서는 별도 uid·네임스페이스가 없어 'process-only' 로만 돈다(운영 대상 아님).
새 키가 들어오면 다음 요청부터 자동으로 Daytona 로 돌아간다.
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

LOCAL_TIMEOUT_SECONDS = 120
LOCAL_MEMORY_BYTES = 700_000_000
LOCAL_CPU_SECONDS = 90
LOCAL_FILE_BYTES = 30_000_000
LOCAL_NPROC = 4
LOCAL_DIR_BYTES = 150_000_000       # Cloud Run 의 /tmp 는 메모리다
OUTPUT_READ_BYTES = 64_000
SLOT_WAIT_SECONDS = 30
DAYTONA_COOLDOWN_SECONDS = 60
ERROR_TTL_SECONDS = 300
UID_RANGE = (20000, 60000)
SHARED_TMP_DIRS = ("/tmp", "/var/tmp", "/dev/shm")
# Daytona 쪽 안전망: 우리가 지우지 못해도(재시작·삭제 실패) 15분 쉬면 멈추고, 멈추면 바로 지워진다. [미검증: 키 만료로 실측 못 함]
DAYTONA_BACKSTOP = {"auto_stop_interval": 15, "auto_delete_interval": 0}

LAUNCHER = (
    "import sys\n"
    "try:\n"
    " import resource as r\n"
    " for k, v in ((r.RLIMIT_AS, %d), (r.RLIMIT_CPU, %d), (r.RLIMIT_FSIZE, %d),"
    " (r.RLIMIT_NPROC, %d), (r.RLIMIT_NOFILE, 64), (r.RLIMIT_CORE, 0)):\n"
    "  r.setrlimit(k, (v, v))\n"
    "except ImportError:\n"
    " pass\n"
    "import runpy\n"
    "sys.argv = sys.argv[1:]\n"
    "runpy.run_path(sys.argv[0], run_name='__main__')\n"
) % (LOCAL_MEMORY_BYTES, LOCAL_CPU_SECONDS, LOCAL_FILE_BYTES, LOCAL_NPROC)

KILL_ALL_OF_MY_UID = "import os, signal\ntry:\n os.kill(-1, signal.SIGKILL)\nexcept ProcessLookupError:\n pass\n"


class LocalBoxUnavailable(RuntimeError):
    """이 서버에서는 대체 상자를 안전하게 열 수 없다."""


class Busy(RuntimeError):
    """대체 상자가 다른 작업 중이다."""


_slot = threading.BoundedSemaphore(1)
_state_lock = threading.Lock()
_error: tuple[float, str] = (0.0, "")
_daytona_skip_until = 0.0
_live_uids: set[int] = set()
_mode: str | None = None


def make_client() -> Daytona:
    api_key = os.getenv("DAYTONA_API_KEY")
    if not api_key:
        raise RuntimeError("DAYTONA_API_KEY 가 없습니다. .env 파일에 키를 넣어 주세요.")
    return Daytona(DaytonaConfig(api_key=api_key))


def create_sandbox(network: bool):
    params = CreateSandboxFromSnapshotParams(network_block_all=not network, **DAYTONA_BACKSTOP)
    return make_client().create(params)


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
        data = self._sandbox.fs.download_file(path)
        if len(data) > max_bytes:
            raise ValueError("결과 파일이 너무 큽니다.")
        return data

    def run_job(self, args: list[str], timeout: int) -> RunResult:
        run = self._sandbox.process.exec("python " + " ".join(shlex.quote(a) for a in args), timeout=timeout)
        return RunResult(run.exit_code, run.result or "")

    def close(self) -> None:
        try:
            self._sandbox.delete()
        except Exception as exc:  # 정리 실패가 결과·원래 오류를 가리지 않게. 남은 것은 DAYTONA_BACKSTOP 이 지운다
            print(f"[job_box] 샌드박스 삭제 실패: {type(exc).__name__}: {exc}")


class LocalBox:
    where = "local"

    def __init__(self):
        mode = isolation_mode()
        if mode is None:
            raise LocalBoxUnavailable("이 서버에서는 격리 프로세스를 만들 수 없습니다.")
        self._mode = mode
        self._uid = _claim_uid() if mode != "process-only" else None
        self._root = Path(tempfile.mkdtemp(prefix="sori_job_"))
        try:
            if self._uid is not None:
                os.chown(self._root, self._uid, self._uid)
        except Exception:
            self.close()
            raise
        self.id = "local-" + self._root.name
        self.home = str(self._root / "sori")

    @property
    def root(self) -> Path:
        return self._root

    def mkdirs(self, *paths: str) -> None:
        for p in paths:
            Path(p).mkdir(parents=True, exist_ok=True)
            self._own(Path(p))

    def upload(self, data: bytes, path: str) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._own(target.parent)
        target.write_bytes(data)
        self._own(target)

    def download(self, path: str, max_bytes: int) -> bytes:
        """자식이 만든 파일을 읽는다. 상자 밖·링크·FIFO·장치·과대 파일은 거부한다."""
        target = Path(path)
        current = self._root
        for part in target.relative_to(self._root).parts:   # 상자 밖 경로면 여기서 ValueError
            current = current / part
            if current.is_symlink():
                raise ValueError("결과 파일 경로가 올바르지 않습니다.")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(target, flags)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("결과 파일이 일반 파일이 아닙니다.")
            with os.fdopen(fd, "rb", closefd=False) as f:
                data = f.read(max_bytes + 1)
        finally:
            os.close(fd)
        if len(data) > max_bytes:
            raise ValueError("결과 파일이 너무 큽니다.")
        return data

    def run_job(self, args: list[str], timeout: int) -> RunResult:
        if not _slot.acquire(timeout=SLOT_WAIT_SECONDS):
            raise Busy("잠시 뒤 다시 시도해 주세요.")
        try:
            return self._run(args, min(timeout, LOCAL_TIMEOUT_SECONDS))
        finally:
            _slot.release()

    def _run(self, args: list[str], timeout: int) -> RunResult:
        out_path = self._root / ".job_output"
        cmd, kwargs = _command(self._mode, self._uid, [sys.executable, "-I", "-c", LAUNCHER, *args])
        deadline, code, reason = time.time() + timeout, None, ""
        with open(out_path, "wb") as out:
            proc = subprocess.Popen(cmd, cwd=self._root, env=_clean_env(), stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=subprocess.STDOUT, start_new_session=True, **kwargs)
            try:
                while code is None:
                    try:
                        code = proc.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        if time.time() > deadline:
                            reason, code = "시간 초과", 124
                        elif _dir_size(self._root) > LOCAL_DIR_BYTES:
                            reason, code = "작업 폴더 용량 초과", 125
            finally:
                _kill_group(proc)
                self._kill_uid()          # 백그라운드로 빠져나간 자식까지
        if reason:
            return RunResult(code, "JOB_FAIL " + reason)
        return RunResult(code, _read_head(out_path))

    def close(self) -> None:
        self._kill_uid()
        shutil.rmtree(self._root, ignore_errors=True)
        if self._root.exists():
            print(f"[job_box] 임시 폴더를 다 지우지 못함: {self._root}")
        if self._uid is not None:
            _remove_owned_by(self._uid)
            with _state_lock:
                _live_uids.discard(self._uid)

    def _own(self, path: Path) -> None:
        if self._uid is not None:
            os.chown(path, self._uid, self._uid)

    def _kill_uid(self) -> None:
        if self._uid is None:
            return
        try:
            subprocess.run([sys.executable, "-I", "-c", KILL_ALL_OF_MY_UID], env=_clean_env(), timeout=10,
                           user=self._uid, group=self._uid, extra_groups=[], capture_output=True)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"[job_box] uid {self._uid} 프로세스 정리 실패: {exc}")


def open_box(network: bool = False):
    """Daytona 샌드박스를 먼저 시도하고, 못 만들면(또는 최근 실패 직후면) 격리 프로세스 상자를 연다."""
    if daytona_allowed():
        try:
            box = DaytonaBox(create_sandbox(network))
            record_error(None)
            return box
        except Exception as exc:
            daytona_failed(exc)
            print(f"[job_box] 샌드박스 생성 실패 → 서버 안 격리 프로세스로 대체: {type(exc).__name__}: {exc}")
    return LocalBox()


def isolation_mode() -> str | None:
    """대체 상자 격리 방식. None 이면 열지 않는다. 처음 한 번 확인해 기억한다."""
    global _mode
    if _mode is None:
        _mode = _probe_mode() or ""
    return _mode or None


def _probe_mode() -> str | None:
    if not sys.platform.startswith("linux"):
        return "process-only"                     # 윈도우 개발 PC
    if os.geteuid() != 0:
        return "process-only" if os.getenv("ALLOW_UNSAFE_LOCAL") == "1" else None
    uid = UID_RANGE[0] - 1
    for mode in ("netns-setpriv", "netns-userns"):
        cmd, kwargs = _command(mode, uid, ["true"])
        try:
            if subprocess.run(cmd, env=_clean_env(), capture_output=True, timeout=10, **kwargs).returncode == 0:
                return mode
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def _command(mode: str, uid: int | None, argv: list[str]) -> tuple[list[str], dict]:
    if mode == "netns-setpriv":    # root 가 네트워크를 끊고, 그다음 uid 로 내려간다 — 자식은 어떤 권한도 없다
        return ["unshare", "-n", "setpriv", f"--reuid={uid}", f"--regid={uid}", "--clear-groups",
                "--no-new-privs", *argv], {}
    if mode == "netns-userns":     # 먼저 uid 로 내려간 뒤, 사용자 네임스페이스로 네트워크를 끊는다
        return ["unshare", "-rn", *argv], {"user": uid, "group": uid, "extra_groups": []}
    return argv, {}


def self_check() -> str:
    """대체 상자 안에서 서버 환경변수 읽기·메타데이터 서버 접속이 실제로 막히는지 확인한다. 결과는 로그로."""
    if not sys.platform.startswith("linux"):
        return "skipped"
    script = (
        "import os, socket\n"
        "try:\n    open('/proc/%d/environ' % os.getppid(), 'rb').read(1); proc = 'OPEN'\n"
        "except Exception:\n    proc = 'blocked'\n"
        "s = socket.socket(); s.settimeout(2)\n"
        "try:\n    s.connect(('169.254.169.254', 80)); net = 'OPEN'\n"
        "except Exception:\n    net = 'blocked'\n"
        "print('CHECK proc=%s net=%s' % (proc, net))\n"
    )
    try:
        box = LocalBox()
    except LocalBoxUnavailable:
        return "unavailable"
    try:
        box.upload(script.encode(), f"{box.home}/check.py")
        out = box.run_job([f"{box.home}/check.py"], 20).result
        return out.split("CHECK ", 1)[1].strip()[:60] if "CHECK " in out else "failed"
    finally:
        box.close()


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
            os.killpg(proc.pid, signal.SIGKILL)
        elif proc.poll() is None:
            proc.kill()
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _remove_owned_by(uid: int) -> None:
    """자식이 상자 밖 공용 임시 폴더에 남긴 파일을 지운다(메모리를 계속 붙잡지 않게)."""
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


def _read_head(path: Path) -> str:
    try:
        with open(path, "rb") as f:
            return f.read(OUTPUT_READ_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _dir_size(root: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total
