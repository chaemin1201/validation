"""(2026-11-22 커밋 3) intake 이미지(원본 쪽 캡처·문서 속 그림)의 MinIO 저장소 — 승인 화면에서 원본을 확인하는 용도의 임시 데이터.

- INTAKE_IMAGE_STORE = db(기본) | minio.  minio 면 새 이미지를 비공개 버킷 INTAKE_MINIO_BUCKET(기본 intake)에 올리고 DB 에는 key·mime·크기만 둔다.
  env 만 db 로 되돌리면 새 이미지는 다시 DB(bytea / record b64)로 간다. 이미 올라간 객체는 key 가 남아 있는 한 계속 읽힌다(읽기는 설정과 무관).
- key: {tenant}/{job_id}/cap/{file_idx}_{page}.jpg · {tenant}/{job_id}/img/{seq}_{n}.{ext}. job_id 가 uuid4 라 추측할 수 없고 파일명은 key 에 넣지 않는다(DB 에만).
- 서명 URL: INTAKE_SIGNED_URL_MINUTES(기본 5분). 발급은 service.original_of 에서만 — 라우터 `_item` 소유자 검사를 통과한 요청뿐이다. 익명 읽기 정책은 두지 않는다.
- lifecycle: max(INTAKE_CAPTURE_DAYS, INTAKE_JOB_DAYS) + 1 일 뒤 자동 만료(안전망). 평소 삭제는 service.delete_jobs 가 객체 → DB 순서로 한다.
- 실패 처리는 호출자가 한다: 캡처는 그 쪽만 건너뜀, 문서 그림은 b64 인라인 유지, 처리 전체는 계속.
- 업로드는 put_object 스트림(BytesIO 는 CPython 에서 bytes 를 복사하지 않고 공유) — 파일 전체를 추가로 들고 있지 않는다.
모든 함수는 동기(minio 클라이언트가 동기)이며 비동기 코드에서는 a* 래퍼(asyncio.to_thread)를 쓴다.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import threading
from datetime import timedelta
from typing import Iterable, Optional

from . import config

logger = logging.getLogger(__name__)

_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif", "image/bmp": "bmp", "image/tiff": "tif"}


# ── 설정 (호출 시점에 env 를 읽는다 — config.py 와 같은 방식, 테스트에서 바꿀 수 있게) ──
def store() -> str:
    return os.getenv("INTAKE_IMAGE_STORE", "db").strip().lower()


def enabled() -> bool:
    """새 이미지를 MinIO 에 올리는가."""
    return store() == "minio"


def bucket() -> str:
    return os.getenv("INTAKE_MINIO_BUCKET", "intake").strip()


def signed_minutes() -> int:
    try:
        return max(1, int(os.getenv("INTAKE_SIGNED_URL_MINUTES", "5")))
    except ValueError:
        return 5


def inline_b64() -> bool:
    """응답에 b64 를 함께 넣는가(프론트 전환기 호환). 기본 on."""
    return os.getenv("INTAKE_INLINE_B64", "on").strip().lower() in ("1", "on", "true", "yes")


def lifecycle_days() -> int:
    d = max(config.capture_days(), config.job_days())
    return d + 1 if d > 0 else 0


def ext_of(mime: str) -> str:
    return _EXT.get((mime or "").lower(), "bin")


# ── key ──
def job_prefix(tenant: str, job_id: str) -> str:
    return f"{tenant}/{job_id}/"


def cap_key(tenant: str, job_id: str, file_idx: int, page: int) -> str:
    return f"{tenant}/{job_id}/cap/{int(file_idx)}_{int(page)}.jpg"


def img_key(tenant: str, job_id: str, seq: int, n: int, mime: str) -> str:
    return f"{tenant}/{job_id}/img/{int(seq)}_{int(n)}.{ext_of(mime)}"


# ── 클라이언트 ──
_lock = threading.Lock()
_internal = None
_public = None
_ensured: set[str] = set()


def _clients():
    global _internal, _public
    with _lock:
        if _internal is None:
            from minio import Minio
            from app.image_storage.storage import minio_client
            _internal = minio_client                                  # 기존 media 와 같은 내부 접속(MINIO_ENDPOINT, 루트 키)
            _public = Minio(
                endpoint=os.getenv("MINIO_PUBLIC_ENDPOINT", "haicoder-image.k-university.ai"),
                access_key=os.getenv("MINIO_ACCESS_KEY"), secret_key=os.getenv("MINIO_SECRET_KEY"),
                secure=os.getenv("MINIO_PUBLIC_SECURE", "true").strip().lower() in ("1", "true", "yes", "on"),
                region="us-east-1",
            )
    return _internal, _public


def reset_clients() -> None:
    """테스트용."""
    global _internal, _public
    with _lock:
        _internal = None; _public = None; _ensured.clear()


def ensure_bucket() -> bool:
    """버킷이 없으면 만들고 lifecycle 을 맞춘다(프로세스당 1회). 익명 정책은 두지 않는다. 실패하면 False(호출자가 업로드를 건너뜀)."""
    b = bucket()
    if b in _ensured:
        return True
    try:
        cli, _ = _clients()
        if not cli.bucket_exists(b):
            cli.make_bucket(b)
        days = lifecycle_days()
        if days:
            from minio.commonconfig import ENABLED, Filter
            from minio.lifecycleconfig import Expiration, LifecycleConfig, Rule
            cli.set_bucket_lifecycle(b, LifecycleConfig([Rule(ENABLED, rule_filter=Filter(prefix=""), rule_id="intake-expire", expiration=Expiration(days=days))]))
        _ensured.add(b)
        return True
    except Exception:
        logger.warning("intake 버킷 준비 실패(%s) — 이미지는 DB/인라인으로 남긴다", b, exc_info=True)
        return False


def current_lifecycle_days() -> Optional[int]:
    """점검용: 버킷에 걸린 만료 일수."""
    cli, _ = _clients()
    cfg = cli.get_bucket_lifecycle(bucket())
    if not cfg:
        return None
    for r in cfg.rules:
        if r.expiration is not None and r.expiration.days:
            return int(r.expiration.days)
    return None


# ── 저장·읽기·삭제 (동기) ──
def put_bytes(key: str, data: bytes, mime: str) -> int:
    """스트림 업로드. 실패는 예외로 올린다(호출자가 건너뜀/인라인 유지 판단). 반환: 크기."""
    if not ensure_bucket():
        raise RuntimeError("intake bucket unavailable")
    cli, _ = _clients()
    cli.put_object(bucket(), key, io.BytesIO(data), len(data), content_type=mime or "application/octet-stream")
    return len(data)


def get_bytes(key: str) -> bytes:
    cli, _ = _clients()
    resp = cli.get_object(bucket(), key)
    try:
        return resp.read()
    finally:
        resp.close(); resp.release_conn()


def presign(key: str, seconds: Optional[int] = None) -> str:
    """읽기 서명 URL(공개 엔드포인트). 호출자는 반드시 소유자 검사 뒤에 부른다."""
    _, pub = _clients()
    return pub.presigned_get_object(bucket(), key, expires=timedelta(seconds=seconds) if seconds else timedelta(minutes=signed_minutes()))


def list_keys(prefix: str) -> list[str]:
    cli, _ = _clients()
    return [o.object_name for o in cli.list_objects(bucket(), prefix=prefix, recursive=True)]


def remove_keys(keys: Iterable[str]) -> tuple[int, list[str]]:
    """(지운 수, 실패한 key)."""
    keys = [k for k in keys if k]
    if not keys:
        return 0, []
    from minio.deleteobjects import DeleteObject
    cli, _ = _clients()
    failed = [e.name for e in cli.remove_objects(bucket(), [DeleteObject(k) for k in keys])]   # 이터레이터를 소비해야 실제로 지운다
    return len(keys) - len(failed), failed


def remove_prefix(prefix: str) -> tuple[int, list[str]]:
    """prefix 아래 객체 전부 삭제. 목록 조회 실패는 예외."""
    return remove_keys(list_keys(prefix))


def job_prefixes() -> set[str]:
    """정리 스크립트용: 버킷 안의 {tenant}/{job_id} 목록."""
    cli, _ = _clients()
    out = set()
    for o in cli.list_objects(bucket(), recursive=True):
        parts = o.object_name.split("/")
        if len(parts) >= 3:
            out.add(f"{parts[0]}/{parts[1]}")
    return out


# ── 비동기 래퍼 ──
async def aput_bytes(key: str, data: bytes, mime: str) -> int:
    return await asyncio.to_thread(put_bytes, key, data, mime)


async def aget_bytes(key: str) -> bytes:
    return await asyncio.to_thread(get_bytes, key)


async def apresign(key: str) -> Optional[str]:
    try:
        return await asyncio.to_thread(presign, key)
    except Exception:
        logger.warning("intake 서명 URL 발급 실패 key=%s", key, exc_info=True)
        return None


async def aremove_prefix(prefix: str) -> tuple[int, list[str]]:
    return await asyncio.to_thread(remove_prefix, prefix)


async def aremove_keys(keys: Iterable[str]) -> tuple[int, list[str]]:
    return await asyncio.to_thread(remove_keys, list(keys))


# ── 문서 그림(record["이미지"]) 변환 ──
def offload_record_images(rec: dict, tenant: str, job_id: str, seq: int) -> tuple[dict, int, int]:
    """record 의 그림 [{n,mime,b64}] 를 MinIO 로 올리고 [{n,mime,key,size}] 로 바꾼다. 실패한 그림은 b64 그대로 둔다(인라인 유지).
    반환: (새 record, 올린 수, 실패 수). 동기 — 스레드에서 부른다."""
    import base64
    imgs = rec.get("이미지")
    if not isinstance(imgs, list) or not imgs:
        return rec, 0, 0
    out, ok, bad = [], 0, 0
    for g in imgs:
        if not isinstance(g, dict) or not g.get("b64") or g.get("key"):
            out.append(g); continue
        try:
            data = base64.b64decode(g["b64"])
            key = img_key(tenant, job_id, seq, int(g.get("n") or len(out) + 1), g.get("mime") or "image/png")
            size = put_bytes(key, data, g.get("mime") or "image/png")
            out.append({"n": g.get("n"), "mime": g.get("mime"), "key": key, "size": size}); ok += 1
        except Exception:
            logger.warning("문서 그림 업로드 실패(b64 인라인 유지) job=%s seq=%s n=%s", job_id, seq, g.get("n"), exc_info=True)
            out.append(g); bad += 1
    return {**rec, "이미지": out}, ok, bad


def image_weight(imgs) -> int:
    """정렬 가중치용 크기(글자 수 기준과 비슷하게: b64 길이 또는 size×4/3)."""
    w = 0
    for g in imgs or []:
        if isinstance(g, dict):
            w += len(g.get("b64") or "") or int((g.get("size") or 0) * 4 / 3)
    return w


async def load_images(imgs) -> list[dict]:
    """AI 입력용: [{n,mime,b64}] 로 만든다. key 만 있는 그림은 MinIO 에서 읽어 b64 로(호출 동안만 메모리에). 읽기 실패한 그림은 뺀다."""
    import base64
    out = []
    for g in imgs or []:
        if not isinstance(g, dict):
            continue
        if g.get("b64"):
            out.append({"n": g.get("n"), "mime": g.get("mime"), "b64": g["b64"]}); continue
        if g.get("key"):
            try:
                data = await aget_bytes(g["key"])
                out.append({"n": g.get("n"), "mime": g.get("mime"), "b64": base64.b64encode(data).decode("ascii")})
            except Exception:
                logger.warning("문서 그림 읽기 실패(AI 입력에서 제외) key=%s", g.get("key"), exc_info=True)
    return out
