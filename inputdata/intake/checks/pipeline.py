"""AI 결과 한 줄을 '서버 보정 → 검사 → 상태 분류' 하는 단계들.

사전 검사(service._precheck), 저장(service._store_rows), 사람이 고친 뒤 재검사(service.edit_payload), 시험 도구(run_ai_tests)가
모두 이 파일의 STAGES 를 쓴다. 규칙을 더하려면 단계 함수 하나를 만들어 STAGES 의 알맞은 자리에 넣는다.

단계는 ctx(Ctx) 를 받아 제자리에서 고치는 함수다 (AI·DB 호출 없음. 정답 코드 실행은 하므로 스레드에서 돌린다).
- ctx.row    : {id, payload, review} AI 가 낸 한 줄. 단계들이 이 값을 고친다.
- ctx.fails / warns / reasons / status : classify 단계가 채우는 검사 결과.
"""
import asyncio, os, re, threading, time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .. import config
from ..models import BLOCKED, HELD, NEEDS_INPUT, NEEDS_REVIEW, READY
from . import checker, fix, suggest
from .fix import approval_reason as echo_reason, fidelity_reason


@dataclass
class Ctx:
    rec: Optional[dict]                                   # 문제 입력(서버가 읽은 레코드). 없을 수 있다 (사람이 고친 뒤 재검사)
    row: Dict[str, Any]                                   # {"id", "payload", "review"}
    exec_on: bool = False                                 # 정답 코드를 서버에서 실행해 확인할지
    seen: Dict[str, Any] = field(default_factory=dict)    # 같은 작업에서 이미 나온 문제 (중복 검사용)
    status: Optional[str] = None
    fails: List[str] = field(default_factory=list)
    warns: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)

    @property
    def payload(self):
        return self.row.get("payload")

    @property
    def review(self):
        return self.row.setdefault("review", {})

    def case(self):
        """checker.check_case 가 받는 모양."""
        c = {"id": self.row.get("id"), "payload": self.payload, "review": self.review}
        if self.rec: c["record"] = self.rec
        if (self.rec or {}).get("원본조각"): c["원본조각"] = self.rec["원본조각"]
        if (self.rec or {}).get("이미지"): c["그림있음"] = True           # 그림에서 옮긴 설명 글은 원본 글에 없으므로 설명 대조를 완화한다
        return c


# ---------------------------------------------------------------- 상태 분류
def runnable(payload):
    return bool(payload) and payload.get("problemType") in ("코딩", "디버깅") and payload.get("test_cases") and payload.get("reference_codes")


def classify_case(case, seen, exec_on):
    """(상태, 검사 실패, 검사 경고, 승인 필요 이유)"""
    fails, warns = checker.check_case(case, seen, skip_exec=not exec_on)
    review, payload = case.get("review") or {}, case.get("payload")
    if review.get("상태") == "보류" or payload is None:
        return (BLOCKED if fails else HELD), fails, warns, []
    if fails and fix.short_answer_missing(payload) and all("answer_text" in str(f) for f in fails):
        return NEEDS_INPUT, [], warns, [fix.SHORT_REASON]                # 정답만 비어 있는 단답형은 막지 않고 사람이 정답을 입력하게 한다
    if fails:
        return BLOCKED, fails, warns, []
    cf = fix.code_fails(payload)                             # 빈칸이 남은 정답 코드, 제공 코드에 노출된 정답은 등록하지 않는다
    if cf:
        return BLOCKED, cf, warns, []
    if fix.needs_input(payload):
        return NEEDS_INPUT, fails, warns, [fix.REASON]
    gaps = fix.test_case_gaps(payload)
    reasons = []
    if (case.get("record") or {}).get("강의자료"): reasons.append(fix.LECTURE_REASON)         # 강의자료에서 만든 문제는 항상 사람이 확인하고 승인한다 (자동 등록하지 않음)
    rec_ = case.get("record") or {}
    if rec_.get("스캔"):                                                                   # 글 층이 없는 스캔 문서: AI 가 그림을 읽어 옮기고 문제 경계도 정했으므로 항상 원본과 대조한다
        reasons.append("스캔 문서를 AI 가 그림에서 읽어 옮긴 문제입니다. 코드·숫자·기호가 원본과 같은지, 문제 나눔이 맞는지 확인하세요.")
    elif rec_.get("이미지"):                                                               # 그림(쪽 화면)으로 받은 내용이 있으면 AI 가 그림 속 코드·출력·글자를 옮겼을 수 있다
        reasons.append("문제에 붙은 그림(쪽 화면)에서 AI 가 읽어 옮긴 내용이 있을 수 있습니다. 원본의 코드·출력 예시와 같은지 확인하세요.")
    notes = review.get("확인필요") or []
    if notes: reasons.append(f"AI가 사람이 확인할 것 {len(notes)}건을 표시했습니다.")
    if any(fix.UNSURE_TYPE_RE.search(str(n)) for n in notes):
        reasons.append("문제 유형이 5가지(코딩·디버깅·객관식·단답형·주관식)에 딱 맞지 않아 가장 가까운 유형으로 넣었습니다. 유형을 확인하세요.")
    if config.ai_answer_review() and (payload or {}).get("problemType") in ("객관식", "단답형") and {"correct_answers", "answer_text"} & set(fix.ai_filled(review)):
        reasons.append("AI 가 직접 푼 정답입니다(손으로 따라간 값이라 실행으로 확인할 수 없고 틀릴 수 있음). 정답을 확인하세요.")
    up = [g["n"] for g in (review.get("그림") or []) if isinstance(g, dict) and g.get("올림")]
    if review.get("그림연결") is not None and (payload or {}).get("image_url"):
        reasons.append(f"문서 속 그림 {review['그림연결']} 을 문제 그림(image_url)으로 넣었습니다. 정답이 보이지 않는지 확인하세요.")
    elif up: reasons.append(f"문제에 올릴 그림 {len(up)}장(번호 {up}: 개념·설명 그림)이 있습니다. 정답이 보이지 않는지 확인하고 올리세요.")
    for w in warns:
        if str(w).startswith("ref:"): reasons.append("정답 코드에 제공 코드가 거의 들어 있지 않습니다. 제공 코드까지 포함한 전체 코드인지 확인하세요."); break
    r = checker.answer_leak_reason(payload)
    if r: reasons.append(r)
    r = checker.dup_option_reason(payload)
    if r: reasons.append(r)
    r = echo_reason(warns)
    if r: reasons.append(r)
    r = fidelity_reason(case.get("record"), payload)
    if r: reasons.append(r)
    r = fix.residue_reason(case.get("record"), payload)
    if r: reasons.append(r)
    sp = fix.syntax_problem(payload)
    if sp: reasons.append(f"정답 코드에 문법 오류가 있어 보입니다 ({sp}). 서버가 코드를 실행하지 않으므로 사람이 정답 코드를 확인하세요.")
    if "ref_lacks_base" in fix.code_issues(payload):         # 정답 코드가 제공 코드를 담지 않음: 사람이 승인 때 확인
        reasons.append("정답 코드가 제공 코드(문제에 주어진 코드)를 담고 있지 않습니다. 제공 코드까지 포함한 완성 코드인지 확인하세요.")
    r = fix.type_mismatch_reason(payload)                    # 물음에 답하는 문제인데 코딩으로 정해짐: 사람이 승인 때 유형을 확인
    if r: reasons.append(r)
    r = fix.review_reason(review, payload)                   # AI 가 표시한 '필요 없어 보이는 줄' (삭제하지 않고 안내만)
    if r: reasons.append(r)
    if payload.get("problemType") in ("코딩", "디버깅") and fix.reads_input(payload) and len(payload.get("test_cases") or []) < fix.MIN_CASES:      # 막지 않고 승인 전에 권장한다. 입력을 읽는 코드는 입력만 바꿔 늘릴 수 있다 (입력이 없는 프로그램은 늘릴 수 없어 권장하지 않음)
        reasons.append(f"테스트 케이스가 {len(payload.get('test_cases') or [])}개입니다. 정답 코드가 입력을 읽으므로 입력이 서로 다른 케이스를 최소 {fix.MIN_CASES}개 두는 것을 권장합니다. 승인하기 전에 입력이 다른 케이스를 추가해 주세요.")
    if (review.get("제목정보") or {}).get("주제번호확실") is False:
        reasons.append("제목 코드의 주제 번호를 문제 ID에서 찾지 못해 0으로 했습니다. 등록 제목(" + str(review["제목정보"].get("등록제목")) + ")의 주제 번호를 확인하세요.")
    if runnable(payload):
        if not exec_on:
            reasons.append("정답 코드를 서버에서 실행해 확인하지 않았습니다.")
        else:                                           # 실행 환경이 없는 언어(java 등)는 실행 검증을 건너뛰므로 사람이 확인
            skipped = sorted({str(w).split(":", 1)[1].strip() for w in warns if str(w).startswith("exec:") and "실행 환경 없음" in str(w)})
            if skipped: reasons.append(f"정답 코드를 실행해 확인하지 못했습니다 ({', '.join(skipped)}).")
    if gaps: return NEEDS_INPUT, fails, warns, gaps + reasons                          # 테스트 케이스 입력·출력 빈칸은 사람이 채워야 승인 가능
    return (NEEDS_REVIEW if reasons else READY), fails, warns, reasons


# ---------------------------------------------------------------- 단계들
def fill_exec(ctx):
    """정답 코드의 실제 실행 출력으로 AI 가 손으로 추적한 기대 출력을 채운다 (모델에 따라 틀리는 값을 바로잡는다)."""
    if ctx.exec_on and ctx.payload:
        fix.fill(ctx.payload, ctx.review, checker.run_code, checker.norm_out)

def correct(ctx):
    """형식 바로잡기·원문 복원·설명 정리(다른 칸으로 옮겨진 구역 제거 포함) 등 서버 보정 전부."""
    fix.correct(ctx.rec, ctx.row)

def extend_cases(ctx):
    """입력을 받는 코드인데 테스트 케이스가 1개뿐이면 입력만 다른 케이스를 2개까지 더한다."""
    if ctx.payload:
        fix.extend_cases(ctx.payload, ctx.review, checker.run_code, checker.norm_out, ctx.exec_on)

def show_input(ctx):
    """기대 출력에서 안내 문구 뒤에 입력값을 보이게 한다 (extend_cases 가 더한 케이스까지)."""
    if ctx.payload:
        fix.show_input_echo(ctx.rec, ctx.payload, ctx.review)

def apply_conditions(ctx):
    """AI 가 제안한 조건(사용 기능·출력형식)을 정답 코드·기대 출력과 맞는지 확인하고 자동으로 더한다."""
    if ctx.payload:
        suggest.apply_conditions(ctx.payload, ctx.review)

def finish(ctx):
    """그림 올림 표시·거짓 경고 지우기·제목 되돌리기·서술형 답 비우기."""
    fix.finish(ctx.rec, ctx.row)

def classify(ctx):
    """서버 검사를 돌려 상태·실패·경고·승인 이유를 정한다."""
    ctx.status, ctx.fails, ctx.warns, ctx.reasons = classify_case(ctx.case(), ctx.seen, ctx.exec_on)


STAGES: List[Callable[[Ctx], None]] = [fill_exec, correct, extend_cases, show_input, apply_conditions, finish, classify]


# ---------------------------------------------------------------- 일정 조절 (서버가 한꺼번에 몰려 버티지 못하지 않게)
def _env_int(name, default):
    try: return int(os.environ.get(name, default))
    except ValueError: return default


class AiGate:
    """서버 전체(모든 작업 합쳐서)의 AI 호출을 고르게 내보낸다.
    - 동시에 나가는 호출 수 상한 (INTAKE_AI_MAX_INFLIGHT, 기본 20: 작업 하나가 쓰던 동시 호출 수와 같아 평소 속도는 그대로이고, 작업이 여럿 겹칠 때만 합쳐서 제한)
    - 호출을 시작하는 간격 (INTAKE_AI_PER_SEC, 기본 초당 30회: 실제 AI 호출은 수 초~수십 초 걸려 평소엔 걸리지 않고, 짧은 호출이 한꺼번에 쏟아질 때만 고르게 펴 줌)
    - 혼잡(429·5xx·시간 초과)이 나면 동시 호출 상한을 절반으로 줄이고 잠깐 쉰다 (INTAKE_AI_COOLDOWN_SEC, 기본 5초). 성공이 10번 이어지면 하나씩 다시 늘린다.
    줄 서서 기다리는 시간은 AI 응답 기한에 들어가지 않도록 호출 시작 전에 기다린다 (service._timed)."""
    OVERLOAD = re.compile(r"429|\b5\d\d\b|한도|혼잡|rate.?limit|quota|overload|timed? ?out|시간\s*초과|지나도 오지 않", re.I)

    def __init__(self):
        self._lock = threading.Lock()
        self.inflight = 0
        self.limit = None
        self.tokens = None
        self.t_last = time.monotonic()
        self.cool_until = 0.0
        self.good = 0

    def _max(self): return max(1, _env_int("INTAKE_AI_MAX_INFLIGHT", 20))

    async def enter(self):
        await MEM_GUARD.wait_ok()                                     # 메모리가 한도를 넘었으면 새 호출 시작을 잠깐 미룬다 (정상일 때는 즉시 통과)
        while True:
            with self._lock:
                now, mx = time.monotonic(), self._max()
                if self.tokens is None: self.tokens = float(mx)
                if self.limit is None or self.limit > mx: self.limit = mx
                rate = max(0, _env_int("INTAKE_AI_PER_SEC", 30))                     # 초당 시작 수 (0 이면 제한 없음). 처음 한꺼번에 나가는 수는 동시 호출 상한까지 허용한다
                if rate:
                    self.tokens = min(float(mx), self.tokens + (now - self.t_last) * rate)
                else:
                    self.tokens = float(mx)
                self.t_last = now
                if self.inflight < self.limit and self.tokens >= 1 and now >= self.cool_until:
                    self.inflight += 1; self.tokens -= 1
                    return
                wait = max(self.cool_until - now, (1 - self.tokens) / rate if rate else 0.02, 0.02)
            await asyncio.sleep(min(wait, 1.0))

    def leave(self, overload=False):
        with self._lock:
            self.inflight = max(0, self.inflight - 1)
            if overload:
                self.limit = max(1, (self.limit or self._max()) // 2)
                self.cool_until = time.monotonic() + max(0, _env_int("INTAKE_AI_COOLDOWN_SEC", 5))
                self.good = 0
            else:
                self.good += 1
                if self.good >= 10 and (self.limit or 0) < self._max(): self.limit += 1; self.good = 0

    def is_overload(self, exc):
        return isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or type(exc).__name__ == "SlowCall" or bool(self.OVERLOAD.search(str(exc)))


class MemGuard:
    """서버 프로세스의 메모리(RSS)가 한도에 가까워지면 새 일을 잠깐 미뤄 서버가 죽지 않게 한다. 평소(한도 아래)에는 아무것도 하지 않아 속도에 영향이 없다.
    한도: INTAKE_MEM_LIMIT_MB (0 이면 끔). 설정이 없으면 컨테이너(cgroup) 한도의 75%, 그것도 없으면 기계 전체 메모리의 60%, 그것도 알 수 없으면 1500MB 를 쓴다 (한도를 몰라도 보호는 항상 켜져 있다).
    - 한도를 넘으면 새 업로드·새 AI 호출 시작을 한도 아래로 내려올 때까지 기다리게 한다 (진행 중인 일이 끝나 메모리가 풀리길 기다림. 이때 gc 를 한 번 돌림)
    - 한도의 120% 를 넘으면 새 업로드는 기다리지 않고 503 으로 돌려보낸다."""
    def __init__(self):
        self._gc_at = 0.0
        self._limit = None

    @staticmethod
    def parse_cgroup_stat(text):
        """cgroup memory.stat 글에서 프로그램이 실제로 쓰는 메모리(MB)를 읽는다: v2 는 anon + kernel, v1 은 rss + cache 를 뺀 rss. 파일 캐시는 넣지 않는다(곧 비워져 오탐이 됨). 읽을 수 없으면 None."""
        vals = {}
        for line in str(text or "").splitlines():
            k, _, v = line.partition(" ")
            if v.strip().isdigit(): vals[k] = int(v)
        if "anon" in vals: return (vals["anon"] + vals.get("kernel", 0) + vals.get("shmem", 0)) / 1048576
        if "rss" in vals: return (vals["rss"] + vals.get("mapped_file", 0)) / 1048576
        return None

    @classmethod
    def cgroup_used_mb(cls):
        """컨테이너 전체(프로세스가 여러 개여도 합쳐서)가 쓰는 메모리(MB). 컨테이너가 아니면 None."""
        for path in ("/sys/fs/cgroup/memory.stat", "/sys/fs/cgroup/memory/memory.stat"):
            try:
                with open(path) as f: v = cls.parse_cgroup_stat(f.read())
                if v is not None: return v
            except Exception:
                continue
        return None

    @classmethod
    def rss_mb(cls):
        """지금 쓰는 메모리(MB): 이 프로세스 값과 컨테이너 전체 값 중 큰 쪽(프로세스가 여러 개여도 합쳐서 지킨다). 알 수 없으면 None."""
        mine, box = cls._proc_mb(), cls.cgroup_used_mb()
        vals = [x for x in (mine, box) if x is not None]
        return max(vals) if vals else None

    @staticmethod
    def _proc_mb():
        """이 프로세스가 지금 쓰는 메모리(MB). psutil → 리눅스 /proc → 윈도우 API 순으로 시도한다. 알 수 없으면 None."""
        try:
            import psutil
            return psutil.Process().memory_info().rss / 1048576
        except Exception:
            pass
        try:
            with open("/proc/self/statm") as f:
                return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1048576
        except Exception:
            pass
        try:
            import ctypes
            from ctypes import wintypes
            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
            pmc = PMC(); pmc.cb = ctypes.sizeof(PMC)
            k = ctypes.WinDLL("kernel32", use_last_error=True); k.GetCurrentProcess.restype = wintypes.HANDLE
            ps = ctypes.WinDLL("psapi", use_last_error=True); ps.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]      # 64비트에서 핸들이 잘리지 않게 형을 지정
            if ps.GetProcessMemoryInfo(k.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb): return pmc.WorkingSetSize / 1048576
        except Exception:
            pass
        return None

    @staticmethod
    def total_mb():
        """이 기계(또는 컨테이너 밖 전체)의 메모리(MB). 알 수 없으면 None."""
        try:
            import psutil
            return psutil.virtual_memory().total / 1048576
        except Exception:
            pass
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"): return int(line.split()[1]) / 1024
        except Exception:
            pass
        try:
            import ctypes
            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong), ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
            ms = MS(); ms.dwLength = ctypes.sizeof(MS)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)): return ms.ullTotalPhys / 1048576
        except Exception:
            pass
        return None

    def limit_mb(self):
        """한도(MB). 환경변수 → 컨테이너 한도의 75% → 기계 전체 메모리의 60% → (아무것도 알 수 없으면) 1500MB. 한도를 알 수 없어도 보호는 꺼지지 않는다. INTAKE_MEM_LIMIT_MB=0 으로만 끌 수 있다."""
        v = os.environ.get("INTAKE_MEM_LIMIT_MB")
        if v is not None:
            try: return max(0, int(float(v)))
            except ValueError: pass
        if self._limit is None:
            lim = 0
            for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
                try:
                    raw = open(path).read().strip()
                    if raw.isdigit() and int(raw) < 1 << 50: lim = int(int(raw) / 1048576 * 0.75); break
                except Exception:
                    continue
            if not lim:
                tot = self.total_mb()
                lim = int(tot * 0.6) if tot else 1500
            self._limit = lim
        return self._limit

    def level(self):
        """0 정상, 1 한도 넘음(새 일을 미룸), 2 한도의 120% 넘음(업로드 거절)."""
        lim = self.limit_mb()
        if not lim: return 0
        cur = self.rss_mb()
        if cur is None: return 0
        return 2 if cur > lim * 1.2 else (1 if cur > lim else 0)

    def relieve(self):
        """메모리를 줄일 수 있는 만큼 줄인다 (5초에 한 번만 gc)."""
        now = time.monotonic()
        if now - self._gc_at > 5:
            self._gc_at = now
            import gc; gc.collect()

    async def wait_ok(self, max_wait=30):
        """한도 아래가 될 때까지 기다린다 (최대 max_wait 초. 넘으면 그냥 진행: 영영 멈춰 있지 않게). 정상일 때는 바로 돌아온다."""
        if self.level() == 0: return
        self.relieve()
        t0 = time.monotonic()
        while self.level() and time.monotonic() - t0 < max_wait:
            await asyncio.sleep(0.5)


MEM_GUARD = MemGuard()


class ParseGate:
    """파일 읽기·문제 나누기(CPU·메모리를 많이 씀)를 서버 전체에서 동시에 몇 개까지만 한다 (INTAKE_PARSE_CONCURRENCY, 기본 2).
    넘치면 거절하지 않고 차례를 기다리게 한다(사용자에게는 업로드가 조금 늦게 접수될 뿐). 서버 메모리가 한도를 넘었으면 내려올 때까지 기다린다(최대 60초, 그 뒤에는 진행해 영영 멈추지 않음)."""
    def __init__(self):
        self.active = 0

    async def enter(self):
        await MEM_GUARD.wait_ok(60)
        mx = max(1, _env_int("INTAKE_PARSE_CONCURRENCY", 2))
        while self.active >= mx:
            await asyncio.sleep(0.05)
        self.active += 1

    def leave(self):
        self.active = max(0, self.active - 1)


PARSE_GATE = ParseGate()


class ImageGate:
    """그림이 붙은 AI 호출(요청 하나가 수 MB 의 base64 를 들고 있어 메모리를 많이 씀)만 따로 동시에 몇 개까지 허용한다 (INTAKE_AI_IMAGE_INFLIGHT, 기본 6).
    글만 있는 호출은 이 문을 거치지 않으므로 속도가 그대로이고, 그림 문제가 한꺼번에 몰려 메모리가 치솟는 것만 막는다."""
    def __init__(self):
        self.n = 0

    async def enter(self):
        mx = max(1, _env_int("INTAKE_AI_IMAGE_INFLIGHT", 6))
        while self.n >= mx:
            await asyncio.sleep(0.05)
        self.n += 1

    def leave(self):
        self.n = max(0, self.n - 1)


IMAGE_GATE = ImageGate()
AI_GATE = AiGate()
_RUN_SLOTS = None


def _slots():
    """보정·검사 단계(정답 코드 실행 포함)를 동시에 돌리는 수 상한 (INTAKE_PIPELINE_CONCURRENCY, 기본 4). 코드 실행이 서버 CPU 를 한꺼번에 쓰지 않게 한다."""
    global _RUN_SLOTS
    if _RUN_SLOTS is None: _RUN_SLOTS = threading.BoundedSemaphore(max(1, _env_int("INTAKE_PIPELINE_CONCURRENCY", 4)))
    return _RUN_SLOTS


def run(ctx, stages=None):
    """단계를 차례로 돌리고 ctx 를 돌려준다. 정답 코드를 실행할 수 있으므로 비동기 코드에서는 asyncio.to_thread(run, ctx) 로 부른다. 동시에 도는 수는 INTAKE_PIPELINE_CONCURRENCY 로 제한한다."""
    with _slots():
        for stage in (STAGES if stages is None else stages):
            stage(ctx)
    return ctx
