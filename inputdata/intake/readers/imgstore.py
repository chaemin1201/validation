"""(2026-11-23) 그림 저장소 어댑터 — intake 새 버전(images.attach · images.shrink · llm._b64 · fix.put_image_url · service 의 for_ai)이 부르는
imgstore 인터페이스를 이 저장소의 blobstore(비공개 버킷 intake, {tenant}/{job_id}/… 키, 서명 URL, gc.py 고아 정리)에 잇는다. 저장 정책은 blobstore 가 정한다.

- enabled() 는 항상 False: 파싱 중에는 작업 ID 가 없어 tenant/job 키를 만들 수 없으므로 그림을 바로 올리지 않는다.
  그림은 지금처럼 service.create_job 의 blobstore.offload_record_images 가 올리고(INTAKE_IMAGE_STORE=minio), 삭제·gc 는 작업 단위로 동작한다.
  같은 이유로 put()·ref() 는 쓰지 않는다(부르면 오류).
- 결과: fix.put_image_url(문제 그림 칸 image_url 자동 채움)은 지금은 동작하지 않는다. 영구 공개 URL 이 필요한데 비공개 버킷의 서명 URL(5분)은 쓸 수 없다.
  후속: 승인(등록) 시 그림을 media 버킷으로 복사해 image_url 을 채우는 단계가 필요하다.
- key 가 있는 그림 읽기(get · bytes_of · b64_of · for_ai), 삭제(delete), 서명 URL(url) 은 blobstore 로 넘긴다.
- key_of() 는 항상 None: 캡처 키는 intake_capture.data 가 아니라 object_key 열에 있다(3825c9f1).
"""
import base64

from .. import blobstore


def mode():
    return blobstore.store()


def enabled():
    """파싱 중 즉시 업로드는 하지 않는다 (설명은 모듈 머리). blobstore 사용 여부는 blobstore.enabled()."""
    return False


def send_url():
    """AI 에는 항상 바이트(b64)로 보낸다 — 비공개 버킷이라 AI 서비스가 링크를 열 수 없다."""
    return False


def put(data, mime):
    raise RuntimeError("imgstore.put 은 쓰지 않습니다 — 그림은 service.create_job 의 blobstore.offload_record_images 가 tenant/job 키로 올립니다.")


def get(key):
    return blobstore.get_bytes(key)


def url(key, seconds=None):
    """서명 URL (INTAKE_SIGNED_URL_MINUTES). 소유자 검사를 통과한 요청에서만 부를 것."""
    return blobstore.presign(key, seconds)


def delete(keys):
    keys = [k for k in dict.fromkeys(keys or []) if k]
    if not keys: return
    try: blobstore.remove_keys(keys)
    except Exception: pass                      # 못 지운 것은 gc.py / lifecycle 이 지운다


def bytes_of(im):
    if im.get("key") and not im.get("b64"): return get(im["key"])
    return base64.b64decode(im["b64"])


def b64_of(im):
    return im["b64"] if im.get("b64") else base64.b64encode(get(im["key"])).decode("ascii")


def for_ai(images):
    """AI 호출에 붙일 그림 목록: 항상 b64. key 만 있는 그림은 읽어서 채우고, 읽지 못한 그림은 뺀다 (service 는 보통 blobstore.load_images 로 이미 b64 를 채워 넘긴다)."""
    out = []
    for im in images or []:
        if not isinstance(im, dict): continue
        if im.get("b64"): out.append(im); continue
        if im.get("key"):
            try: out.append({**{k: v for k, v in im.items() if k != "key"}, "b64": b64_of(im)})
            except Exception: continue
    return out


def ref(key):
    raise RuntimeError("imgstore.ref 는 쓰지 않습니다 — 캡처 키는 intake_capture.object_key 열에 둡니다.")


def key_of(data):
    return None


def keys_in(rec):
    """레코드가 가리키는 MinIO 키."""
    return [g["key"] for g in ((rec or {}).get("이미지") or []) if isinstance(g, dict) and g.get("key")]
