import base64
import io
import json
import logging
import os
import time
from pathlib import Path
import requests as http_requests
from PIL import Image
from google.oauth2 import service_account
from google.auth.transport.requests import Request as GoogleAuthRequest

logger = logging.getLogger(__name__)

ROOT_FOLDER_ID = os.environ.get('GOOGLE_DRIVE_FOLDER_ID', '1lc-gyNPuUUjQUL1s07-fMcBumyWnSEIW')
SUBFOLDER_IDS = {
    'avatar': '1rDLDeJ1NHbVcUFEEFnNMtpvts0jhP70Y',
    'prescription': '1Pw0ABWq5rGfQdkXEoEKOesfSgwudGRc_',
    'fetcher_id': '1M4NMSmFdjCTCct_cdZ0jpSIt_-lEWSiD',
}
BACKEND_PUBLIC_URL = os.environ.get('BACKEND_PUBLIC_URL', 'https://cura-backend-dtfj.onrender.com').rstrip('/')

# ponytail: in-process dict cache (512 items), swap for Redis/CDN only if multi-worker memory matters
_MEDIA_CACHE: dict[str, tuple[bytes, str]] = {}
_CREDS = None


def _load_db_credentials_json() -> dict | None:
    try:
        from .models import AuditLog
        rec = AuditLog.objects.filter(action='SYSTEM_GDRIVE_CONFIG').order_by('-timestamp').first()
        if rec and rec.details:
            return json.loads(rec.details)
    except Exception:
        pass
    return None


def set_runtime_credentials(info: dict) -> bool:
    global _CREDS
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=['https://www.googleapis.com/auth/drive']
    )
    creds.refresh(GoogleAuthRequest())
    _CREDS = creds
    try:
        from .models import AuditLog
        AuditLog.objects.filter(action='SYSTEM_GDRIVE_CONFIG').delete()
        AuditLog.objects.create(
            staff_name='SYSTEM',
            staff_role='System',
            action='SYSTEM_GDRIVE_CONFIG',
            details=json.dumps(info),
        )
    except Exception as exc:
        logger.warning(f"Could not persist Drive config to DB: {exc}")
    return True


def _get_credentials():
    global _CREDS
    if _CREDS is not None:
        if not _CREDS.valid:
            _CREDS.refresh(GoogleAuthRequest())
        return _CREDS

    raw = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON', '').strip()
    info = None
    if raw:
        try:
            info = json.loads(raw)
        except Exception:
            try:
                info = json.loads(base64.b64decode(raw).decode('utf-8'))
            except Exception as exc:
                logger.warning(f"Failed to parse GOOGLE_SERVICE_ACCOUNT_JSON: {exc}")

    if not info:
        cred_path = Path(__file__).resolve().parent.parent / 'google_credentials.json'
        if cred_path.exists():
            try:
                info = json.loads(cred_path.read_text(encoding='utf-8'))
            except Exception as exc:
                logger.warning(f"Failed to read google_credentials.json: {exc}")

    if not info:
        info = _load_db_credentials_json()

    if not info:
        return None

    _CREDS = service_account.Credentials.from_service_account_info(
        info, scopes=['https://www.googleapis.com/auth/drive']
    )
    _CREDS.refresh(GoogleAuthRequest())
    return _CREDS


def compress_image_bytes(raw_bytes: bytes, category: str = 'avatar') -> tuple[bytes, str, str]:
    """Resize and WebP-compress image so it fits inside a single 25KB Drive node and loads fast."""
    max_dim = 240 if category == 'avatar' else 900
    img = Image.open(io.BytesIO(raw_bytes))
    if img.mode not in ('RGB', 'L'):
        bg = Image.new('RGB', img.size, (255, 255, 255))
        if 'A' in img.getbands():
            bg.paste(img, mask=img.split()[-1])
        else:
            bg.paste(img)
        img = bg
    elif img.mode == 'L':
        img = img.convert('RGB')

    img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)

    for quality in (76, 66, 56, 46, 36):
        buf = io.BytesIO()
        img.save(buf, format='WEBP', quality=quality, method=4)
        out = buf.getvalue()
        b64 = base64.b64encode(out).decode('ascii')
        data_uri = f"data:image/webp;base64,{b64}"
        if len(data_uri) <= 24500:
            return out, 'image/webp', data_uri

    img.thumbnail((max(120, max_dim // 2), max(120, max_dim // 2)), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format='WEBP', quality=40, method=4)
    out = buf.getvalue()
    b64 = base64.b64encode(out).decode('ascii')
    return out, 'image/webp', f"data:image/webp;base64,{b64}"


def _extract_raw_bytes(value: str | bytes) -> bytes | None:
    if isinstance(value, bytes):
        return value
    if not isinstance(value, str):
        return None
    s = value.strip()
    if s.startswith('data:image/') and ';base64,' in s:
        return base64.b64decode(s.split(';base64,', 1)[1])
    return None


def offload_image(value: str | bytes | None, category: str = 'avatar', label: str = 'media') -> str | None:
    """
    Offloads a data:image/...;base64 string or raw image bytes to Google Drive (CURA_CLINIC_STORAGE)
    and returns a compact URL (/api/media/<drive_id>/). Leaves existing URLs and mascot strings untouched.
    """
    if not value:
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s or s.startswith(('http://', 'https://', '/api/media/', 'mascot:')):
            return s

    raw_bytes = _extract_raw_bytes(value)
    if not raw_bytes:
        return value if isinstance(value, str) else None

    try:
        comp_bytes, mime_type, data_uri = compress_image_bytes(raw_bytes, category=category)
    except Exception as exc:
        logger.warning(f"Image compression failed ({exc}); keeping original value")
        return value if isinstance(value, str) else None

    try:
        creds = _get_credentials()
        if not creds or not creds.token:
            return data_uri

        parent_id = SUBFOLDER_IDS.get(category, ROOT_FOLDER_ID)
        safe_label = ''.join(c if c.isalnum() or c in ('-', '_') else '_' for c in str(label))[:40]
        filename = f"{category}_{safe_label}_{int(time.time())}.webp"

        resp = http_requests.post(
            'https://www.googleapis.com/drive/v3/files?fields=id',
            headers={
                'Authorization': f'Bearer {creds.token}',
                'Content-Type': 'application/json',
            },
            json={
                'name': filename,
                'mimeType': 'application/vnd.google-apps.folder',
                'parents': [parent_id],
                'description': data_uri[:24900],
            },
            timeout=8,
        )
        if resp.status_code == 200:
            drive_id = resp.json().get('id')
            if drive_id:
                if len(_MEDIA_CACHE) >= 512:
                    _MEDIA_CACHE.pop(next(iter(_MEDIA_CACHE)))
                _MEDIA_CACHE[drive_id] = (comp_bytes, mime_type)
                return f"{BACKEND_PUBLIC_URL}/api/media/{drive_id}/"
        else:
            logger.warning(f"Google Drive upload returned {resp.status_code}: {resp.text[:200]}")
    except Exception as exc:
        logger.warning(f"Google Drive offload fallback to compressed data URI: {exc}")

    return data_uri


def fetch_media_from_drive(drive_id: str) -> tuple[bytes, str] | None:
    if drive_id in _MEDIA_CACHE:
        return _MEDIA_CACHE[drive_id]

    try:
        creds = _get_credentials()
        if not creds or not creds.token:
            return None

        resp = http_requests.get(
            f"https://www.googleapis.com/drive/v3/files/{drive_id}?fields=description,mimeType",
            headers={'Authorization': f'Bearer {creds.token}'},
            timeout=8,
        )
        if resp.status_code != 200:
            return None

        desc = (resp.json() or {}).get('description') or ''
        if desc.startswith('data:image/') and ';base64,' in desc:
            header, b64 = desc.split(';base64,', 1)
            mime_type = header.replace('data:', '') or 'image/webp'
            raw = base64.b64decode(b64)
            if len(_MEDIA_CACHE) >= 512:
                _MEDIA_CACHE.pop(next(iter(_MEDIA_CACHE)))
            _MEDIA_CACHE[drive_id] = (raw, mime_type)
            return raw, mime_type
    except Exception as exc:
        logger.warning(f"Failed to fetch Drive media {drive_id}: {exc}")

    return None
