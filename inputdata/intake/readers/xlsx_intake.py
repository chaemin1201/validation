"""엑셀로 정리된 문제를 읽어 문제(행) 하나씩의 레코드로 만들고, 등록 필수 항목이 비어 있는지 점검한다."""
import json, os, re


class _LazyOpenpyxl:
    """openpyxl 은 엑셀을 실제로 읽을 때만 불러온다. 서버에 이 패키지가 없어도 서버(와 문서·한글·PDF 처리)는 정상으로 뜨고, 엑셀을 올렸을 때만 안내가 나간다."""
    def __getattr__(self, name):
        try:
            import openpyxl as _m
        except ImportError:
            raise SystemExit("엑셀을 읽으려면 서버에 openpyxl 패키지가 필요합니다 (pip install openpyxl). 서버 관리자에게 문의하세요.")
        return getattr(_m, name)


openpyxl = _LazyOpenpyxl()

from . import images as safety      # 업로드 파일 안전 점검(images.py 안)

SYN = {
    "이름": ["이름", "문제 이름", "문제이름", "제목", "문제 제목"],
    "유형": ["유형", "문제 유형"], "난이도": ["난이도", "문제 난이도"],
    "조건": ["조건", "문제 조건"], "설명": ["설명", "문제 설명"],
    "부제목": ["부제목"], "문제의도": ["문제의도", "출제의도", "출제 의도"],
    "제공코드": ["제공코드", "제공 코드", "문제 제공코드", "문제 제공 코드"], "정답코드": ["정답코드", "정답 코드", "전체 코드", "전체코드", "문제 정답코드"],
    "입력": ["입력", "입력1"], "출력": ["출력", "예상 출력", "출력1"], "언어": ["언어"], "이미지": ["이미지"],
    "입력2": ["입력2"], "출력2": ["출력2"], "입력3": ["입력3"], "출력3": ["출력3"],
    "보기": ["보기", "객관식 보기"], "정답": ["정답", "단답형 정답", "주관식 정답"], "채점기준": ["채점기준", "채점 기준"],
    "비고": ["비고"],
    "태그": ["태그", "태그들", "키워드"],
}
TYPES = {"코딩", "디버깅", "객관식", "주관식", "단답형"}
# DB 열 이름은 영어인 경우가 많다 (예: 이 서비스의 problem 표: title, description, difficulty, problem_type …)
for _k, _names in {"이름": ["title", "name", "problem_title"], "설명": ["description", "desc", "problem_description", "content"], "난이도": ["difficulty", "level"],
                   "유형": ["problem_type", "problemtype", "type", "kind"], "조건": ["condition", "conditions", "problem_condition"],
                   "제공코드": ["base_code", "starter_code", "provided_code"], "정답코드": ["reference_code", "reference_codes", "answer_code", "solution", "solution_code"],
                   "입력": ["input", "test_input"], "출력": ["expected_output", "output", "test_output"], "보기": ["options", "choices"],
                   "정답": ["answer", "answers", "correct_answers", "answer_text"], "언어": ["language", "lang"], "부제목": ["subtitle"], "문제의도": ["intent"],
                   "채점기준": ["grading_criteria"], "태그": ["tags", "tag"], "비고": ["note", "notes"]}.items():
    SYN[_k] = SYN.get(_k, []) + _names

UNKNOWN_COLS_MEMO = "인식하지 못해 무시한 열: "      # 이 열의 값은 AI 에게 전달되지 않는다. 열 이름을 SYN 에 있는 이름으로 바꾸거나 SYN 에 추가해야 한다
REQUIRED = {                      # 유형별 필수 (사용자가 정한 기준)
    "코딩": ["이름", "유형", "설명", "정답코드", "테스트결과"],
    "디버깅": ["이름", "유형", "설명", "정답코드", "제공코드", "테스트결과"],
    "객관식": ["이름", "유형", "설명", "보기"],
    "주관식": ["이름", "유형", "설명", "정답"],
    "단답형": ["이름", "유형", "설명", "정답"],
}


def canon(header):
    h = re.sub(r"\s+", " ", str(header or "")).strip()
    for k, names in SYN.items():
        if h.lower() in [n.lower() for n in names]: return k
    m = re.match(r"^학생 실행 (코드|결과) (\d+)$", h)
    if m: return ("실행코드" if m.group(1) == "코드" else "실행결과") + m.group(2)
    return None


MAX_SHEET_ROWS = 20000                      # 시트 하나에서 읽는 줄 수의 상한 (INTAKE_MAX_SHEET_ROWS). 문제 한 줄은 문제 하나이므로 이보다 많으면 문제표가 아니다
MAX_SHEET_COLS = 60                         # 한 줄에서 읽는 칸 수의 상한


def _max_rows():
    try: return max(100, int(os.environ.get("INTAKE_MAX_SHEET_ROWS", MAX_SHEET_ROWS)))
    except ValueError: return MAX_SHEET_ROWS


class _LiteCell:
    __slots__ = ("value", "column")
    def __init__(self, value, column): self.value, self.column = value, column


class _LiteSheet:
    """읽기 전용으로 한 줄씩 읽어 값만 담은 시트 (openpyxl 의 일반 모드는 칸마다 객체를 만들어 큰 엑셀이 메모리를 수백 MB~수 GB 쓴다).
    find_header · read 가 쓰는 ws.title / ws.max_row / ws.cell(r, c).value / ws[r] 만 같은 모양으로 준다."""
    def __init__(self, title, rows):
        self.title, self._rows, self.max_row = title, rows, len(rows)

    def _row(self, r): return self._rows[r - 1] if 1 <= r <= len(self._rows) else ()

    def cell(self, r, c):
        row = self._row(r)
        return _LiteCell(row[c - 1] if 1 <= c <= len(row) else None, c)

    def __getitem__(self, r): return [_LiteCell(v, i) for i, v in enumerate(self._row(r), 1)]

    def iter_rows(self, values_only=False): return iter(self._rows)


def _lite_sheets(path):
    """[(시트 이름, _LiteSheet)]. 줄은 한 줄씩 읽고, 뒤쪽의 빈 줄(서식만 남은 줄)은 담지 않으며, 줄 수가 상한을 넘으면 거절한다."""
    limit = _max_rows()
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    out = []
    try:
        for ws in wb.worksheets:
            rows, pending, kept = [], 0, 0
            for row in ws.iter_rows(values_only=True):
                row = tuple(row[:MAX_SHEET_COLS])
                if all(v is None or (isinstance(v, str) and not v.strip()) for v in row):
                    pending += 1; continue
                rows.extend(() for _ in range(pending)); pending = 0
                rows.append(row); kept += 1
                if len(rows) > limit:
                    raise SystemExit(f"{ws.title}: 표가 너무 큽니다 (줄 {limit}개 초과). 문제 표가 아닌 큰 데이터가 들어 있지 않은지 확인하세요 (INTAKE_MAX_SHEET_ROWS 로 바꿀 수 있습니다).")
            out.append((ws.title, _LiteSheet(ws.title, rows)))
    finally:
        wb.close()
    return out


_NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_NS_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_NS_XDR = "{http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing}"
_NS_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_NS_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
MAX_PICS_PER_ROW = 8                       # 한 줄(문제)에 붙이는 그림 수


def _rels(z, path):
    """관계 파일(.rels)의 {id: (종류 끝부분, 목표 경로)}. 없으면 {}."""
    import xml.etree.ElementTree as ET
    if path not in z.namelist(): return {}
    out = {}
    for r in ET.fromstring(z.read(path)).iter(_NS_REL + "Relationship"):
        out[r.get("Id")] = ((r.get("Type") or "").rsplit("/", 1)[-1], r.get("Target") or "")
    return out


def _resolve(base_dir, target):
    import posixpath
    return posixpath.normpath(target.lstrip("/")) if target.startswith("/") else posixpath.normpath(posixpath.join(base_dir, target))


_NS_RD = "{http://schemas.microsoft.com/office/spreadsheetml/2017/richdata}"
_NS_RD2 = "{http://schemas.microsoft.com/office/spreadsheetml/2017/richdata2}"


def _col_index(ref):
    """'AB12' 의 열 번호(1부터)와 줄 번호."""
    m = re.match(r"^([A-Za-z]+)(\d+)$", ref or "")
    if not m: return None, None
    col = 0
    for ch in m.group(1).upper(): col = col * 26 + ord(ch) - 64
    return col, int(m.group(2))


def _inline_image_map(z, names):
    """셀에 직접 넣은 그림(엑셀 365 '셀에 배치', 값이 #VALUE! 로 보임): {vm 번호: 그림 파일 경로}. 파일 안의 richData 를 따라간다 (셀 vm → 메타데이터 → 리치 값 → 그림 관계 → media). 없으면 {}."""
    import xml.etree.ElementTree as ET
    need = ("xl/metadata.xml", "xl/richData/rdrichvalue.xml", "xl/richData/richValueRel.xml")
    if not all(n in names for n in need): return {}
    try:
        # 리치 값 구조: 구조 번호 → 'LocalImageIdentifier' 가 몇 번째 키인지
        key_pos = {}
        if "xl/richData/rdrichvaluestructure.xml" in names:
            for i, st in enumerate(ET.fromstring(z.read("xl/richData/rdrichvaluestructure.xml")).iter(_NS_RD + "s")):
                ks = [k.get("n") for k in st.iter(_NS_RD + "k")]
                key_pos[i] = next((j for j, n in enumerate(ks) if n and "LocalImageIdentifier" in n), 0)
        rich = []                                              # 리치 값 번호 → 관계 목록에서의 위치
        for rv in ET.fromstring(z.read("xl/richData/rdrichvalue.xml")).iter(_NS_RD + "rv"):
            vs = [v.text for v in rv.iter(_NS_RD + "v")]
            pos = key_pos.get(int(rv.get("s") or 0), 0)
            rich.append(int(vs[pos]) if pos < len(vs) and (vs[pos] or "").lstrip("-").isdigit() else None)
        rel_ids = [r.get(_NS_R + "id") for r in ET.fromstring(z.read("xl/richData/richValueRel.xml")) if r.get(_NS_R + "id")]
        rels = _rels(z, "xl/richData/_rels/richValueRel.xml.rels")
        # 메타데이터: 셀의 vm(1부터) → valueMetadata 의 bk → rc.v → futureMetadata 의 rvb.i(리치 값 번호)
        md = ET.fromstring(z.read("xl/metadata.xml"))
        fut = []
        for fm in md.iter(_NS_MAIN + "futureMetadata"):
            if fm.get("name") == "XLRICHVALUE":
                for bk in fm.iter(_NS_MAIN + "bk"):
                    rvb = next(bk.iter(_NS_RD + "rvb"), None)
                    fut.append(int(rvb.get("i")) if rvb is not None and (rvb.get("i") or "").isdigit() else None)
        out = {}
        for n, bk in enumerate(next(md.iter(_NS_MAIN + "valueMetadata"), []), 1):
            rc = next(bk.iter(_NS_MAIN + "rc"), None)
            if rc is None: continue
            fi = int(rc.get("v") or 0)
            ri = fut[fi] if fi < len(fut) else None
            rel_pos = rich[ri] if ri is not None and ri < len(rich) else None
            rid = rel_ids[rel_pos] if rel_pos is not None and rel_pos < len(rel_ids) else None
            if rid in rels: out[n] = _resolve("xl/richData", rels[rid][1])
        return out
    except (KeyError, ET.ParseError, ValueError, IndexError, AttributeError, TypeError):
        return {}


def _inline_cells(z, sheet_path):
    """한 시트에서 vm 속성이 있는 칸 [(줄, 열, vm)]. 시트 xml 을 한 칸씩 읽는다 (통째로 올리지 않음)."""
    import xml.etree.ElementTree as ET
    out = []
    with z.open(sheet_path) as fh:
        for _ev, el in ET.iterparse(fh, events=("end",)):
            if el.tag == _NS_MAIN + "c":
                vm = el.get("vm")
                if vm and vm.isdigit():
                    col, row = _col_index(el.get("r"))
                    if col: out.append((row, col, int(vm)))
                el.clear()
            elif el.tag == _NS_MAIN + "row":
                el.clear()
    return out


def sheet_pictures(path, with_cells=False):
    """엑셀 시트에 놓인 그림을 {(시트 이름, 엑셀 줄 번호): [그림 바이트]} 로. 그림이 놓인 칸(왼쪽 위 모서리)의 줄 기준이고 같은 줄에서는 왼쪽 칸부터이다.
    파일 안의 xml 만 읽는다(openpyxl 로 통째로 올리지 않음). 그림 하나 10MB·엑셀 하나 80MB 를 넘으면 건너뛴다. 그림이 없거나 읽을 수 없으면 {}."""
    import xml.etree.ElementTree as ET, zipfile
    if not str(path).lower().endswith((".xlsx", ".xlsm")): return {}
    budget = safety.RawBudget()
    out, cells = {}, set()
    ret = (lambda: (out, cells)) if with_cells else (lambda: out)
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if not any(n.startswith("xl/media/") for n in names): return ret()
            inline = _inline_image_map(z, names)
            wb_rels = _rels(z, "xl/_rels/workbook.xml.rels")
            for sh in ET.fromstring(z.read("xl/workbook.xml")).iter(_NS_MAIN + "sheet"):
                kind_target = wb_rels.get(sh.get(_NS_R + "id"))
                if not kind_target: continue
                sheet_path = _resolve("xl", kind_target[1])
                if inline and sheet_path in names:                               # 셀 안 그림: 칸에 붙은 그림을 그 줄에 넣고, 칸 값(#VALUE!)은 표시해 둔다
                    for row, col, vm in _inline_cells(z, sheet_path):
                        media = inline.get(vm)
                        if not media or media not in names: continue
                        cells.add((sh.get("name"), row, col))
                        if len(out.get((sh.get("name"), row), [])) >= MAX_PICS_PER_ROW or not budget.take(z.getinfo(media).file_size): continue
                        out.setdefault((sh.get("name"), row), []).append(z.read(media))
                srels = _rels(z, sheet_path.rsplit("/", 1)[0] + "/_rels/" + sheet_path.rsplit("/", 1)[1] + ".rels")
                for _id, (kind, target) in srels.items():
                    if kind != "drawing": continue
                    dpath = _resolve(sheet_path.rsplit("/", 1)[0], target)
                    if dpath not in names: continue
                    drels = _rels(z, dpath.rsplit("/", 1)[0] + "/_rels/" + dpath.rsplit("/", 1)[1] + ".rels")
                    found = []
                    for anchor in ET.fromstring(z.read(dpath)):
                        frm = anchor.find(_NS_XDR + "from")
                        blip = next(anchor.iter(_NS_A + "blip"), None)
                        if frm is None or blip is None: continue
                        rel = drels.get(blip.get(_NS_R + "embed"))
                        if not rel: continue
                        try: found.append((int(frm.find(_NS_XDR + "row").text) + 1, int(frm.find(_NS_XDR + "col").text), _resolve(dpath.rsplit("/", 1)[0], rel[1])))
                        except (AttributeError, TypeError, ValueError): continue
                    for row, _col, media in sorted(found):
                        if media not in names or len(out.get((sh.get("name"), row), [])) >= MAX_PICS_PER_ROW: continue
                        if not budget.take(z.getinfo(media).file_size): continue
                        out.setdefault((sh.get("name"), row), []).append(z.read(media))
    except (zipfile.BadZipFile, KeyError, ET.ParseError, OSError, ValueError):
        return ret()
    return ret()


def _attach_pictures(path, out, include_examples):
    """read() 가 만든 행들에 시트 속 그림을 붙인다(blobs). 그림이 문제 줄이 아닌 칸(머리글·빈 줄)에 놓였으면 바로 위의 문제 줄에 붙인다."""
    pics = sheet_pictures(path)
    if not pics: return
    by_sheet = {}
    for rec in out:
        title = rec["source"].rsplit(":", 1)[-1]
        by_sheet.setdefault(title, []).append(rec)
    for (title, row), blobs in pics.items():
        recs = by_sheet.get(title) or []
        target = None
        for rec in recs:
            if rec["row"] <= row and (target is None or rec["row"] > target["row"]): target = rec          # 그림이 놓인 줄 이하에서 가장 가까운 문제 줄
        if target is not None and row - target["row"] <= 30:
            target.setdefault("blobs", []).extend(blobs[:MAX_PICS_PER_ROW])


def find_header(ws, scan=12):
    for r in range(1, scan + 1):
        mapped = {c.column: canon(c.value) for c in ws[r] if canon(c.value)}
        if "설명" in mapped.values() and "이름" in mapped.values():
            return r, mapped
    raise SystemExit(f"{ws.title}: 이름/설명 머리글을 찾지 못했습니다.")


def infer_type(f):
    """유형 열이 비어 있을 때의 단순 추정 (보고용). 의미 판단은 AI 가 다시 한다."""
    d = f.get("설명", "")
    if f.get("보기"): return "객관식"
    if f.get("제공코드"): return "디버깅"
    if re.search(r"(출력\s*)?결과를?\s*작성", d): return "단답형"
    if re.search(r"(기술|설명)\s*(을\s*)?(하|해)\s*(시오|보)", d) and "프로그램" not in d: return "주관식"
    if f.get("정답코드") or f.get("입력") or f.get("출력") or any(f.get(f"실행결과{i}") for i in (1, 2, 3)): return "코딩"
    return None                                  # 코드 단서가 없으면 코딩으로 단정하지 않고 AI 가 본문을 보고 판단한다 (O/X·연결하기 같은 문제를 코딩으로 오해하지 않게)


def intake_row(f, source):
    notes = []
    given = f.get("유형", "")
    ptype = given if given in TYPES else infer_type(f)
    if given and given not in TYPES: notes.append(f"알 수 없는 유형 값: {given!r}")
    inferred = given not in TYPES

    has_test = bool(f.get("출력")) or any(f.get(f"실행결과{i}") for i in (1, 2, 3))
    present = {"이름": bool(f.get("이름")), "유형": not inferred, "설명": bool(f.get("설명")),
               "정답코드": bool(f.get("정답코드")), "제공코드": bool(f.get("제공코드")),
               "테스트결과": has_test, "보기": bool(f.get("보기")),
               "정답": bool(f.get("정답") or f.get("출력"))}      # 예전 엑셀은 정답을 출력 열에 적었음
    missing = [x for x in REQUIRED[ptype] if not present[x]] if ptype else []
    if not ptype: notes.append("유형 열이 비어 있고 코드 단서도 없음 → 본문을 보고 유형을 판단(코딩으로 단정하지 말 것)")

    d = f.get("설명", "")
    hold = bool(re.search(r"실행 테스트 불가|테스트 불가", d)) and len(d) < 120
    if hold: notes.append("설명이 상태 메시지뿐 → 보류 후보")
    if re.search(r"방법\s*\d|풀이\s*\d", d + f.get("조건", "")): notes.append("풀이 방식이 여러 개 → 문제 분할 후보")
    if f.get("이미지") not in (None, "", "X"): notes.append(f"이미지 있음: {f['이미지']}")
    if re.search(r"터틀|turtle|\.mp4|그림과 같이", d, re.I): notes.append("그래픽 출력 가능성 → 테스트 케이스 없음 예상")
    if "입력" in f and f.get("입력") == "" and has_test and ptype in ("코딩", "디버깅"):
        pass
    return {"유형": ptype, "유형추정": inferred, "필수누락": missing, "보류후보": hold, "메모": notes}


SKIP_SHEETS = {"안내", "예시", "설명"}      # 템플릿의 안내·예시 시트는 문제로 읽지 않는다 (--include-examples 로 포함 가능)


def _csv_rows(path):
    """CSV/TSV 를 읽어 (머리글 행 번호, 열 매핑, 행 목록) 형태로 돌려준다."""
    import csv
    import io
    text = safety.read_text_file(path)                                    # UTF-8·UTF-16·CP949 모두 읽는다
    delim = "\t" if path.lower().endswith((".tsv", ".tab")) else ","
    if delim == ",":                                                      # .csv 라도 엑셀 설정(지역)에 따라 세미콜론·탭으로 저장될 수 있다: 머리글 줄이 가장 많이 나뉘는 구분자를 쓴다
        head = next((ln for ln in text.splitlines() if ln.strip()), "")
        best = max((",", ";", "\t"), key=lambda d: len(next(csv.reader([head], delimiter=d), [])))
        if len(next(csv.reader([head], delimiter=best), [])) >= 2: delim = best
    limit, rows, pending = _max_rows(), [], 0                            # 줄을 한꺼번에 목록으로 만들지 않고 한 줄씩 담는다: 뒤쪽 빈 줄은 담지 않고, 줄 수가 상한을 넘으면 거절한다 (엑셀과 같은 규칙)
    for row in csv.reader(io.StringIO(text, newline=""), delimiter=delim):
        if not any(str(c).strip() for c in row):
            pending += 1; continue
        rows.extend([] for _ in range(pending)); pending = 0
        rows.append(row[:MAX_SHEET_COLS])
        if len(rows) > limit:
            raise SystemExit(f"표가 너무 큽니다 (줄 {limit}개 초과). 문제 표가 아닌 큰 데이터가 들어 있지 않은지 확인하세요 (INTAKE_MAX_SHEET_ROWS 로 바꿀 수 있습니다).")
    return rows


DUP_NAME_MEMO = "같은 이름이 여러 번 나옴 "      # 프롬프트의 보류 조건("같은 이름이 중복됨")이 이 메모를 보고 판단한다. AI 는 문제를 하나씩만 보므로 서버가 알려 줘야 한다


def mark_duplicate_names(rows):
    """같은 이름(이름 열)이 둘 이상이면 해당 행 모두의 메모에 알린다. 이름이 같아도 설명이 다르면 서버 중복 검사는 통과하므로 여기서 잡는다."""
    where = {}
    for r in rows:
        name = (r["fields"].get("이름") or "").strip()
        if name: where.setdefault(name, []).append(r["row"])
    for r in rows:
        name = (r["fields"].get("이름") or "").strip()
        if name and len(where[name]) > 1:
            r["intake"]["메모"].append(f"{DUP_NAME_MEMO}({len(where[name])}번, 행 {', '.join(str(x) for x in where[name])})")
    return rows


DB_EXT = (".db", ".sqlite", ".sqlite3")
SQLITE_MAGIC = b"SQLite format 3\x00"
MAX_DB_ROWS = 1000                  # 표 하나에서 읽는 최대 행 수 (작업 전체 한도는 INTAKE_MAX_PROBLEMS 가 따로 제한한다)
MAX_DB_CELL = 200_000               # 칸 하나의 최대 글자 수


def _db_value(v):
    """DB 값을 글로. JSON 목록(보기·조건이 ["가","나"] 로 저장된 경우)은 줄로 풀고, 바이트·빈 값은 건너뛴다."""
    if v is None or isinstance(v, (bytes, bytearray, memoryview)): return ""
    s = str(v).strip()
    if s.startswith("[") and s.endswith("]"):
        try:
            arr = json.loads(s)
            if isinstance(arr, list) and all(isinstance(x, (str, int, float)) for x in arr): s = "\n".join(str(x) for x in arr)
        except ValueError:
            pass
    return s[:MAX_DB_CELL]


def _worksheet_rows(conn, path, names):
    """학습지를 문제(problems: 문제 번호·구분·문장)와 줄(problem_lines: 요구사항·제공 코드·정답 코드·실행 결과를 한 줄씩), 그림(problem_images)으로 나눠 담은 DB 를 읽는다."""
    from pathlib import Path
    if not {"problems", "problem_lines"} <= set(names): return []
    cols = lambda tb: [r[1] for r in conn.execute(f"PRAGMA table_info({tb})")]
    pc, lc = cols("problems"), cols("problem_lines")
    if not ({"id", "statement"} <= set(pc) and {"problem_id", "kind", "content"} <= set(lc)): return []
    titles = {}
    if "documents" in names and {"id", "title"} <= set(cols("documents")):
        titles = {r[0]: str(r[1] or "") for r in conn.execute("SELECT id, title FROM documents LIMIT 1000")}
    kind_field = {"requirement": "조건", "given_code": "제공코드", "solution_code": "정답코드", "result": "출력"}
    lines = {}
    for pid, kind, content in conn.execute("SELECT problem_id, kind, content FROM problem_lines ORDER BY problem_id, line_no LIMIT 300000"):
        if kind in kind_field: lines.setdefault(pid, {}).setdefault(kind_field[kind], []).append(str(content or ""))
    pics = {}
    if "problem_images" in names and {"problem_id", "data"} <= set(cols("problem_images")):
        for pid, data in conn.execute("SELECT problem_id, data FROM problem_images ORDER BY problem_id, seq LIMIT 2000"):
            if isinstance(data, (bytes, bytearray, memoryview)): pics.setdefault(pid, []).append(bytes(data))
    keys = [c for c in ("id", "document_id", "problem_no", "section", "statement") if c in pc]
    order = ("document_id, " if "document_id" in pc else "") + ("problem_no" if "problem_no" in pc else "id")
    out = []
    for row in conn.execute(f"SELECT {', '.join(keys)} FROM problems ORDER BY {order} LIMIT {MAX_DB_ROWS}"):
        r = dict(zip(keys, row))
        title, no = titles.get(r.get("document_id"), ""), r.get("problem_no") or len(out) + 1
        m = re.match(r"^\s*([A-Za-z+#]{1,6})[ \t]+(\d{1,2})\b", title)
        f = {"이름": f"{m.group(1)}_{m.group(2)}_{int(no):02d}" if m else f"{title} {no}".strip(), "설명": str(r.get("statement") or "").strip()}
        for k, v in lines.get(r["id"], {}).items():
            s = "\n".join(v).strip()
            if s: f[k] = s[:MAX_DB_CELL]
        if r.get("section"): f["비고"] = str(r["section"]).strip()
        if not f["설명"] and len(f) <= 2: continue
        out.append({"source": f"{Path(path).name}:problems", "row": len(out) + 1, "fields": f, "intake": intake_row(f, path), "blobs": pics.get(r["id"], [])})
    return out


def read_sqlite(path):
    """SQLite 파일(.db/.sqlite/.sqlite3)의 표를 엑셀처럼 읽는다. 이름·설명에 해당하는 열이 있는 표만 문제 표로 본다 (뷰는 같은 데이터가 중복되고 무거운 계산이 숨을 수 있어 읽지 않는다)."""
    import sqlite3, time
    from pathlib import Path
    with open(path, "rb") as fh:
        if fh.read(16) != SQLITE_MAGIC: raise SystemExit("SQLite 데이터베이스 파일이 아닙니다. (.db 는 SQLite 만 지원합니다)")
    try:
        conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as e:
        raise SystemExit(f"데이터베이스를 열 수 없습니다: {type(e).__name__}")
    t0 = time.time()
    conn.set_progress_handler(lambda: 1 if time.time() - t0 > 15 else 0, 100000)       # 15초를 넘으면 중단
    try:
        conn.execute("PRAGMA query_only = ON")
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        out, seen_cols = [], []
        for tb in names:
            q = '"' + tb.replace('"', '""') + '"'
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({q})")]
            keymap = {c: canon(c) for c in cols if canon(c)}
            seen_cols.append(f"{tb}({', '.join(cols[:8])})")
            if "이름" not in keymap.values() or "설명" not in keymap.values(): continue
            unknown = [c for c in cols if c not in keymap]
            sel = ", ".join('"' + c.replace('"', '""') + '"' for c in cols)
            for n, row in enumerate(conn.execute(f"SELECT {sel} FROM {q} LIMIT {MAX_DB_ROWS}"), 1):
                f = {}
                for c, v in zip(cols, row):
                    if c in keymap:
                        s = _db_value(v)
                        if s: f[keymap[c]] = s
                if not f.get("이름") and not f.get("설명"): continue
                it = intake_row(f, path)
                if unknown: it["메모"].append(UNKNOWN_COLS_MEMO + ", ".join(unknown))
                out.append({"source": f"{Path(path).name}:{tb}", "row": n, "fields": f, "intake": it})
        if not out: out = _worksheet_rows(conn, path, names)           # 이름·설명 열이 없으면 학습지 DB(문제 표 + 줄 표 + 그림 표) 모양인지 본다
        if not out and not any(True for _ in names):
            raise SystemExit("데이터베이스에 표가 없습니다.")
        if not out:
            raise SystemExit("문제 표를 찾지 못했습니다. 이름(title)·설명(description) 열이 있는 표가 필요합니다. 있는 표: " + "; ".join(seen_cols[:6]))
    except sqlite3.Error as e:
        raise SystemExit(f"데이터베이스를 읽는 중 오류가 났습니다: {type(e).__name__}")
    finally:
        conn.close()
    return mark_duplicate_names(out)


def read_csv(path):
    rows = _csv_rows(path)
    hr = next((i for i, r in enumerate(rows[:12]) if "설명" in [canon(c) for c in r] and "이름" in [canon(c) for c in r]), None)
    if hr is None: raise SystemExit(f"{path}: 이름/설명 머리글을 찾지 못했습니다.")
    cols = {i: canon(c) for i, c in enumerate(rows[hr]) if canon(c)}
    unknown = [c.strip() for i, c in enumerate(rows[hr]) if c.strip() and i not in cols]       # 모르는 열은 버리지 말고 알린다
    out = []
    for n, r in enumerate(rows[hr + 1:], hr + 2):
        f = {key: r[i].strip() for i, key in cols.items() if i < len(r) and r[i].strip()}
        if not f.get("이름") and not f.get("설명"): continue
        it = intake_row(f, path)
        if unknown: it["메모"].append(UNKNOWN_COLS_MEMO + ", ".join(unknown))
        out.append({"source": path.split("/")[-1], "row": n, "fields": f, "intake": it})
    return mark_duplicate_names(out)


def sheet_text(path, limit=300_000):
    """머리글(이름·설명 열)이 없는 자유 형식 엑셀·CSV 의 모든 칸을 문서처럼 읽는다. 행마다 한 줄이고 칸 사이는 공백 3개이다 (워드 표와 같은 약속)."""
    lines = []
    if str(path).lower().endswith((".csv", ".tsv", ".tab")):
        for r in _csv_rows(path):
            cells = [str(c).strip() for c in r if str(c).strip()]
            if cells: lines.append("   ".join(cells))
    else:
        if str(path).lower().endswith((".xlsx", ".xlsm")): safety.check_zip(path)
        sheets = _lite_sheets(path)
        for title, ws in sheets:
            rows = []
            for row in ws.iter_rows(values_only=True):
                cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
                if cells: rows.append("   ".join(cells))
            if rows:
                if len(sheets) > 1: lines.append(title)
                lines += rows
            if sum(len(x) + 1 for x in lines) > limit: break
    text = "\n".join(lines)
    return text[:limit]


def read(path, include_examples=False):
    if path.lower().endswith((".csv", ".tsv", ".tab")): return read_csv(path)
    if str(path).lower().endswith(DB_EXT): return read_sqlite(path)
    if str(path).lower().endswith((".xlsx", ".xlsm")): safety.check_zip(path)        # 압축 폭탄 점검
    sheets = _lite_sheets(path)
    pic_cells = sheet_pictures(path, with_cells=True)[1] if str(path).lower().endswith((".xlsx", ".xlsm")) else set()      # 셀 안 그림 칸 (값이 #VALUE! 로 읽힘)
    out = []
    for _title, ws in sheets:
        if ws.title in SKIP_SHEETS and not include_examples: continue
        try:
            hr, cols = find_header(ws)
        except SystemExit:
            if len(sheets) == 1: raise
            continue
        unknown = [str(c.value).strip() for c in ws[hr] if c.value not in (None, "") and str(c.value).strip() and c.column not in cols]   # 모르는 열은 버리지 말고 알린다
        for r in range(hr + 1, ws.max_row + 1):
            f = {}
            for col, key in cols.items():
                v = ws.cell(r, col).value
                if pic_cells and (ws.title, r, col) in pic_cells and isinstance(v, str) and v.startswith("#"): v = None       # 그림이 든 칸: 글이 아니라 그림이다
                if v is not None and str(v).strip() != "": f[key] = str(v).strip()
            if not f.get("이름") and not f.get("설명"): continue           # 서식만 있는 빈 행
            it = intake_row(f, path)
            if unknown: it["메모"].append(UNKNOWN_COLS_MEMO + ", ".join(unknown))
            out.append({"source": f"{path.split('/')[-1]}:{ws.title}", "row": r, "fields": f, "intake": it})
    _attach_pictures(path, out, include_examples)                         # 시트에 그림이 있으면 그 줄(문제)에 붙인다 (AI 입력·이미지 저장소)
    return mark_duplicate_names(out)
