# app/inputdata/intake/router.py
"""문제지 업로드 → AI 정리 → 승인 후 등록 API."""
import logging
import os
import shutil
import tempfile
from typing import Annotated, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import asyncio

from app.database import get_db
from app.security.security import get_current_user
from . import config, parsing, service
from .checks import pipeline
from .models import ALL_STATUS, EDITABLE, REGISTERED, SKIPPED, DUPLICATE, IntakeItem, IntakeJob

router = APIRouter(prefix="/intake", tags=["intake"])


def _uid(current_user):
    uid = current_user.get("sub")
    if not uid: raise HTTPException(status_code=401, detail="User ID not found in token")
    return uid


async def _job(db, job_id, uid):
    j = await db.get(IntakeJob, job_id)
    if not j or j.maker_id != uid: raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.")
    return j


async def _item(db, item_id, uid):
    it = await db.get(IntakeItem, item_id)
    if not it: raise HTTPException(status_code=404, detail="항목을 찾을 수 없습니다.")
    j = await db.get(IntakeJob, it.job_id)
    if not j or j.maker_id != uid: raise HTTPException(status_code=404, detail="항목을 찾을 수 없습니다.")
    return it


def _cleanup(blobs):
    """_read_uploads 가 만든 임시 폴더를 지운다."""
    for b in blobs or []:
        if isinstance(b[2], str):
            shutil.rmtree(os.path.dirname(b[2]), ignore_errors=True); return


async def _read_uploads(files):
    """올린 파일을 검사해 (표 위치, 원본 위치, [(이름, 확장자, 임시 파일 경로)]) 로. 작업 생성과 미리보기가 함께 쓴다.
    파일을 메모리에 통째로 읽지 않고 1MB 씩 임시 파일로 옮겨 쓴다(파일 3개가 한꺼번에 메모리를 차지하지 않음). 끝나면 _cleanup(blobs)."""
    if not files: raise HTTPException(status_code=400, detail="파일을 올려 주세요.")
    if len(files) > config.max_files(): raise HTTPException(status_code=400, detail=f"한 번에 최대 {config.max_files()}개 파일까지 올릴 수 있습니다.")
    tables, originals, blobs = [], [], []
    d = tempfile.mkdtemp(prefix="intake_")
    try:
        for f in files:
            k = parsing.kind_of(f.filename or "")
            if not k:
                raise HTTPException(status_code=400, detail=f"지원하지 않는 파일 형식입니다: {os.path.basename(f.filename or '')} (받을 수 있는 형식: {', '.join(config.allowed_exts()) if config.allowed_exts() else '모든 읽기 형식'})")
            ext = os.path.splitext(f.filename)[1].lower()
            path = os.path.join(d, f"f{len(blobs)}{ext}"); size = 0
            with open(path, "wb") as out:
                while True:
                    chunk = await f.read(1024 * 1024)
                    if not chunk: break
                    size += len(chunk)
                    if size > config.max_file_bytes(): raise HTTPException(status_code=413, detail=f"파일이 너무 큽니다. (최대 {config.max_file_bytes() // 1024 // 1024}MB)")
                    out.write(chunk)
            if not size: raise HTTPException(status_code=400, detail=f"빈 파일입니다: {os.path.basename(f.filename or '')}")
            (tables if k == "table" else originals).append(len(blobs)); blobs.append((os.path.basename(f.filename), ext, path))
    except BaseException:
        shutil.rmtree(d, ignore_errors=True); raise
    return tables, originals, blobs


async def _parse_uploads(tables, originals, blobs, pattern, warnings=None):
    """(입력 종류, 문제 레코드 목록): 미리보기용. 사용자가 고칠 수 있는 입력 문제는 422 로. 읽지 못한 파일(다른 파일은 처리됨)은 warnings 목록에 안내를 더한다."""
    def parse():
        paths = [b[2] for b in blobs]
        kind, recs, errs = parsing.build_records_per_file([paths[i] for i in tables], [paths[i] for i in originals], pattern, [blobs[i][0] for i in originals], table_labels=[blobs[i][0] for i in tables])
        if warnings is not None: warnings.extend(f"읽지 못한 파일이 있어 건너뛰었습니다. {e}" for e in errs)
        return kind, recs
    await pipeline.PARSE_GATE.enter()                             # 파일 읽기가 한꺼번에 몰리지 않게 차례를 기다린다 (거절하지 않음)
    try:
        return await asyncio.to_thread(parse)
    except parsing.IntakeInputError as e:
        raise HTTPException(status_code=422, detail=str(e))
    finally:
        pipeline.PARSE_GATE.leave()


@router.post("/preview")
async def preview(current_user: Annotated[dict, Depends(get_current_user)], files: List[UploadFile] = File(...), pattern: Optional[str] = Form(None)):
    """AI 를 부르지 않고 파일을 문제로 나눈 결과만 보여 준다 (문제 수, 경계, 그림, 이상 징후). 작업을 만들지 않고 아무것도 저장하지 않는다."""
    _uid(current_user)
    tables, originals, blobs = await _read_uploads(files)
    try:
        kind, records = await _parse_uploads(tables, originals, blobs, pattern)
    finally:
        _cleanup(blobs)                                              # 임시 파일은 읽고 바로 지운다
    return parsing.preview_of(kind, records)


@router.post("/jobs", status_code=202)
async def create_job(background_tasks: BackgroundTasks, current_user: Annotated[dict, Depends(get_current_user)],
                     files: List[UploadFile] = File(...), mode: str = Form("none"), auto_register: bool = Form(True),
                     pattern: Optional[str] = Form(None), db: AsyncSession = Depends(get_db)):
    """엑셀(표) 하나 + 원본(PDF·워드·한글·txt·md) 여러 개 중 하나 이상을 올린다. 둘 다 올리면 서로 대조한다."""
    uid = _uid(current_user)
    tables, originals, blobs = await _read_uploads(files)                # 임시 파일로 받는다 (메모리에 올리지 않음)
    scheduled = False
    try:
        if await service.active_job(db, uid): raise HTTPException(status_code=409, detail="처리 중인 작업이 있습니다. 끝난 뒤에 올려 주세요.")
        if mode not in ("none", "space", "regex", "hard", "exact", "partial", "soft"):
            raise HTTPException(status_code=400, detail="채점 모드 값이 올바르지 않습니다.")
        jobs_used, problems_used = await service.daily_usage(db, uid)
        paths = [b[2] for b in blobs]
        errs = []
        gen = parsing.iter_units([paths[i] for i in tables], [paths[i] for i in originals], pattern, [blobs[i][0] for i in originals], table_labels=[blobs[i][0] for i in tables], errors=errs)
        jobs, dups, rejected, entries, seen_h = [], [], [], [], {}
        added, total_records, skipped_cols = 0, 0, set()
        await pipeline.PARSE_GATE.enter()                                # 파일 읽기가 한꺼번에 몰리지 않게 차례를 기다린다 (거절하지 않음)
        try:
            while True:                                                  # 묶음(파일)을 하나씩 읽어 저장하고 버린 뒤 다음을 읽는다 (읽기를 나눠서 한다)
                try:
                    u = await asyncio.to_thread(next, gen, None)
                except parsing.IntakeInputError as e:
                    raise HTTPException(status_code=422, detail=str(e))
                except Exception as e:                                    # 예상 밖 오류: 이미 만든 작업은 그대로 처리하고 안내만 남긴다
                    logging.getLogger(__name__).exception("파일 읽기 실패")
                    errs.append(f"파일을 읽는 중 오류가 났습니다 ({type(e).__name__})"); break
                if u is None: break
                recs = u["records"]
                h = parsing.unit_hash(u)
                same = seen_h.get(h) or await service.find_same_content(db, uid, h)
                if same:                                                 # 이미 올린 내용과 같은 파일은 다시 만들지 않고 '이미 존재함'으로 알린다
                    dups.append({"files": u["files"], "message": "이미 올린 파일입니다(같은 내용).", "existing_job_id": same["job_id"], "existing_status": same["status"], "uploaded_at": same.get("created_at")})
                    continue
                est = sum(int(r.get("생성수") or 1) for r in recs)       # 강의자료는 만들어질 문제 수로 센다
                if len(recs) > config.max_problems():
                    rejected.append((413, f"{', '.join(u['files'])}: 문제가 {len(recs)}개입니다. 한 번에 최대 {config.max_problems()}개까지 처리할 수 있으니 나눠서 올려 주세요.")); continue
                if config.daily_jobs() and jobs_used + len(jobs) + 1 > config.daily_jobs():
                    rejected.append((429, f"오늘 올릴 수 있는 작업 수({config.daily_jobs()}개)를 넘습니다. 파일마다 작업이 하나씩 만들어지며 오늘 이미 {jobs_used}개를 사용했습니다. 내일 다시 올려 주세요.")); continue
                if config.daily_problems() and problems_used + added + est > config.daily_problems():
                    rejected.append((429, f"오늘 처리할 수 있는 문제 수({config.daily_problems()}개)를 넘습니다. 오늘 이미 {problems_used + added}개를 올렸고 {', '.join(u['files'])} 는 {est}개입니다. 나눠서 내일 올려 주세요.")); continue
                for r in recs:
                    for m in ((r.get("intake") or {}).get("메모") or []):
                        if m.startswith("인식하지 못해 무시한 열: "): skipped_cols.add(m.split(": ", 1)[1])
                st = "processing" if not jobs else "queued"
                jid = await service.create_job(db, uid, u["kind"], u["files"], recs, mode, auto_register, status=st, content_hash=h, problems=est)
                seen_h[h] = {"job_id": jid, "status": st}
                jobs.append({"job_id": jid, "files": u["files"], "status": st, "input_kind": u["kind"], "records_total": len(recs)})
                entries.append((jid, [blobs[originals[k]] for k in u["original_idx"]]))
                added += est; total_records += len(recs)
                del u, recs                                              # 이 묶음의 레코드(그림 포함)는 저장했으니 버린다
        finally:
            pipeline.PARSE_GATE.leave()
        if not jobs:
            if dups and not rejected: raise HTTPException(status_code=409, detail="이미 올린 파일입니다: " + ", ".join(", ".join(d["files"]) + f" (작업 {d['existing_job_id']})" for d in dups))
            if rejected: raise HTTPException(status_code=rejected[0][0], detail=rejected[0][1])
            raise HTTPException(status_code=422, detail="; ".join(errs) or "처리할 문제를 찾지 못했습니다.")
        background_tasks.add_task(service.process_queue, [j["job_id"] for j in jobs])
        background_tasks.add_task(service.capture_jobs, entries, os.path.dirname(blobs[0][2]))        # 원본 캡처는 AI 처리와 따로 (임시 폴더는 캡처가 끝나면 지움)
        scheduled = True
        parse_warns = [f"읽지 못한 파일이 있어 건너뛰었습니다. {e}" for e in errs] + [f"한도 때문에 건너뛰었습니다. {d}" for _, d in rejected]
        return {"job_id": jobs[0]["job_id"], "status": "processing", "input_kind": jobs[0]["input_kind"], "records_total": total_records, "jobs": jobs, "duplicates": dups,
                "warnings": [f"이미 올린 파일이라 건너뛰었습니다: {', '.join(d['files'])} (작업 {d['existing_job_id']})" for d in dups] + [f"엑셀 열을 인식하지 못해 값이 AI 에게 전달되지 않았습니다: {c}" for c in sorted(skipped_cols)] + parse_warns,
                "message": f"파일 {len(jobs)}개를 하나씩 차례로 처리합니다(파일마다 작업이 하나씩 만들어짐). 끝난 작업부터 GET /intake/jobs/{{job_id}} 와 /items 로 결과를 볼 수 있고, 기다리는 작업은 status 가 queued 입니다."}
    finally:
        if not scheduled: _cleanup(blobs)


@router.get("/jobs")
async def list_jobs(background_tasks: BackgroundTasks, current_user: Annotated[dict, Depends(get_current_user)], limit: int = Query(20, ge=1, le=100), db: AsyncSession = Depends(get_db)):
    uid = _uid(current_user)
    ids = await service.recover_stalled(db, uid)                        # 서버가 재시작되어 멈춘 작업이 있으면 조회하는 김에 자동으로 이어서 처리한다
    if ids: background_tasks.add_task(service.process_queue, ids)
    jobs = (await db.execute(select(IntakeJob).where(IntakeJob.maker_id == uid).order_by(IntakeJob.created_at.desc()).limit(limit))).scalars().all()
    return [await service.job_summary(db, j) for j in jobs]


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, background_tasks: BackgroundTasks, current_user: Annotated[dict, Depends(get_current_user)], db: AsyncSession = Depends(get_db)):
    uid = _uid(current_user)
    await _job(db, job_id, uid)
    ids = await service.recover_stalled(db, uid)                        # 서버가 재시작되어 멈춘 작업이 있으면 조회하는 김에 자동으로 이어서 처리한다
    if ids: background_tasks.add_task(service.process_queue, ids)
    return await service.job_summary(db, await _job(db, job_id, uid))


@router.get("/jobs/{job_id}/items")
async def list_items(job_id: str, current_user: Annotated[dict, Depends(get_current_user)], status: Optional[str] = Query(None),
                     limit: int = Query(200, ge=1, le=200), offset: int = Query(0, ge=0), with_record: bool = False,
                     file: Optional[str] = Query(None, description="파일 이름. 그 파일의 문제만 (파일마다 결과를 따로 볼 때)"),
                     db: AsyncSession = Depends(get_db), ai_filled: bool = Query(False, description="true 면 AI 가 직접 푼 정답이 들어 있는 항목만 (등록 뒤 사후 점검용)")):
    job = await _job(db, job_id, _uid(current_user))
    plan = (job.options or {}).get("files") or []
    if status and status not in ALL_STATUS: raise HTTPException(status_code=400, detail=f"status 는 {', '.join(ALL_STATUS)} 중 하나여야 합니다.")
    q = select(IntakeItem).where(IntakeItem.job_id == job_id)
    if file:
        rng = next((f for f in plan if f["name"] == file), None)
        if rng is None: raise HTTPException(status_code=404, detail="그 이름의 파일이 이 작업에 없습니다.")
        q = q.where(IntakeItem.seq.between(rng["from"], rng["to"]))
    if status: q = q.where(IntakeItem.status == status)
    q = q.order_by(IntakeItem.seq, IntakeItem.out_index)
    if ai_filled:                                             # JSON 안의 값이라 DB 에서 거르지 않고 가져와서 거른다 (작업당 최대 200문제)
        items = [i for i in (await db.execute(q)).scalars().all() if service.ai_filled(i.review)][offset:offset + limit]
    else:
        items = (await db.execute(q.limit(limit).offset(offset))).scalars().all()
    return [service.item_out(i, with_record, plan) for i in items]


class ApproveItem(BaseModel):
    item_id: int
    payload: Optional[dict] = None                  # 고친 문제 내용 전체 (없으면 지금 저장된 내용 그대로)
    subtitle: Optional[str] = Field(None, max_length=300)
    intent: Optional[str] = Field(None, max_length=500)


class ApproveRequest(BaseModel):
    item_ids: Optional[List[int]] = None            # 등록할 항목들
    all: bool = False                               # true 면 승인할 수 있는 모든 항목
    edits: Optional[List[ApproveItem]] = None       # 고친 항목들: 서버가 먼저 저장·재검사한 뒤 같이 등록 (한 번의 호출로 수정+등록)


@router.post("/jobs/{job_id}/approve")
async def approve(job_id: str, body: ApproveRequest, current_user: Annotated[dict, Depends(get_current_user)], db: AsyncSession = Depends(get_db)):
    """한꺼번에 등록한다: 문제마다 호출하지 말고 item_ids / all / edits 로 여러 개를 한 번에 보낸다."""
    uid = _uid(current_user)
    await _job(db, job_id, uid)
    if not body.all and not body.item_ids and not body.edits: raise HTTPException(status_code=400, detail="item_ids·edits 를 보내거나 all=true 로 보내세요.")
    edits = [e.model_dump() for e in (body.edits or [])]
    results = await service.approve(db, job_id, uid, None if body.all else (body.item_ids or []), edits)
    cnt = {}
    for r in results: cnt[r["result"]] = cnt.get(r["result"], 0) + 1
    return {"summary": cnt, "results": results}


@router.delete("/jobs/{job_id}")
async def delete_job(job_id: str, current_user: Annotated[dict, Depends(get_current_user)], db: AsyncSession = Depends(get_db)):
    """작업 데이터(작업·항목·캡처)를 지운다. 등록된 문제는 그대로다. 처리 중인 작업은 지울 수 없다."""
    job = await _job(db, job_id, _uid(current_user))
    if job.status in ("processing", "queued") and service._alive(job): raise HTTPException(status_code=409, detail="처리 중인 작업은 지울 수 없습니다.")
    await service.delete_jobs(db, [job_id])
    return {"job_id": job_id, "deleted": True}


@router.post("/jobs/{job_id}/resume", status_code=202)
async def resume(job_id: str, background_tasks: BackgroundTasks, current_user: Annotated[dict, Depends(get_current_user)], db: AsyncSession = Depends(get_db)):
    uid = _uid(current_user)
    job = await _job(db, job_id, uid)
    if job.status in ("processing", "queued") and service._alive(job): raise HTTPException(status_code=409, detail="아직 처리 중이거나 차례를 기다리는 중입니다.")   # paused·failed·done 은 heartbeat 가 남아 있어도 돌고 있지 않다
    other = await service.active_job(db, uid)
    if other and other.job_id != job_id: raise HTTPException(status_code=409, detail="다른 작업이 처리 중입니다.")
    n = await service.reset_failed(db, job_id)
    job = await _job(db, job_id, uid)
    job.status = "processing"; await db.commit()
    background_tasks.add_task(service.process_job, job_id)
    return {"job_id": job_id, "status": "processing", "retry_items": n}


class EditRequest(BaseModel):
    payload: dict                                   # 문제 내용 전체 (image_url 포함 가능)
    subtitle: Optional[str] = Field(None, max_length=300)     # 부제목 (보내지 않으면 그대로)
    intent: Optional[str] = Field(None, max_length=500)       # 문제의도 (보내지 않으면 그대로)


@router.patch("/items/{item_id}")
async def edit_item(item_id: int, body: EditRequest, current_user: Annotated[dict, Depends(get_current_user)], db: AsyncSession = Depends(get_db)):
    it = await _item(db, item_id, _uid(current_user))
    if it.status not in EDITABLE: raise HTTPException(status_code=409, detail=f"상태가 {it.status} 인 항목은 수정할 수 없습니다.")
    await service.edit_payload(db, it, body.payload, body.subtitle, body.intent)
    return service.item_out(await _item(db, item_id, _uid(current_user)))


@router.get("/items/{item_id}/original")
async def item_original(item_id: int, current_user: Annotated[dict, Depends(get_current_user)], db: AsyncSession = Depends(get_db)):
    """승인 화면 왼쪽 패널: 이 문제의 원본 글·엑셀 열·그림 (그림은 base64)."""
    return await service.original_of(db, await _item(db, item_id, _uid(current_user)))


class SuggestRequest(BaseModel):
    payload: Optional[dict] = None                  # 화면의 지금 내용(저장 전 수정 포함). 없으면 저장된 내용
    conditions: Optional[List[str]] = None          # 화면의 지금 조건 목록. 없으면 payload 의 problem_condition


@router.post("/items/{item_id}/suggest-conditions")
async def suggest_conditions(item_id: int, current_user: Annotated[dict, Depends(get_current_user)], body: SuggestRequest = SuggestRequest(),
                             db: AsyncSession = Depends(get_db)):
    """조건 예시 하나 (정답 코드가 쓰는 것 기준, AI 호출 없음). 항목 조회의 condition_example 과 같은 값이며, 화면에서 정답 코드를 고친 뒤 다시 받을 때 쓴다."""
    return service.suggest_conditions(await _item(db, item_id, _uid(current_user)), body.payload, body.conditions)


@router.post("/items/{item_id}/reject")
async def reject_item(item_id: int, current_user: Annotated[dict, Depends(get_current_user)], db: AsyncSession = Depends(get_db)):
    it = await _item(db, item_id, _uid(current_user))
    if it.status in (REGISTERED, DUPLICATE): raise HTTPException(status_code=409, detail="이미 등록된 항목은 제외할 수 없습니다.")
    it.status = SKIPPED; await db.commit()
    return {"item_id": item_id, "status": SKIPPED}
