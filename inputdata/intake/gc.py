"""intake 버킷 정리 스크립트: 작업 행(intake_job)이 없는 {tenant}/{job_id}/ 객체를 지운다.
delete_jobs 가 객체 삭제에 실패해 DB 행을 남겨 둔 경우(경고 로그)와, 반대로 행만 사라진 고아 객체를 정리한다.
    docker exec aprofi_fastapi_ver_prod python -m app.inputdata.intake.gc [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.tenant.context import bypass_tenant

from . import blobstore
from .models import IntakeJob


async def run(dry_run: bool) -> dict:
    prefixes = await asyncio.to_thread(blobstore.job_prefixes)
    with bypass_tenant():
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(IntakeJob.tenant, IntakeJob.job_id))).all()
    live = {f"{t}/{j}" for t, j in rows}
    orphans = sorted(p for p in prefixes if p not in live)
    removed, failed = 0, []
    if not dry_run:
        for p in orphans:
            n, f = await blobstore.aremove_prefix(p + "/")
            removed += n; failed += f
    return {"bucket": blobstore.bucket(), "prefixes": len(prefixes), "live_jobs": len(live), "orphan_prefixes": orphans, "removed_objects": removed, "failed": failed, "dry_run": dry_run}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    print(asyncio.run(run(a.dry_run)))
