"""해석 작업을 돌릴 '상자' 두 종류 — Daytona 샌드박스(기본)와 서버 안 격리 프로세스(대체).

남의 HTML·이미지를 해석하는 sandbox_job.py 는 원래 Daytona 일회용 샌드박스 안에서만 돈다.
키가 만료되거나 Daytona 가 멈추면 서비스가 멈췄기 때문에, 같은 스크립트를 서버 안의
별도 프로세스로 돌리는 대체 상자를 둔다.

대체 상자가 하는 것 (리눅스 = Cloud Run 기준)
  - 권한 없는 별도 사용자(JOB_USER)로 실행한다 → 서버 프로세스의 환경변수(/proc/<pid>/environ)를 못 읽는다.
  - 가능하면 네트워크 네임스페이스를 끊는다(unshare -rn). 안 되면 isolation 에 그대로 드러낸다.
  - 환경변수를 전부 비우고, 메모리·CPU·파일 크기·프로세스 수 상한을 건다. 출력은 파일로만 받는다.
  - 한 번에 1건만 돌리고, 시간이 넘으면 프로세스 그룹째 죽인 뒤 임시 폴더를 지운다.
대체 상자는 Daytona 같은 '별도 컴퓨터'가 아니다. 그래서 어느 쪽이 쓰였는지 응답과 /api/health 에 드러낸다.
자식이 써 낸 파일 이름·목록은 믿지 않는다 — 검증은 sandbox_runner 가 한다.
새 키가 들어오면 다음 요청부터 자동으로 Daytona 로 돌아간다.
"""
import os
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

from daytona import Daytona, DaytonaConfig

JOB_USER = os.getenv("JOB_USER", "sorijob")        # Dockerfile 이 만든다
LOCAL_TIMEOUT_SECONDS = 120
LOCAL_MEMORY_BYTES = 900_000_000    # 동시 1건 + 서버 본체가 2Gi 안에 들어가게
LOCAL_CPU_SECONDS = 90
LOCAL_FILE_BYTES = 30_000_000       # 파일 1개 상한 (출력 파일 포함)
LOCAL_DIR_BYTES = 150_000_000       # 작업 폴더 전체 상한 — Cloud Run 의 /tmp 는 메모리다
OUTPUT_READ_BYTES = 64_000
DAYTONA_COOLDOWN_SECONDS = 60       # Daytona 장애 중 매 요청이 생성 대기로 늦어지지 않게
ERROR_TTL_SECONDS = 300

# 자식 안에서 상한을 건 뒤 스크립트를 실행하는 시동기. preexec_fn(스레드와 함께 쓰면 위험)을 쓰지 않는다.
LAUNCHER = (
    "import sys\n"
    "try:\n"
    " import resource as r\n"
    " for k, v in ((r.RLIMIT_AS, %d), (r.RLIMIT_CPU, %d), (r.RLIMIT_FSIZE, %d),"
    " (r.RLIMIT_NPROC, 16), (r.RLIMIT_NOFILE, 128), (r.RLIMIT_CORE, 0)):\n"
    "  r.setrlimit(k, (v, v))\n"
    "except ImportError:\n"
    " pass\n"
    "import runpy\n"
    "sys.argv = sys.argv[1:]\n"
    "runpy.run_path(sys.argv[0], run_name='__main__')\n"
) % (LOCAL_MEMORY_BYTES, LOCAL_CPU_SECONDS, LOCAL_FILE_BYTES)

_local_slot = threading.BoundedSemaphore(1)
_state_lock = threading.Lock()
_error: tuple[float, str] = (0.0, "")       # (시각, 오류 종류)
_daytona_skip_until = 0.0
_isolation: str | None = None


def make_client() -> Daytona:
    api_key = os.getenv("DAYTONA_API_KEY")
    if not api_key:
        raise RuntimeError("DAYTONA_API_KEY 가 없습니다. .env 파일에 키를 넣어 주세요.")
    return Daytona(DaytonaConfig(api_key=api_key))


def record_error(exc: Exception | None) -> None:
    """마지막 샌드박스 실패. 성공하면 지운다. 화면에는 오류 '종류'만 보인다."""
    global _error
    with _state_lock:
        _error = (time.time(), type(exc).__name__) if exc else (0.0, "")


def sandbox_status() -> dict:
    with _state_lock:
        at, kind = _error
    recent = bool(kind) and time.time() - at < ERROR_TTL_SECONDS
    has_key = bool(os.getenv("DAYTONA_API_KEY"))
    return {"sandbox": "ok" if has_key and not recent else "fallback",
            "sandbox_error": kind if recent else ("NoKey" if not has_key else ""),
            "local_isolation": isolation(), "local_check": self_check()}


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
            self.close()      # 만들어 놓고 못 쓰는 샌드박스를 남기지 않는다
            raise

    def mkdirs(self, *paths: str) -> None:
        self._sandbox.process.exec("mkdir -p " + " ".join(shlex.quote(p) for p in paths))

    def upload(self, data: bytes, path: str) -> None:
        self._sandbox.fs.upload_file(data, path)

    def download(self, path: str) -> bytes:
        return self._sandbox.fs.download_file(path)

    def run_job(self, args: list[str], timeout: int) -> RunResult:
        run = self._sandbox.process.exec("python " + " ".join(shlex.quote(a) for a in args), timeout=timeout)
        return RunResult(run.exit_code, run.result or "")

    def close(self) -> None:
        try:
            self._sandbox.delete()
        except Exception as exc:  # 정리 실패가 원래 결과·오류를 가리지 않게
            print(f"[job_box] 샌드박스 삭제 실패: {type(exc).__name__}: {exc}")


class LocalBox:
    where = "local"

    def __init__(self):
        self._root = Path(tempfile.mkdtemp(prefix="sori_job_"))
        self.id = "local-" + self._root.name
        self.home = str(self._root / "sori")
        self._uid = _job_uid()
        if self._uid is not None:
            os.chown(self._root, self._uid, self._uid)

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

    def download(self, path: str) -> bytes:
        return Path(path).read_bytes()

    def run_job(self, args: list[str], timeout: int) -> RunResult:
        out_path = self._root / ".job_output"
        cmd = [sys.executable, "-I", "-c", LAUNCHER, *args]
        if isolation().startswith("netns"):
            cmd = ["unshare", "-rn", *cmd]
        with _local_slot, open(out_path, "wb") as out:
            proc = subprocess.Popen(cmd, cwd=self._root, env=_clean_env(), stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
                                    **_user_kwargs(self._uid))
            try:
                code = proc.wait(timeout=min(timeout, LOCAL_TIMEOUT_SECONDS))
            except subprocess.TimeoutExpired:
                _kill_group(proc)
                code = 124
        text = _read_head(out_path)
        if _dir_size(self._root) > LOCAL_DIR_BYTES:
            return RunResult(125, "JOB_FAIL 작업 폴더 용량 초과")
        return RunResult(code, text if code != 124 else "JOB_FAIL 시간 초과")

    def close(self) -> None:
        def unlock_and_retry(func, path, _exc):
            try:
                os.chmod(path, stat.S_IRWXU)
                func(path)
            except OSError:
                pass
        shutil.rmtree(self._root, onerror=unlock_and_retry)
        if self._root.exists():
            print(f"[job_box] 임시 폴더를 다 지우지 못함: {self._root}")

    def _own(self, path: Path) -> None:
        if self._uid is not None:
            os.chown(path, self._uid, self._uid)


def open_box():
    """Daytona 샌드박스를 먼저 시도하고, 못 만들면(또는 최근 실패 직후면) 격리 프로세스 상자를 연다."""
    global _daytona_skip_until
    if os.getenv("DAYTONA_API_KEY") and time.time() >= _daytona_skip_until:
        try:
            box = DaytonaBox(make_client().create())
            record_error(None)
            return box
        except Exception as exc:
            record_error(exc)
            _daytona_skip_until = time.time() + DAYTONA_COOLDOWN_SECONDS
            print(f"[job_box] 샌드박스 생성 실패 → 서버 안 격리 프로세스로 대체: {type(exc).__name__}: {exc}")
    return LocalBox()


def isolation() -> str:
    """대체 상자가 실제로 얼마나 갇히는지. 처음 한 번 확인해 기억한다."""
    global _isolation
    if _isolation is None:
        uid = _job_uid()
        if uid is None:
            _isolation = "process-only"          # 윈도우 개발 PC 등 — 별도 사용자 없음
        else:
            try:
                probe = subprocess.run(["unshare", "-rn", "true"], env=_clean_env(), capture_output=True,
                                       timeout=10, **_user_kwargs(uid))
                _isolation = "netns+uid" if probe.returncode == 0 else "uid-only"
            except (OSError, subprocess.SubprocessError):
                _isolation = "uid-only"
    return _isolation


SELF_CHECK = (
    "import os, socket\n"
    "try:\n"
    "    open('/proc/%d/environ' % os.getppid(), 'rb').read(1)\n"
    "    proc = 'OPEN'\n"
    "except Exception:\n"
    "    proc = 'blocked'\n"
    "s = socket.socket()\n"
    "s.settimeout(2)\n"
    "try:\n"
    "    s.connect(('169.254.169.254', 80))\n"
    "    net = 'OPEN'\n"
    "except Exception:\n"
    "    net = 'blocked'\n"
    "print('CHECK proc=%s net=%s' % (proc, net))\n"
)
_self_check: str | None = None


def self_check() -> str:
    """대체 상자 안에서 서버 환경변수 읽기·메타데이터 서버 접속이 실제로 막히는지 한 번 확인한다(리눅스만)."""
    global _self_check
    if _self_check is None:
        if not sys.platform.startswith("linux"):
            _self_check = "skipped"
        else:
            box = LocalBox()
            try:
                box.upload(SELF_CHECK.encode(), f"{box.home}/check.py")
                out = box.run_job([f"{box.home}/check.py"], 20).result
                _self_check = out.split("CHECK ", 1)[1].strip()[:60] if "CHECK " in out else "failed"
            finally:
                box.close()
    return _self_check


def _job_uid() -> int | None:
    if not sys.platform.startswith("linux") or os.geteuid() != 0:
        return None
    try:
        import pwd
        return pwd.getpwnam(JOB_USER).pw_uid
    except KeyError:
        return None


def _user_kwargs(uid: int | None) -> dict:
    return {} if uid is None else {"user": uid, "group": uid, "extra_groups": []}


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
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait()


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
