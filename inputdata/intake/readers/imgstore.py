"""그림(문서 속 그림·쪽 캡처)을 MinIO 에 두고 레코드에는 키(링크)만 남긴다.

왜: 그림 바이트(base64)가 레코드·DB·AI 호출 준비로 메모리를 오가며 서버를 무겁게 했다. MinIO 에 한 번 올리면
  - 파싱 때: 꺼낸 즉시 올리고 메모리에서 버린다 (레코드에는 {"n","mime","key","size"} 만)
  - AI 호출 때: 짧게 유효한 링크(presigned GET)만 보낸다 → AI 가 직접 내려받으므로 서버 메모리를 쓰지 않는다
  - 화면 때: 링크만 내려 주고 브라우저가 직접 받는다
설정: INTAKE_IMAGE_STORE = auto(기본: MINIO_ACCESS_KEY 가 있으면 minio, 없으면 memory) | minio | memory
      INTAKE_IMAGE_SEND  = url(기본: AI 에 링크를 보냄) | bytes(서버가 내려받아 base64 로 보냄. AI 가 MinIO 에 닿지 못할 때)
      INTAKE_IMAGE_PREFIX = "" (기본. 기존 /media/storage 와 같은 YYYY/MM/DD/uuid.확장자 키. 값을 주면 그 접두 아래에 둔다)
키·버킷·링크는 기존 이미지 업로드(/media/storage)와 같다: 버킷 MINIO_BUCKET, 링크 PUBLIC_BASE_URL/media/{키} (기본 https://api.hellonoti.co.kr)
"""
import base64, os, uuid
from datetime import datetime

_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}
ALLOWED_CONTENT_TYPES = set(_EXT)          # 기존 이미지 업로드(/media/storage/presigned-url)와 같은 허용 형식
MAX_SIZE = 10 * 1024 * 1024                # 같은 크기 한도(10MB)
_BACKEND = None            # 시험용으로 바꿔 끼우는 자리: put(key, data, mime) / get(key) / url(key, seconds) / delete(keys)


def mode():
    v = os.environ.get("INTAKE_IMAGE_STORE", "auto").strip().lower()
    if v == "auto": return "minio" if os.environ.get("MINIO_ACCESS_KEY") else "memory"
    return "minio" if v == "minio" else "memory"


def enabled():
    return _BACKEND is not None or mode() == "minio"


def send_url():
    """AI 호출에 링크를 보낼지 (아니면 바이트)."""
    return os.environ.get("INTAKE_IMAGE_SEND", "url").strip().lower() != "bytes"


def prefix():
    p = os.environ.get("INTAKE_IMAGE_PREFIX", "").strip().strip("/")
    return p + "/" if p else ""


def new_key(mime):
    now = datetime.now()
    return f"{prefix()}{now.year}/{now.month:02d}/{now.day:02d}/{uuid.uuid4().hex}{_EXT.get(mime, '.bin')}"


class _Minio:
    """app.image_storage.storage 의 클라이언트를 쓴다. 내부 주소로 올리고(minio_client), 화면·AI 가 받을 링크는 기존 확인 API(/media/storage/confirm)가 주는 것과 같은 공개 주소 PUBLIC_BASE_URL/media/{키} 이다."""
    def __init__(self):
        from app.image_storage.storage import minio_client, BUCKET_NAME      # 기존 이미지 업로드와 같은 클라이언트·버킷 (버킷은 기존 설정이 만든다: 여기서 새로 만들지 않는다)
        self.c, self.bucket = minio_client, BUCKET_NAME
        self.base = os.getenv("PUBLIC_BASE_URL", "https://api.hellonoti.co.kr").rstrip("/")

    def put(self, key, data, mime):
        import io
        self.c.put_object(self.bucket, key, io.BytesIO(data), len(data), content_type=mime)
        st = self.c.stat_object(self.bucket, key)              # 기존 confirm API 와 같은 확인: 올라간 것을 확인한 뒤에만 링크를 쓴다
        if getattr(st, "size", len(data)) != len(data): raise IOError("이미지 저장소에 올라간 크기가 다릅니다")

    def get(self, key):
        r = self.c.get_object(self.bucket, key)
        try: return r.read()
        finally:
            r.close(); r.release_conn()

    def url(self, key, seconds):
        return f"{self.base}/media/{key}"

    def delete(self, keys):
        from minio.deleteobjects import DeleteObject
        for err in self.c.remove_objects(self.bucket, [DeleteObject(k) for k in keys]): pass


def backend():
    global _BACKEND
    if _BACKEND is None: _BACKEND = _Minio()
    return _BACKEND


def put(data, mime):
    """바이트를 올리고 키를 돌려준다."""
    if mime not in ALLOWED_CONTENT_TYPES: raise ValueError("허용되지 않는 이미지 형식입니다")        # 기존 presigned-url API 와 같은 검사
    if len(data) > MAX_SIZE: raise ValueError("이미지가 너무 큽니다 (최대 10MB)")
    key = new_key(mime)
    backend().put(key, data, mime)
    return key


def get(key):
    return backend().get(key)


def url(key, seconds=3600):
    return backend().url(key, seconds)


def delete(keys):
    """키 목록을 지운다. 실패해도 조용히 넘어간다(수명 규칙이 남은 것을 지운다)."""
    keys = [k for k in dict.fromkeys(keys) if k]
    if not keys: return
    try: backend().delete(keys)
    except Exception: pass


def bytes_of(im):
    """그림 항목의 바이트: 키가 있으면 MinIO 에서, 없으면 b64 에서."""
    if im.get("key"): return get(im["key"])
    return base64.b64decode(im["b64"])


def b64_of(im):
    return im["b64"] if im.get("b64") else base64.b64encode(get(im["key"])).decode("ascii")


def for_ai(images):
    """AI 호출에 붙일 그림 목록으로 바꾼다. 키가 있으면 링크(또는 설정에 따라 바이트)를 넣고, 레코드 쪽 항목은 건드리지 않는다. 호출할 때마다 새 링크를 만든다."""
    out = []
    for im in images or []:
        if im.get("key") and not im.get("b64"):
            out.append({"n": im.get("n"), "mime": im["mime"], "key": im["key"], "url": url(im["key"])} if send_url() else {"n": im.get("n"), "mime": im["mime"], "b64": b64_of(im)})
        else:
            out.append(im)
    return out


_REF = b"minio:"


def ref(key):
    """DB 의 바이트 열(intake_capture.data)에 넣을 값: 'minio:{키}'. 열을 새로 만들지 않기 위해 키를 이 열에 둔다 (그림 바이트는 이 글자로 시작하지 않는다)."""
    return _REF + key.encode("utf-8")


def key_of(data):
    """ref() 로 둔 값이면 키, 그림 바이트 자체이면 None."""
    return bytes(data)[len(_REF):].decode("utf-8") if data and bytes(data[:len(_REF)]) == _REF else None


def keys_in(rec):
    """레코드가 가리키는 MinIO 키."""
    return [g["key"] for g in (rec.get("이미지") or []) if isinstance(g, dict) and g.get("key")]
