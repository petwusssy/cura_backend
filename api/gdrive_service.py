import base64
import calendar
import csv
import hashlib
import io
import json
import logging
import os
import threading
import time
import zlib
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
import requests as http_requests
from PIL import Image
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as UserCredentials
from google.auth.transport.requests import Request as GoogleAuthRequest

logger = logging.getLogger(__name__)

ROOT_FOLDER_ID = os.environ.get('GOOGLE_DRIVE_FOLDER_ID', '1lc-gyNPuUUjQUL1s07-fMcBumyWnSEIW')
SUBFOLDER_IDS = {
    'patients': '1Cc7f2FvQNNv4XqMBLgUtXvhdiAfvSVJT',
    'avatar': '1rDLDeJ1NHbVcUFEEFnNMtpvts0jhP70Y',
    'live_logs': '1S52a2jI60YjFOzyal5RPrpOMOlLpltmT',
    'prescription': '1Pw0ABWq5rGfQdkXEoEKOesfSgwudGRc_',
    'fetcher_id': '1M4NMSmFdjCTCct_cdZ0jpSIt_-lEWSiD',
    'live_reports': '1QQO74sZoq5VN05osoh4_ETrkZlSZz6uO',
    'archives': '18OVSVoi9i3LI4v2VOBdtNEPP2ou5wzRy',
    'audit_logs': os.environ.get('GOOGLE_DRIVE_AUDIT_FOLDER_ID', '1qRJu1lh8pOmwJ8W_ugpc_OEt8ML7cnie'),
}
BACKEND_PUBLIC_URL = os.environ.get('BACKEND_PUBLIC_URL', 'https://cura-backend-dvj5.onrender.com').rstrip('/')
_FOLDER_MIME = 'application/vnd.google-apps.folder'

# ponytail: in-process dict cache (512 items), swap for Redis/CDN only if multi-worker memory matters
_MEDIA_CACHE: dict[str, tuple[bytes, str]] = {}
_CACHE_LOCK = threading.Lock()
_CREDS = None
_LOCK = threading.Lock()
_AUDIT_LOCK = threading.Lock()
_CONSULT_LOCK = threading.Lock()
_MIRROR_SCHED_LOCK = threading.Lock()
_MIRROR_TIMERS: dict[str, threading.Timer] = {}
_MIRROR_LOCKS: dict[str, threading.Lock] = {}
_SEEDED_MIRRORS = False


_MANILA_TZ = dt_timezone(timedelta(hours=8))


def _manila_now() -> datetime:
    return datetime.now(_MANILA_TZ)


def _fmt_manila(val, fmt: str = '%Y-%m-%d %I:%M:%S %p') -> str:
    if not val:
        return ''
    if isinstance(val, datetime):
        dt = val.replace(tzinfo=dt_timezone.utc) if val.tzinfo is None else val
        return dt.astimezone(_MANILA_TZ).strftime(fmt)
    return str(val)[:19]


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
            staff_name_snapshot='SYSTEM',
            staff_role_snapshot='System',
            action='SYSTEM_GDRIVE_CONFIG',
            details=json.dumps(info),
        )
    except Exception as exc:
        logger.warning(f"Could not persist Drive config to DB: {exc}")
    return True


def _using_user_oauth(creds) -> bool:
    return isinstance(creds, UserCredentials)


def _get_credentials():
    global _CREDS
    with _LOCK:
        if _CREDS is not None:
            if not _CREDS.valid:
                try:
                    _CREDS.refresh(GoogleAuthRequest())
                except Exception as exc:
                    logger.warning(f"Failed refreshing cached Drive credentials: {exc}")
                    _CREDS = None
            if _CREDS is not None:
                return _CREDS

        oauth_client_id = os.environ.get('GOOGLE_OAUTH_CLIENT_ID', '').strip()
        oauth_client_secret = os.environ.get('GOOGLE_OAUTH_CLIENT_SECRET', '').strip()
        oauth_refresh_token = os.environ.get('GOOGLE_OAUTH_REFRESH_TOKEN', '').strip()
        if any((oauth_client_id, oauth_client_secret, oauth_refresh_token)):
            if not all((oauth_client_id, oauth_client_secret, oauth_refresh_token)):
                logger.warning("Incomplete GOOGLE_OAUTH_* env vars; falling back to service account")
            else:
                try:
                    creds = UserCredentials(
                        token=None,
                        refresh_token=oauth_refresh_token,
                        token_uri='https://oauth2.googleapis.com/token',
                        client_id=oauth_client_id,
                        client_secret=oauth_client_secret,
                        scopes=['https://www.googleapis.com/auth/drive'],
                    )
                    creds.refresh(GoogleAuthRequest())
                    _CREDS = creds
                    return _CREDS
                except Exception as exc:
                    logger.warning(f"OAuth2 Drive auth failed, falling back to service account: {exc}")

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

        try:
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=['https://www.googleapis.com/auth/drive']
            )
            creds.refresh(GoogleAuthRequest())
            _CREDS = creds
            return _CREDS
        except Exception as exc:
            logger.warning(f"Service account Drive auth failed: {exc}")
            return None


def _base_mime(mime_type: str) -> str:
    return (mime_type or 'application/octet-stream').split(';', 1)[0].strip() or 'application/octet-stream'


def _drive_url(drive_id: str, mime_type: str = '') -> str:
    if mime_type and mime_type != _FOLDER_MIME:
        return f"https://drive.google.com/file/d/{drive_id}/view"
    return f"https://drive.google.com/drive/folders/{drive_id}"


def _upload_drive_file(
    creds,
    name: str,
    raw_bytes: bytes,
    mime_type: str,
    parent_id: str,
    description: str = '',
    fields: str = 'id',
) -> dict | None:
    """Uploads a real binary/text file using the OAuth user's My Drive quota."""
    clean_mime = _base_mime(mime_type)
    meta = {'name': name, 'parents': [parent_id], 'mimeType': clean_mime}
    if description:
        meta['description'] = description[:2000]
    boundary = f"cura_{uuid.uuid4().hex}"
    body = (
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(meta)}\r\n"
        f"--{boundary}\r\nContent-Type: {clean_mime}\r\n\r\n"
    ).encode('utf-8') + raw_bytes + f"\r\n--{boundary}--\r\n".encode('utf-8')
    resp = http_requests.post(
        f"https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields={fields}",
        headers={
            'Authorization': f'Bearer {creds.token}',
            'Content-Type': f'multipart/related; boundary={boundary}',
        },
        data=body,
        timeout=20,
    )
    if resp.status_code == 200:
        return resp.json()
    logger.warning(f"Google Drive multipart upload failed ({resp.status_code}): {resp.text[:200]}")
    return None


def _update_drive_file(creds, file_id: str, raw_bytes: bytes, mime_type: str) -> bool:
    clean_mime = _base_mime(mime_type)
    resp = http_requests.patch(
        f"https://www.googleapis.com/upload/drive/v3/files/{file_id}?uploadType=media&fields=id",
        headers={'Authorization': f'Bearer {creds.token}', 'Content-Type': clean_mime},
        data=raw_bytes,
        timeout=20,
    )
    if resp.status_code != 200:
        logger.warning(f"Google Drive media update failed ({resp.status_code}): {resp.text[:200]}")
        return False
    return True


def _read_drive_file_bytes(creds, file_id: str) -> bytes | None:
    resp = http_requests.get(
        f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media",
        headers={'Authorization': f'Bearer {creds.token}'},
        timeout=20,
    )
    return resp.content if resp.status_code == 200 else None


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
    if s.startswith('data:') and ';base64,' in s:
        return base64.b64decode(s.split(';base64,', 1)[1])
    try:
        return base64.b64decode(s)
    except Exception:
        return None


def _cache_media(drive_id: str, raw_bytes: bytes, mime_type: str) -> None:
    with _CACHE_LOCK:
        if len(_MEDIA_CACHE) >= 512:
            _MEDIA_CACHE.pop(next(iter(_MEDIA_CACHE)), None)
        _MEDIA_CACHE[drive_id] = (raw_bytes, mime_type)


def _evict_media(drive_id: str, _default=None) -> None:
    with _CACHE_LOCK:
        _MEDIA_CACHE.pop(drive_id, None)


def _append_daily_csv_oauth(
    creds,
    folder_id: str,
    filename: str,
    csv_headers: list[str],
    row_values: list,
    lock: threading.Lock,
) -> None:
    """Appends one row to a real CSV file in Drive (creating/seeding from legacy folder if needed)."""
    with lock:
        q = f"'{folder_id}' in parents and name = '{filename}' and trashed = false"
        search_resp = http_requests.get(
            'https://www.googleapis.com/drive/v3/files',
            params={'q': q, 'fields': 'files(id,name,mimeType,description)', 'pageSize': 10},
            headers={'Authorization': f'Bearer {creds.token}'},
            timeout=8,
        )
        items = (search_resp.json() or {}).get('files', []) if search_resp.status_code == 200 else []
        real_file = next((f for f in items if f.get('mimeType') != _FOLDER_MIME), None)
        legacy_folder = next((f for f in items if f.get('mimeType') == _FOLDER_MIME), None)

        existing_csv = ''
        if real_file:
            raw_bytes = _read_drive_file_bytes(creds, real_file['id'])
            if raw_bytes is not None:
                existing_csv = raw_bytes.decode('utf-8', errors='replace')
        elif legacy_folder:
            existing_csv = legacy_folder.get('description') or ''

        out_buf = io.StringIO()
        writer = csv.writer(out_buf, lineterminator='\n')
        if not existing_csv.strip():
            writer.writerow(csv_headers)
        writer.writerow(row_values)
        new_chunk = out_buf.getvalue()
        updated_csv = (existing_csv.rstrip('\r\n') + '\n' + new_chunk) if existing_csv.strip() else new_chunk
        csv_bytes = updated_csv.encode('utf-8')

        if real_file:
            if _update_drive_file(creds, real_file['id'], csv_bytes, 'text/csv'):
                _evict_media(real_file['id'], None)
        else:
            _upload_drive_file(creds, filename, csv_bytes, 'text/csv', folder_id)


_IMAGE_TAG_LABELS = {
    'avatar': 'Photo',
    'prescription': 'Prescription',
    'fetcher_id': 'FetcherID',
}


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
        manila_stamp = datetime.now(dt_timezone(timedelta(hours=8))).strftime('%Y-%m-%d_%H%M%S')
        safe_label = '_'.join(''.join(c if c.isalnum() or c in ('-', '_') else ' ' for c in str(label)).split())[:45] or 'Record'
        type_tag = _IMAGE_TAG_LABELS.get(category, category)
        filename = f"{manila_stamp}_{safe_label}_{type_tag}.webp"

        drive_id = None
        if _using_user_oauth(creds):
            node = _upload_drive_file(
                creds, filename, comp_bytes, mime_type, parent_id,
                description=f"CURA {category}: {safe_label}",
            )
            drive_id = (node or {}).get('id')
        else:
            resp = http_requests.post(
                'https://www.googleapis.com/drive/v3/files?fields=id',
                headers={
                    'Authorization': f'Bearer {creds.token}',
                    'Content-Type': 'application/json',
                },
                json={
                    'name': filename,
                    'mimeType': _FOLDER_MIME,
                    'parents': [parent_id],
                    'description': data_uri[:24900],
                },
                timeout=8,
            )
            if resp.status_code == 200:
                drive_id = resp.json().get('id')
            else:
                logger.warning(f"Google Drive upload returned {resp.status_code}: {resp.text[:200]}")

        if drive_id:
            _cache_media(drive_id, comp_bytes, mime_type)
            return f"{BACKEND_PUBLIC_URL}/api/media/{drive_id}/"
    except Exception as exc:
        logger.warning(f"Google Drive offload fallback to compressed data URI: {exc}")

    return data_uri


def fetch_media_from_drive(drive_id: str) -> tuple[bytes, str] | None:
    with _CACHE_LOCK:
        cached = _MEDIA_CACHE.get(drive_id)
    if cached is not None:
        return cached

    try:
        creds = _get_credentials()
        if not creds or not creds.token:
            return None

        resp = http_requests.get(
            f"https://www.googleapis.com/drive/v3/files/{drive_id}?fields=name,description,mimeType",
            headers={'Authorization': f'Bearer {creds.token}'},
            timeout=8,
        )
        if resp.status_code != 200:
            return None

        data = resp.json() or {}
        desc = data.get('description') or ''
        name = data.get('name') or 'archive.bin'
        drive_mime = data.get('mimeType') or ''

        if drive_mime and drive_mime != _FOLDER_MIME:
            raw = _read_drive_file_bytes(creds, drive_id)
            if raw is not None:
                out_mime = f"{drive_mime}; charset=utf-8" if drive_mime.startswith('text/') else drive_mime
                _cache_media(drive_id, raw, out_mime)
                return raw, out_mime

        if desc.startswith('data:') and ';base64,' in desc:
            header, b64 = desc.split(';base64,', 1)
            mime_type = header.replace('data:', '') or 'application/octet-stream'
            raw = base64.b64decode(b64)
            _cache_media(drive_id, raw, mime_type)
            return raw, mime_type

        if desc.startswith(('CURA_ZLIB:', 'CURA_ZLIB_MULTI:')):
            # Compressed binary archive (possibly chunked in child nodes if multi-part)
            parts = desc.split(':', 3)
            mime_type = parts[1] if len(parts) > 2 else 'application/octet-stream'
            b64_payload = parts[-1]
            if desc.startswith('CURA_ZLIB_MULTI:'):
                q = f"'{drive_id}' in parents and trashed = false"
                c_resp = http_requests.get(
                    'https://www.googleapis.com/drive/v3/files',
                    params={'q': q, 'fields': 'files(name,description)', 'orderBy': 'name', 'pageSize': 100},
                    headers={'Authorization': f'Bearer {creds.token}'},
                    timeout=10,
                )
                if c_resp.status_code == 200:
                    chunks = sorted(c_resp.json().get('files', []), key=lambda x: x.get('name', ''))
                    b64_payload = ''.join((c.get('description') or '') for c in chunks)
            raw = zlib.decompress(base64.b64decode(b64_payload))
            _cache_media(drive_id, raw, mime_type)
            return raw, mime_type

        if desc:
            mime_type = 'text/csv; charset=utf-8' if name.endswith('.csv') else 'text/plain; charset=utf-8'
            raw = desc.encode('utf-8')
            return raw, mime_type
    except Exception as exc:
        logger.warning(f"Failed to fetch Drive media {drive_id}: {exc}")

    return None


def store_archive_file(
    raw_bytes: bytes,
    filename: str,
    mime_type: str = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    folder_key: str = 'archives',
    staff_name: str = 'SYSTEM',
    report_title: str = 'Clinic Archive',
) -> dict | None:
    """
    Stores an Excel/CSV/JSON report inside Google Drive (Medical_Archives, Live_Daily_Logs, or Audit_Logs)
    using real Drive files under OAuth2 user quota, or zero-quota folder nodes under Service Account fallback.
    """
    creds = _get_credentials()
    if not creds or not creds.token:
        return None

    sha256_hash = hashlib.sha256(raw_bytes).hexdigest()[:16]
    parent_id = SUBFOLDER_IDS.get(folder_key, SUBFOLDER_IDS['archives'])

    if _using_user_oauth(creds):
        node = _upload_drive_file(
            creds,
            filename,
            raw_bytes,
            mime_type,
            parent_id,
            description=f"CURA Archive ({report_title}) by {staff_name} | sha256:{sha256_hash}",
            fields='id,name,mimeType,createdTime',
        )
        if not node:
            return None
        node_mime = node.get('mimeType') or _base_mime(mime_type)
    else:
        compressed_b64 = base64.b64encode(zlib.compress(raw_bytes, level=9)).decode('ascii')
        headers = {
            'Authorization': f'Bearer {creds.token}',
            'Content-Type': 'application/json',
        }
        if len(compressed_b64) <= 24000:
            desc = f"CURA_ZLIB:{mime_type}:{sha256_hash}:{compressed_b64}"
            resp = http_requests.post(
                'https://www.googleapis.com/drive/v3/files?fields=id,name,createdTime',
                headers=headers,
                json={
                    'name': filename,
                    'mimeType': _FOLDER_MIME,
                    'parents': [parent_id],
                    'description': desc,
                },
                timeout=10,
            )
            if resp.status_code != 200:
                logger.warning(f"Failed to create Drive archive node: {resp.text[:200]}")
                return None
            node = resp.json()
        else:
            desc = f"CURA_ZLIB_MULTI:{mime_type}:{sha256_hash}:"
            resp = http_requests.post(
                'https://www.googleapis.com/drive/v3/files?fields=id,name,createdTime',
                headers=headers,
                json={
                    'name': filename,
                    'mimeType': _FOLDER_MIME,
                    'parents': [parent_id],
                    'description': desc,
                },
                timeout=10,
            )
            if resp.status_code != 200:
                return None
            node = resp.json()
            parent_node_id = node['id']
            chunk_size = 24000
            for idx in range(0, len(compressed_b64), chunk_size):
                chunk_str = compressed_b64[idx:idx + chunk_size]
                http_requests.post(
                    'https://www.googleapis.com/drive/v3/files?fields=id',
                    headers=headers,
                    json={
                        'name': f"chunk_{idx // chunk_size:03d}",
                        'mimeType': _FOLDER_MIME,
                        'parents': [parent_node_id],
                        'description': chunk_str,
                    },
                    timeout=10,
                )
        node_mime = _FOLDER_MIME

    drive_id = node['id']
    _cache_media(drive_id, raw_bytes, mime_type)

    info = {
        'id': drive_id,
        'name': filename,
        'title': report_title,
        'folder': folder_key,
        'sha256': sha256_hash,
        'size_bytes': len(raw_bytes),
        'staff_name': staff_name,
        'created_at': node.get('createdTime') or _manila_now().isoformat(),
        'download_url': f"{BACKEND_PUBLIC_URL}/api/reports/archives/{drive_id}/download/",
        'drive_url': _drive_url(drive_id, node_mime),
    }

    try:
        from django.conf import settings as dj_settings
        if dj_settings.configured:
            from .models import AuditLog
            is_audit_backup = folder_key == 'audit_logs'
            AuditLog.objects.create(
                staff_name_snapshot=staff_name or 'SYSTEM',
                staff_role_snapshot='Archive Vault',
                action='BACKUP_AUDIT_LOGS' if is_audit_backup else 'ARCHIVE_REPORT',
                target_type='AuditLog' if is_audit_backup else 'Report',
                target_id=drive_id,
                details=json.dumps(info),
            )
    except Exception as exc:
        logger.warning(f"Failed to log archive to AuditLog: {exc}")

    return info


def _sync_consultation_worker(payload: dict):
    """Background worker that appends/updates the daily CSV ledger in Google Drive (Live_Daily_Logs)."""
    try:
        creds = _get_credentials()
        if not creds or not creds.token:
            return

        date_str = str(payload.get('date') or _manila_now().strftime('%Y-%m-%d'))[:10]
        ledger_name = f"Consultations_{date_str}.csv"
        live_folder_id = SUBFOLDER_IDS['live_logs']

        csv_headers = [
            'Consultation_ID', 'Date', 'Time_In', 'Time_Out', 'Patient_ID', 'Patient_Name',
            'Category', 'Course_Dept', 'Status', 'Complaint', 'Categories', 'BP', 'Temp',
            'Pulse', 'RR', 'O2_Sat', 'Doctor', 'Assisting_Nurse', 'Treatments',
            'Early_Dismissal', 'Destination', 'Nurse_Notes'
        ]
        row_values = [
            payload.get('id', ''),
            date_str,
            payload.get('timeIn', ''),
            payload.get('timeOut', ''),
            payload.get('patient_id', ''),
            payload.get('patient_name', ''),
            payload.get('patient_category', ''),
            payload.get('patient_course', ''),
            payload.get('status', ''),
            payload.get('complaint', ''),
            ', '.join(payload.get('categories') or []),
            payload.get('bp', ''),
            payload.get('temp', ''),
            payload.get('pulse', ''),
            payload.get('rr', ''),
            payload.get('o2', ''),
            payload.get('doctorName', ''),
            payload.get('assistingNurse', ''),
            payload.get('treatments_summary', ''),
            'Yes' if payload.get('earlyDismissal') else 'No',
            payload.get('dismissalDestination', ''),
            payload.get('nurseNotes', ''),
        ]

        if payload.get('_from_db'):
            try:
                from .models import Consultation
                qs = Consultation.objects.select_related('patient').prefetch_related('treatments').filter(
                    date=date_str
                ).order_by('timeIn')
                db_rows, seen_ids, seen_sem = [], set(), set()
                for c in qs:
                    comp = (c.complaint or '').strip()
                    if '[CONVERTED]' in comp:
                        continue
                    cid = str(c.id)
                    sem = (str(c.patient_id), str(c.date)[:10], str(c.timeIn or ''), comp, str(c.status or ''))
                    if cid in seen_ids or sem in seen_sem:
                        continue
                    seen_ids.add(cid)
                    seen_sem.add(sem)
                    p = getattr(c, 'patient', None)
                    v = getattr(c, 'vitals', None) or {}
                    t_str = '; '.join(f"{t.medicineName} ({t.quantity} {t.unit})" for t in c.treatments.all())
                    db_rows.append([
                        cid, date_str, str(c.timeIn or ''), str(c.timeOut or ''),
                        str(p.id if p else c.patient_id), str(p.name if p else ''),
                        str(p.category if p else ''), str((p.course or p.department or p.gradeLevel or '') if p else ''),
                        str(c.status or 'Consultation'), str(comp or c.purposeOfVisit or ''),
                        ', '.join(c.categories or []),
                        str(v.get('bloodPressure') or v.get('bp') or ''),
                        str(v.get('temperature') or v.get('temp') or ''),
                        str(v.get('pulseRate') or v.get('pulse') or ''),
                        str(v.get('respiratoryRate') or v.get('rr') or ''),
                        str(v.get('oxygenSaturation') or v.get('o2') or ''),
                        str(c.doctorName or ''), str(c.assistingNurse or ''), t_str,
                        'Yes' if c.earlyDismissal else 'No', str(c.dismissalDestination or ''),
                        str(c.nurseNotes or c.operationalNotes or ''),
                    ])
                _upsert_drive_csv(live_folder_id, ledger_name, csv_headers, db_rows, _CONSULT_LOCK)
                if len(date_str) >= 7 and date_str[4] == '-':
                    yr, mo = int(date_str[:4]), int(date_str[5:7])
                    now = _manila_now()
                    if (yr, mo) != (now.year, now.month):
                        sync_month_report_mirrors(yr, mo)
                return
            except Exception as db_exc:
                logger.warning(f"DB daily consultation rebuild fallback: {db_exc}")
            finally:
                try:
                    from django.db import close_old_connections
                    close_old_connections()
                except Exception:
                    pass

        if _using_user_oauth(creds):
            _append_daily_csv_oauth(creds, live_folder_id, ledger_name, csv_headers, row_values, _CONSULT_LOCK)
            return

        headers = {
            'Authorization': f'Bearer {creds.token}',
            'Content-Type': 'application/json',
        }
        q = f"'{live_folder_id}' in parents and name = '{ledger_name}' and trashed = false"
        search_resp = http_requests.get(
            'https://www.googleapis.com/drive/v3/files',
            params={'q': q, 'fields': 'files(id,name,description)', 'pageSize': 1},
            headers={'Authorization': f'Bearer {creds.token}'},
            timeout=8,
        )
        files = (search_resp.json() or {}).get('files', []) if search_resp.status_code == 200 else []

        out_buf = io.StringIO()
        writer = csv.writer(out_buf)

        if not files:
            writer.writerow(csv_headers)
            writer.writerow(row_values)
            csv_text = out_buf.getvalue()
            create_resp = http_requests.post(
                'https://www.googleapis.com/drive/v3/files?fields=id',
                headers=headers,
                json={
                    'name': ledger_name,
                    'mimeType': _FOLDER_MIME,
                    'parents': [live_folder_id],
                    'description': csv_text[:24800],
                },
                timeout=8,
            )
            if create_resp.status_code != 200:
                return
            ledger_id = create_resp.json().get('id')
        else:
            ledger_node = files[0]
            ledger_id = ledger_node['id']
            existing_csv = ledger_node.get('description') or ''
            if not existing_csv.strip():
                writer.writerow(csv_headers)
            writer.writerow(row_values)
            new_line = out_buf.getvalue()
            updated_csv = (existing_csv.rstrip('\r\n') + '\n' + new_line.lstrip('\r\n')) if existing_csv.strip() else new_line
            if len(updated_csv) > 24800:
                lines = updated_csv.splitlines()
                header_line = lines[0] if lines else ''
                tail = '\n'.join(lines[1:])[-23500:]
                updated_csv = header_line + '\n' + tail
            http_requests.patch(
                f"https://www.googleapis.com/drive/v3/files/{ledger_id}?fields=id",
                headers=headers,
                json={'description': updated_csv},
                timeout=8,
            )
            _evict_media(ledger_id, None)

        # 2. Also create an individual human-readable entry inside today's ledger folder (Service Account mode only)
        if ledger_id:
            time_clean = str(payload.get('timeIn') or '0000').replace(':', '')[:4]
            pname_clean = ''.join(c if c.isalnum() or c in (' ', '-', '_') else '' for c in str(payload.get('patient_name') or 'PATIENT')).strip()[:35]
            entry_name = f"{time_clean}_{pname_clean}_{payload.get('status', 'Visit')}.json"
            http_requests.post(
                'https://www.googleapis.com/drive/v3/files?fields=id',
                headers=headers,
                json={
                    'name': entry_name,
                    'mimeType': _FOLDER_MIME,
                    'parents': [ledger_id],
                    'description': json.dumps(payload, indent=2)[:24800],
                },
                timeout=8,
            )
    except Exception as exc:
        logger.warning(f"Live Drive consultation sync warning: {exc}")


def trigger_live_consultation_sync(consultation) -> None:
    """Extracts consultation fields on the main thread and dispatches non-blocking Drive sync."""
    try:
        patient = getattr(consultation, 'patient', None)
        vitals = getattr(consultation, 'vitals', None) or {}
        treatments_qs = getattr(consultation, 'treatments', None)
        treatments_list = []
        if treatments_qs is not None:
            try:
                for t in treatments_qs.all():
                    treatments_list.append(f"{t.medicineName} ({t.quantity} {t.unit})")
            except Exception:
                pass

        payload = {
            'id': str(consultation.id),
            'date': str(consultation.date),
            'timeIn': str(consultation.timeIn or ''),
            'timeOut': str(consultation.timeOut or ''),
            'patient_id': str(patient.id if patient else consultation.patient_id),
            'patient_name': str(patient.name if patient else ''),
            'patient_category': str(patient.category if patient else ''),
            'patient_course': str((patient.course or patient.department or patient.gradeLevel or '') if patient else ''),
            'status': str(consultation.status or 'Consultation'),
            'complaint': str(consultation.complaint or consultation.purposeOfVisit or ''),
            'categories': list(consultation.categories or []),
            'bp': str(vitals.get('bloodPressure') or vitals.get('bp') or ''),
            'temp': str(vitals.get('temperature') or vitals.get('temp') or ''),
            'pulse': str(vitals.get('pulseRate') or vitals.get('pulse') or ''),
            'rr': str(vitals.get('respiratoryRate') or vitals.get('rr') or ''),
            'o2': str(vitals.get('oxygenSaturation') or vitals.get('o2') or ''),
            'doctorName': str(consultation.doctorName or ''),
            'assistingNurse': str(consultation.assistingNurse or ''),
            'treatments_summary': '; '.join(treatments_list),
            'earlyDismissal': bool(consultation.earlyDismissal),
            'dismissalDestination': str(consultation.dismissalDestination or ''),
            'nurseNotes': str(consultation.nurseNotes or consultation.operationalNotes or ''),
            'synced_at': _manila_now().isoformat(),
            '_from_db': True,
        }
        threading.Thread(target=_sync_consultation_worker, args=(payload,), daemon=True).start()
    except Exception as exc:
        logger.warning(f"Could not dispatch live consultation sync: {exc}")


def _audit_part_num(name: str, base_name: str) -> int:
    if name == f"{base_name}.csv":
        return 1
    prefix = f"{base_name}_part"
    if name.startswith(prefix) and name.endswith('.csv'):
        num_str = name[len(prefix):-4]
        if num_str.isdigit():
            return int(num_str)
    return 0


def _sync_audit_log_worker(payload: dict):
    """Background worker that appends every AuditLog entry to Audit_Logs/Audit_Trail_YYYY-MM-DD.csv in Google Drive."""
    try:
        creds = _get_credentials()
        if not creds or not creds.token:
            return

        date_str = str(payload.get('date') or _manila_now().strftime('%Y-%m-%d'))[:10]
        base_name = f"Audit_Trail_{date_str}"
        audit_folder_id = SUBFOLDER_IDS['audit_logs']
        csv_headers = ['Timestamp', 'Staff_Name', 'Staff_Role', 'Action', 'Target_Type', 'Target_ID', 'Details']
        row_values = [
            payload.get('timestamp', ''),
            payload.get('staff_name', ''),
            payload.get('staff_role', ''),
            payload.get('action', ''),
            payload.get('target_type', ''),
            payload.get('target_id', ''),
            payload.get('details', ''),
        ]

        if _using_user_oauth(creds):
            _append_daily_csv_oauth(creds, audit_folder_id, f"{base_name}.csv", csv_headers, row_values, _AUDIT_LOCK)
            return

        headers = {
            'Authorization': f'Bearer {creds.token}',
            'Content-Type': 'application/json',
        }
        out_buf = io.StringIO()
        writer = csv.writer(out_buf)

        # ponytail: Drive folder description max 24.8KB per part; rolls over into _partNN.csv without truncating history
        with _AUDIT_LOCK:
            q = f"'{audit_folder_id}' in parents and name contains '{base_name}' and trashed = false"
            search_resp = http_requests.get(
                'https://www.googleapis.com/drive/v3/files',
                params={'q': q, 'fields': 'files(id,name,description)', 'pageSize': 50},
                headers={'Authorization': f'Bearer {creds.token}'},
                timeout=8,
            )
            raw_files = (search_resp.json() or {}).get('files', []) if search_resp.status_code == 200 else []
            part_files = [
                (p, f) for f in raw_files
                if (p := _audit_part_num(f.get('name', ''), base_name)) > 0
            ]
            part_files.sort(key=lambda item: item[0])

            if not part_files:
                writer.writerow(csv_headers)
                writer.writerow(row_values)
                http_requests.post(
                    'https://www.googleapis.com/drive/v3/files?fields=id',
                    headers=headers,
                    json={
                        'name': f"{base_name}.csv",
                        'mimeType': _FOLDER_MIME,
                        'parents': [audit_folder_id],
                        'description': out_buf.getvalue()[:24800],
                    },
                    timeout=8,
                )
                return

            latest_part, ledger_node = part_files[-1]
            ledger_id = ledger_node['id']
            existing_csv = ledger_node.get('description') or ''
            if not existing_csv.strip():
                writer.writerow(csv_headers)
            writer.writerow(row_values)
            new_line = out_buf.getvalue()
            updated_csv = (existing_csv.rstrip('\r\n') + '\n' + new_line.lstrip('\r\n')) if existing_csv.strip() else new_line

            if len(updated_csv) <= 24800:
                http_requests.patch(
                    f"https://www.googleapis.com/drive/v3/files/{ledger_id}?fields=id",
                    headers=headers,
                    json={'description': updated_csv},
                    timeout=8,
                )
                _evict_media(ledger_id, None)
            else:
                roll_buf = io.StringIO()
                roll_writer = csv.writer(roll_buf)
                roll_writer.writerow(csv_headers)
                roll_writer.writerow(row_values)
                http_requests.post(
                    'https://www.googleapis.com/drive/v3/files?fields=id',
                    headers=headers,
                    json={
                        'name': f"{base_name}_part{latest_part + 1:02d}.csv",
                        'mimeType': _FOLDER_MIME,
                        'parents': [audit_folder_id],
                        'description': roll_buf.getvalue()[:24800],
                    },
                    timeout=8,
                )
    except Exception as exc:
        logger.warning(f"Live Drive audit log sync warning: {exc}")


def trigger_live_audit_sync(audit_log) -> None:
    """Dispatches non-blocking Google Drive sync for every non-system AuditLog entry."""
    try:
        action = str(getattr(audit_log, 'action', '') or '')
        if action == 'SYSTEM_GDRIVE_CONFIG':
            return
        ts = getattr(audit_log, 'timestamp', None) or _manila_now()
        payload = {
            'date': _fmt_manila(ts, '%Y-%m-%d') if isinstance(ts, datetime) else _manila_now().strftime('%Y-%m-%d'),
            'timestamp': _fmt_manila(ts),
            'staff_name': str(getattr(audit_log, 'staff_name_snapshot', '') or 'SYSTEM'),
            'staff_role': str(getattr(audit_log, 'staff_role_snapshot', '') or 'Staff'),
            'action': action,
            'target_type': str(getattr(audit_log, 'target_type', '') or ''),
            'target_id': str(getattr(audit_log, 'target_id', '') or ''),
            'details': str(getattr(audit_log, 'details', '') or ''),
        }
        threading.Thread(target=_sync_audit_log_worker, args=(payload,), daemon=True).start()
    except Exception as exc:
        logger.warning(f"Could not dispatch live audit sync: {exc}")


def _upsert_drive_csv(
    folder_id: str,
    filename: str,
    csv_headers: list[str],
    rows: list[list],
    lock: threading.Lock,
) -> None:
    """Creates or overwrites a snapshot CSV mirror in Drive without creating duplicate files."""
    creds = _get_credentials()
    if not creds or not creds.token or not folder_id:
        return

    out_buf = io.StringIO()
    writer = csv.writer(out_buf, lineterminator='\n')
    writer.writerow(csv_headers)
    writer.writerows(rows)
    csv_text = out_buf.getvalue()
    csv_bytes = csv_text.encode('utf-8')

    with lock:
        q = f"'{folder_id}' in parents and name = '{filename}' and trashed = false"
        search_resp = http_requests.get(
            'https://www.googleapis.com/drive/v3/files',
            params={'q': q, 'fields': 'files(id,name,mimeType)', 'pageSize': 10},
            headers={'Authorization': f'Bearer {creds.token}'},
            timeout=10,
        )
        items = (search_resp.json() or {}).get('files', []) if search_resp.status_code == 200 else []
        real_file = next((f for f in items if f.get('mimeType') != _FOLDER_MIME), None)
        legacy_folder = next((f for f in items if f.get('mimeType') == _FOLDER_MIME), None)

        if _using_user_oauth(creds):
            if real_file:
                if _update_drive_file(creds, real_file['id'], csv_bytes, 'text/csv'):
                    _evict_media(real_file['id'], None)
            else:
                _upload_drive_file(
                    creds, filename, csv_bytes, 'text/csv', folder_id,
                    description=f"CURA Live Report Mirror: {filename}",
                )
            return

        headers = {'Authorization': f'Bearer {creds.token}', 'Content-Type': 'application/json'}
        if len(csv_text) <= 24800:
            desc_payload = csv_text
        else:
            # ponytail: zlib+b64 fits ~150KB CSV inside Drive's 25KB description limit without chunk nodes
            sha16 = hashlib.sha256(csv_bytes).hexdigest()[:16]
            comp_b64 = base64.b64encode(zlib.compress(csv_bytes, level=9)).decode('ascii')
            desc_payload = f"CURA_ZLIB:text/csv; charset=utf-8:{sha16}:{comp_b64}"[:24800]
        if legacy_folder:
            http_requests.patch(
                f"https://www.googleapis.com/drive/v3/files/{legacy_folder['id']}?fields=id",
                headers=headers,
                json={'description': desc_payload},
                timeout=10,
            )
            _evict_media(legacy_folder['id'], None)
        else:
            http_requests.post(
                'https://www.googleapis.com/drive/v3/files?fields=id',
                headers=headers,
                json={
                    'name': filename,
                    'mimeType': _FOLDER_MIME,
                    'parents': [folder_id],
                    'description': desc_payload,
                },
                timeout=10,
            )


def _mirror_lock(key: str) -> threading.Lock:
    with _MIRROR_SCHED_LOCK:
        return _MIRROR_LOCKS.setdefault(key, threading.Lock())


def _dedup_month_consultations(year: int, month: int):
    from .models import Consultation
    rows = Consultation.objects.select_related('patient').prefetch_related('treatments').filter(
        date__year=year, date__month=month
    ).order_by('date', 'timeIn')
    seen_ids, seen_sem, kept = set(), set(), []
    for c in rows:
        complaint = (c.complaint or '').strip()
        if complaint.startswith('[CONVERTED]'):
            continue
        cid = str(c.id)
        sem = (str(c.patient_id), str(c.date)[:10], str(c.timeIn or ''), complaint, str(c.status or ''))
        if cid in seen_ids or sem in seen_sem:
            continue
        seen_ids.add(cid)
        seen_sem.add(sem)
        kept.append(c)
    return kept


# ── Canonical Web Report Templates & Matching Rules (1:1 with Reports.tsx) ──
import re

_ALL_CASES = [
    'Abdominal Pain/Stomachache', 'Accidents', 'Acute Gingivitis', 'Acute Resp. Tract Infection',
    'Allergy', 'Allergic Rhinitis', 'Anxiety', 'Asthma', 'Blister', 'Body weakness/malaise',
    'Body pain', 'Burns', 'Cat bite/scratch', 'Cellulitis', 'Chicken pox',
    'Chest pain/tightness/Palpitation', 'Colds', 'Contusion/bumps', 'Cough', 'Cyst',
    'Dengue Fever', 'Diarrhea/LBM', 'Dislocation/Fracture', 'Dizziness', 'Dyspepsia',
    'Dog bite/scratch', 'Dysmenorrhea', 'Difficulty of breathing', 'Ear pain',
    'Eye irritation/Sore eyes', 'Fainting', 'Fever', 'Furuncle/carbuncle/boils',
    'Gastritis/Hyperacidity/epigastric pain/heartburn', 'Headache', 'Heat stress',
    'Hypertension', 'Hypotension', 'Hyperventilation', 'Infected toenail',
    'Inflammation/swelling', 'Insect bites', 'Joint pain', 'Lack of sleep', 'Measles',
    'Migraine', 'Mouth sore', 'Mumps', 'Muscle pain', 'Nape pain', 'Nausea',
    'Nose bleeding (epistaxis)', 'Pain (upper and lower body)', 'Pain Right lower quadrant (T/C Appendicitis)',
    'Pruritus/skin irritation/skin condition', 'Rashes', 'Seizure', 'Sore throat',
    'Splinter', 'Sprain', 'Stiff neck', 'Tinnitus', 'Toothache', 'UTI (urinary tract infection)',
    'Vaccine site pain', 'Vertigo', 'Vomiting', 'Wounds (abrasion,laceration,puncture)',
    'Vision blurring', 'Lab works reading', 'Constipation', 'Hair loss', 'Hypoglycemia', 'Indigestion', 'Lethargic', 'Fracture', 'Sinusitis',
]


def _is_case_match(target_case: str, cat_or_complaint: str) -> bool:
    if not cat_or_complaint or not target_case:
        return False
    a = target_case.lower().strip()
    b = cat_or_complaint.lower().strip()
    if a == b:
        return True

    a_parts = [s.strip().lower() for s in re.split(r'[/,;]+', a) if s.strip()]
    b_parts = [s.strip().lower() for s in re.split(r'[/,;]+', b) if s.strip()]
    if a in b_parts or b in a_parts or any(ap in b_parts for ap in a_parts):
        return True

    if ('abdominal pain' in a or 'stomachache' in a) and any(k in b for k in ('abdominal pain', 'stomachache', 'stomach ache', 'abdominal')):
        return True
    if 'diarrhea' in a and ('diarrhea' in b or 'lbm' in b):
        return True
    if 'chest pain' in a and ('chest pain' in b or 'tightness' in b):
        return True
    if 'palpitation' in a and 'palpitation' in b:
        return True
    if 'fainting' in a and ('fainting' in b or 'syncope' in b):
        return True
    if 'stiff neck' in a and 'stiff neck' in b:
        return True
    if 'wounds' in a and any(k in b for k in ('wound', 'laceration', 'abrasion', 'puncture')):
        return True
    if 'eye irritation' in a and any(k in b for k in ('eye complaint', 'sore eyes', 'eye irritation', 'eye pain')):
        return True
    if 'ear pain' in a and any(k in b for k in ('ear complaint', 'ear pain', 'earache')):
        return True
    if 'allergy' in a and 'allergy' in b:
        return True
    if 'rashes' in a and ('rashes' in b or 'rash' in b):
        return True
    if 'headache' in a and 'headache' in b:
        return True
    if 'dizziness' in a and ('dizziness' in b or 'dizzy' in b):
        return True
    if a == 'dengue fever':
        return b == 'dengue fever' or 'dengue' in b
    if a == 'fever':
        return b == 'fever'
    if 'cough' in a and ('cough' in b or 'ubo' in b):
        return True
    if 'colds' in a and ('colds' in b or 'sipon' in b):
        return True
    if 'sore throat' in a and ('sore throat' in b or 'throat pain' in b):
        return True
    if 'toothache' in a and any(k in b for k in ('toothache', 'tooth pain', 'ngipin')):
        return True
    if 'dysmenorrhea' in a and any(k in b for k in ('dysmenorrhea', 'menstrual cramps', 'period pain')):
        return True
    if 'hypertension' in a and any(k in b for k in ('hypertension', 'high blood', 'elevated bp')):
        return True
    if 'asthma' in a and 'asthma' in b:
        return True

    if len(b) > len(a):
        if re.search(rf'\b{re.escape(a)}\b', b, flags=re.IGNORECASE):
            return True
    return False


_CENSUS_SHEET_GROUPS = [
    ('G11', [
        'HUMSS11-ST. ALBERT CHMIEHLOWSKI', '11-ST. CAMILLUS DE LELLIS', '11-ST. DAMIEN DE VEUSTER',
        '11-ST. ELIZABETH OF HUNGARY', '11-ST. FAUSTINA KOWALSKA', 'HUMSS11-ST. FRANCISCO MARTO',
        '11-ST. GIANNA BERETTA MOLLA', 'HUMSS11-ST. JACINTA MARTO', '11-ST. JEROME EMILLANI',
        '11-ST. JOHN BOSCO', '11-ST. JOHN MARIE VIANNEY', '11-ST. JOHN OF GOD',
        '11-ST. JOSEPH BENEDICT COTTO', '11-ST. LEOPOLD MANLIC', 'SHS11- ST. MAXIMILIAN KOLBE',
        '11-ST. PETER CLAVER', '11-ST. TERESA OF CALCUTTA', 'STEM11-ST. VINCENT DE PAUL',
    ]),
    ('G12', [
        'STEM12-OUR LADY OF FATIMA', 'STEM12-OUR LADY OF GOOD COUNSEL', 'GAS12-OUR LADY OF GRACE',
        'SHS12- OUR LADY OF GUADALUPE', 'SHS12- OUR LADY OF IMMA CON', 'SHS12- OUR LADY OF LIFE',
        'GAS12-OUR LADY OF LIGHT', 'STEM12-OUR LADY OF LOURDES', 'HUMSS12-OUR LADY OF LORETO',
        'SHS12- OUR LADY OF M. MEDAL', 'STEM12-OUR LADY OF MERCY', 'SHS12- OUR LADY OF MONTSERRAT',
        'SHS12- OUR LADY OF MT. CARMEL', 'SHS12- OUR LADY OF PEACE', 'SHS12- OUR LADY OF P. HELP',
        'SHS12- OUR LADY OF REMEDIES', 'ABM12-OUR LADY OF SORROWS', 'SHS12- OUR LADY OF THE PILLAR',
        'SHS12- OUR LADY OF THE ROSARY',
    ]),
    ('ABCOMM, AR', [
        'AB COMM 1A', 'AB COMM 1B', 'AB COMM 2A', 'AB COMM 3A', 'AB COMM 4A',
        'AR 1A', 'AR 1B', 'AR 1C', 'AR 2A', 'AR 2B', 'AR 2C',
        'AR 3A', 'AR 3B', 'AR 4A', 'AR 4B', 'AR 5A', 'AR 5B',
    ]),
    ('EDUC', [
        'EDUC 1A', 'EDUC 2A', 'EDUC 3A', 'EDUC 4A', 'GENED2A', 'GENED3A', 'EDUC 4B', 'GENED4A',
        'BPED1A', 'BPED2A', 'BPED 3A', 'BPED 4A', 'BSED4FIL-C', 'FIL4A',
        'MAPEH1A', 'MAPEH2A', 'MAPEH3A', 'MAPEH4A', 'MATH1A', 'MATH2A', 'MATH3A', 'MATH4A',
        'REVE 1A', 'REVE1A', 'EDUC 2B', 'REVE2A', 'EDUC 3B', 'REVE3A', 'REVE4A',
        'SS2A', 'SOCST4A', 'VERE 1A', 'VERE 2A', 'VERE 3A', 'VERE 4A',
        'ENG2A', 'ENG3A', 'BSE 4A', 'ENG4A', 'FIL2A', 'FIL3A',
    ]),
    ('PSE,BHS,BLIS', [
        'BHS 1A', 'BHS 2A', 'BHS 3A', 'BHS 4A',
        'BLIS 1A', 'BLIS 2A', 'BLIS 3A', 'BLIS 4A',
    ]),
    ('HM,TM', [
        'HM 1A', 'HM 1B', 'HM 2A', 'HM 2B', 'HM 3A', 'HM 3B', 'HM 4A', 'HM 4B',
        'TM 1A', 'TM 1B', 'TM 1C', 'TM 2A', 'TM 2B', 'TM 2C', 'TM 3A', 'TM 3B', 'TM 3C', 'TM 4A', 'TM 4B',
    ]),
    ('CRIM, PSYCH', [
        'BS CRIM 1A', 'BS CRIM 2A', 'BS CRIM 3A', 'BS CRIM 4A',
        'PSYCH 1A', 'PSYCH 1B', 'PSYCH 2A', 'PSYCH 2B', 'PSYCH 3A', 'PSYCH 3B', 'PSYCH 4A', 'PSYCH 4B',
    ]),
    ('ACC,AIS', [
        'ACC 1A', 'ACC 1B', 'ACC 1C', 'ACC 1D', 'ACC 1E', 'ACC 2A', 'ACC 2B', 'ACC 2C', 'ACC 2D',
        'ACC 3A', 'ACC 3B', 'ACC 3C', 'ACC 3D', 'ACC 4A', 'ACC 4B', 'ACC 4C', 'ACC 5A', 'ACC 5B', 'ACC 5C',
        'AIS 1A', 'AIS 1B', 'AIS 1C', 'AIS 2A', 'AIS 2B', 'AIS 2C', 'AIS 3A', 'AIS 3B', 'AIS 3C', 'AIS 4A', 'AIS 4B', 'AIS 4C',
    ]),
    ('MM,OM', [
        'MM 1A', 'MM 1B', 'MM 2A', 'MM 2B', 'MM 3A', 'MM 3B', 'MM 4A', 'MM 4B',
        'OM 1A', 'OM 1B', 'OM 2A', 'OM 3A', 'OM 3B', 'OM 4A', 'OM 4B',
    ]),
    ('ENGR', [
        'CE 1A', 'CE 1B', 'CE 1C', 'CE 2A', 'CE 2B', 'CE 2C', 'CE 3A', 'CE 3B', 'CE 4A', 'CE 4B', 'CE 5A',
        'CPE 1A', 'CPE 2A', 'CPE 3A', 'CPE 4A', 'CPE 5A',
        'IE 1A', 'IE 2A', 'IE 3A', 'IE 4A', 'IE 5A',
    ]),
    ('IT, CS', [
        'CS 1A', 'CS 2A', 'CS 3A', 'CS 4A',
        'IT 1A', 'IT 1B', 'IT 1C', 'IT 2A', 'IT 2B', 'IT 2C', 'IT 3A', 'IT 3B', 'IT 4A', 'IT 4B',
    ]),
    ('BSN, BSP', [
        'BSN 1A', 'BSN 1B', 'BSN 1C', 'BSN 1D', 'BSN 1E', 'BSN 1F', 'BSN 1G', 'BSN 1H',
        'BSN 2A', 'BSN 2B', 'BSN 2C', 'BSN 2D', 'BSN 2E', 'BSN 2F',
        'BSN 3A', 'BSN 3B', 'BSN 3C', 'BSN 3D', 'BSN 4A', 'BSN 4B', 'BSN 4C', 'BSN 4D',
        'BSP 1A', 'BSP 2A', 'BSP 2B', 'BSP 3A', 'BSP 4A',
    ]),
]

_MEDICINE_INVENTORY_TEMPLATE = [
    (1, 'Allerta 10mg tab', 'Feb-28'), (2, 'Allerkid 60mL bottle', 'Jul-27'), (3, 'Alnix 10mg tab', 'Jun-28'),
    (4, 'Aspilets-EC 80mg tab', 'Feb-28'), (5, 'Benadryl 25mg', 'Jul-28'), (6, 'Benadryl 50mg', 'Jun-28'),
    (7, 'Benadryl 60mL bottle', 'Jun-29'), (8, 'Bioflu tab', 'Jul-27'), (9, 'Biogesic 500mg tab', 'May-29'),
    (10, 'Budecort respules 250mcg/mL', 'Dec-26'), (11, 'Buscopan 10mg tab', 'Dec-28'), (12, 'Buscopan Plus', 'Jun-28'),
    (13, 'Calmoseptine ointment', 'Nov-30'), (14, 'Catapres 75mcg', 'May-28'), (15, 'Cetirizine (Zyrtec) 10mg tab', 'Jul-27'),
    (16, 'Celecoxib 200mg capsule', 'Aug-27'), (17, 'Dolcet 37.5mg/325mg tab', 'Dec-26'), (18, 'Dolfenal 500mg tab', 'Jul-27'),
    (19, 'Duavent nebules', 'Jul-28'), (20, 'Erceflora Niblet', 'Dec-28'), (21, 'Erythromycin ointment tubes', 'Dec-26'),
    (22, 'Flotera chewable', 'Dec-28'), (23, 'Gaviscon sachet', 'Jan-28'), (24, 'Hidrasec 100mg capsule', 'Dec-28'),
    (25, 'Hydrite sachet', 'Sep-28'), (26, 'Hypromellose 3mg/mL drops', 'Aug-28'), (27, 'Imodium 2mg cap', 'Jul-30'),
    (28, 'Isordil SL 5mg tab', 'NO STOCK'), (29, 'Kramil-S chewable pink', 'Sep-28'), (30, 'Kramil-S ADVANCE', 'Sep-27'),
    (31, 'Motilium 10mg', 'Jan-29'), (32, 'Nafarin A', 'May-28'), (33, 'Norvasc 10mg tab', 'Jan-27'),
    (34, 'Omeprazole 20mg cap', 'Jun-28'), (35, 'Omeprazole 40mg cap', 'Jun-28'), (36, 'Panto Plus cap', 'Sep-28'),
    (37, 'Paracetamol syrup', 'Apr-27'), (38, 'Plavix 75mg tab', 'Jan-27'), (39, 'Ranitidine 150mg tab', 'Feb-28'),
    (40, 'Serc 16mg cap', 'Aug-27'), (41, 'Sinupret tab', 'Aug-27'), (42, 'Strepsils lozenges (8xpack)', 'Jan-29'),
    (43, 'Ventolin nebules', 'Nov-27'), (44, 'Vispring Opthalmic', 'Jan-28'),
]

_SUPPLIES_INVENTORY_TEMPLATE = [
    (1, 'Adhesive steristrips packs 1/2"x4"'), (2, 'Absorbent cotton in balls/pack'),
    (3, 'Arm sling orthopedic small'), (4, 'Arm sling orthopedic medium'),
    (5, 'Arm sling orthopedic large'), (6, 'Arm sling orthopedic x-large'),
    (7, 'Band-Aid 50 strips/box'), (8, 'Betadine 10% solution 120mL'),
    (9, 'Betadine gargle 1% oral antiseptic'), (10, 'Bactidol gargle 0.1% solution'),
    (11, 'Disposable syringe with needle 3mL'), (12, 'Disposable syringe with needle 5mL'),
    (13, 'Disposable syringe with needle 1mL'), (14, 'Efficascent oil ES 100mL'),
    (15, 'Efficascent oil regular 100mL'), (16, 'Elasctic bandage 2"'),
    (17, 'Elasctic bandage 3"'), (18, 'Elasctic bandage 4"'),
    (19, 'Individually packed OS 2x2'), (20, 'Individually packed OS 4x4'),
    (21, 'Micropore plaster 1 inch'), (22, 'Nebulizing kit ADULT'),
    (23, 'Nebulizing kit PEDIA'), (24, 'Non-Rebreathing Mask Adult'),
    (25, 'NSS 1L for irrigation'), (26, 'Omega pain killer'),
    (27, 'Oxygen cannula/mask ADULT'), (28, 'Oxygen cannula PEDIA'),
    (29, 'Salonpas 10pcs/pack x2'), (30, 'Tongue depressors'),
    (31, 'ALCOHOL GREENCROSS'), (32, 'KN95 MASK 50 PCS/BOX'),
]


def _get_census_weeks(year_num: int, month_num: int) -> list[dict]:
    from datetime import date as py_date
    days_in_month = calendar.monthrange(year_num, month_num)[1]
    short_m = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'][month_num - 1]
    # JS getDay(): 0=Sun, 1=Mon .. 6=Sat
    js_dow = lambda d: (py_date(year_num, month_num, d).weekday() + 1) % 7
    mondays = [d for d in range(1, days_in_month + 1) if js_dow(d) == 1]
    first_dow = js_dow(1)
    if 1 <= first_dow <= 3 and len(mondays) < 4:
        starts = [1, *mondays[:3]]
    elif len(mondays) >= 4:
        starts = mondays[:4]
    else:
        starts = [1, 8, 15, 22]

    weeks = []
    for idx, s in enumerate(starts[:4]):
        if idx < 3:
            dow = js_dow(s) or 7
            e = min(days_in_month, s + (6 - dow))
        else:
            e = min(days_in_month, max(s + 5, days_in_month))
        weeks.append({'label': f"{short_m} {s}- {short_m} {e}", 'startDay': s, 'endDay': e})
    return weeks


def _mirror_patients() -> None:
    from .models import Patient
    rows = [
        [
            p.id, p.name, p.category, p.studentCategory or '',
            p.course or p.department or p.position or '',
            p.yearLevel or p.gradeLevel or '', p.sex or '', p.age,
            str(p.birthday or ''), p.contact, p.email or '',
            p.guardianName or '', p.emergencyContact or '', p.emergencyPhone or '',
            p.address or '',
        ]
        for p in Patient.objects.all().order_by('name')
    ]
    _upsert_drive_csv(
        SUBFOLDER_IDS['patients'], 'Patients_Masterlist.csv',
        ['Patient_ID', 'Name', 'Category', 'Student_Level', 'Course_Dept_Position',
         'Year_Grade', 'Sex', 'Age', 'Birthday', 'Contact', 'Email', 'Guardian',
         'Emergency_Contact', 'Emergency_Phone', 'Address'],
        rows, _mirror_lock('patients'),
    )


def _mirror_daily_report(year: int | None = None, month: int | None = None) -> None:
    now = _manila_now()
    yr = year or now.year
    mo = month or now.month
    cons_list = _dedup_month_consultations(yr, mo)
    by_day: dict[int, list] = {}
    for c in cons_list:
        day_num = c.date.day if hasattr(c.date, 'day') else int(str(c.date)[8:10])
        by_day.setdefault(day_num, []).append(c)

    days_in_month = calendar.monthrange(yr, mo)[1]
    mon_abbr = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][mo - 1]
    yr_short = str(yr)[-2:]
    rows, totals = [], [0] * 11
    for day in range(1, days_in_month + 1):
        col = shs = jhs = gs = emp = vis = cons = home = hosp = pre = 0
        for c in by_day.get(day, ()):
            p = getattr(c, 'patient', None)
            if p:
                if p.category == 'Student':
                    sc = p.studentCategory or ''
                    if sc == 'College': col += 1
                    elif sc == 'Senior High School': shs += 1
                    elif sc == 'Junior High School': jhs += 1
                    elif sc == 'Elementary': gs += 1
                elif p.category == 'Employee':
                    emp += 1
                elif p.category == 'Outsider':
                    vis += 1
            if c.status == 'Consultation':
                cons += 1
            if c.earlyDismissal and c.dismissalDestination == 'Sent Home':
                home += 1
            if (c.earlyDismissal and c.dismissalDestination == 'Sent to Hospital') or c.transferred:
                hosp += 1
            purpose = (c.purposeOfVisit or c.complaint or '').lower()
            if 'pre-employment' in purpose or 'pre employment' in purpose:
                pre += 1
        total = col + shs + jhs + gs + emp + vis
        vals = [col, shs, jhs, gs, emp, total, cons, home, hosp, pre, vis]
        for idx, v in enumerate(vals):
            totals[idx] += v
        rows.append([f"{day}-{mon_abbr}-{yr_short}", *[v or '' if i != 5 else v for i, v in enumerate(vals)]])
    rows.append(['TOTAL', *totals])

    _upsert_drive_csv(
        SUBFOLDER_IDS['live_reports'], f"Daily_Report_{yr:04d}-{mo:02d}.csv",
        ['DATE', 'COLLEGE', 'SHS', 'JHS', 'GS', 'EMPLOYEE', 'TOTAL',
         'CONSULTATION', 'SENT HOME', 'SENT TO HOSPITAL', 'PRE-EMPLOYMENT', 'VISITOR'],
        rows, _mirror_lock('daily_report'),
    )


def _mirror_weekly_census(year: int | None = None, month: int | None = None) -> None:
    now = _manila_now()
    yr = year or now.year
    mo = month or now.month
    days_in_month = calendar.monthrange(yr, mo)[1]
    weeks = _get_census_weeks(yr, mo)

    norm_exact = lambda s: re.sub(r'\s+', ' ', (s or '').strip().upper())
    norm_compact = lambda s: re.sub(r'[^A-Z0-9]', '', (s or '').upper())

    groups = [(sheet, list(secs)) for sheet, secs in _CENSUS_SHEET_GROUPS]
    exact_to_canonical: dict[str, str] = {}
    compact_to_canonical: dict[str, str] = {}
    counts: dict[str, list[int]] = {}

    for _, secs in groups:
        for sec in secs:
            counts[sec] = [0, 0, 0, 0]
            exact_to_canonical[norm_exact(sec)] = sec
            comp = norm_compact(sec)
            if comp not in compact_to_canonical:
                compact_to_canonical[comp] = sec

    for c in _dedup_month_consultations(yr, mo):
        p = getattr(c, 'patient', None)
        if not p or p.category != 'Student':
            continue
        if p.studentCategory not in ('Senior High School', 'College'):
            continue
        if p.studentCategory == 'Senior High School' and p.gradeLevel and p.gradeLevel not in ('Grade 11', 'Grade 12'):
            continue
        raw_sec = ((getattr(p, 'section', None) or p.course or '')).strip()
        if not raw_sec:
            continue
        canonical = exact_to_canonical.get(norm_exact(raw_sec)) or compact_to_canonical.get(norm_compact(raw_sec))
        if not canonical:
            # Auto-register custom sections added by staff in Quick Walk-In
            canonical = raw_sec.upper()
            counts[canonical] = [0, 0, 0, 0]
            exact_to_canonical[norm_exact(canonical)] = canonical
            compact_to_canonical[norm_compact(canonical)] = canonical
            target_sheet = 'G11' if p.gradeLevel == 'Grade 11' else ('G12' if p.gradeLevel == 'Grade 12' else 'BSN, BSP')
            for sheet, secs in groups:
                if sheet == target_sheet:
                    secs.append(canonical)
                    break

        day_num = c.date.day if hasattr(c.date, 'day') else int(str(c.date)[8:10])
        if day_num < 1 or day_num > days_in_month:
            continue
        if day_num >= weeks[3]['startDay']:
            w_idx = 3
        elif day_num >= weeks[2]['startDay']:
            w_idx = 2
        elif day_num >= weeks[1]['startDay']:
            w_idx = 1
        else:
            w_idx = 0
        counts[canonical][w_idx] += 1

    rows = []
    grand = [0, 0, 0, 0]
    for sheet, secs in groups:
        for sec in secs:
            c_arr = counts.get(sec, [0, 0, 0, 0])
            row_tot = sum(c_arr)
            for i in range(4):
                grand[i] += c_arr[i]
            rows.append([sheet, sec, c_arr[0], c_arr[1], c_arr[2], c_arr[3], row_tot])
    rows.append(['TOTAL', 'ALL SECTIONS', grand[0], grand[1], grand[2], grand[3], sum(grand)])

    _upsert_drive_csv(
        SUBFOLDER_IDS['live_reports'], f"Weekly_Census_{yr:04d}-{mo:02d}.csv",
        ['SHEET_GROUP', 'YEAR_OR_COURSE_SECTION',
         weeks[0]['label'], weeks[1]['label'], weeks[2]['label'], weeks[3]['label'], 'TOTAL'],
        rows, _mirror_lock('weekly_census'),
    )


def _mirror_cases_attended(year: int | None = None, month: int | None = None) -> None:
    now = _manila_now()
    yr = year or now.year
    mo = month or now.month
    cons_list = _dedup_month_consultations(yr, mo)

    extra_cases: list[str] = []
    for c in cons_list:
        for cat in (c.categories or []):
            cat_s = str(cat or '').strip()
            if cat_s and cat_s not in extra_cases and not any(_is_case_match(ac, cat_s) for ac in _ALL_CASES):
                extra_cases.append(cat_s)

    display_cases = [*_ALL_CASES, *extra_cases]

    def _case_count(case_label: str, target_group: str) -> int:
        is_fever = case_label.lower().strip() == 'fever'
        cnt = 0
        for c in cons_list:
            p = getattr(c, 'patient', None)
            if target_group == 'Student':
                if not (p and p.category == 'Student'):
                    continue
            else:
                if p and p.category not in ('Employee', 'Outsider'):
                    continue
            cats = [str(x or '').strip() for x in (c.categories or []) if str(x or '').strip()]
            if is_fever:
                if any(cn.lower() == 'fever' for cn in cats):
                    cnt += 1
                continue
            if any(_is_case_match(case_label, cn) for cn in cats):
                cnt += 1
            elif c.complaint and _is_case_match(case_label, c.complaint):
                cnt += 1
        return cnt

    rows = []
    total_stu = total_emp = 0
    for case_label in display_cases:
        stu = _case_count(case_label, 'Student')
        emp = _case_count(case_label, 'Personnel')
        tot = stu + emp
        total_stu += stu
        total_emp += emp
        rows.append([case_label, stu or '', emp or '', tot])
    rows.append(['TOTAL CASES ATTENDED', total_stu or '', total_emp or '', total_stu + total_emp])

    _upsert_drive_csv(
        SUBFOLDER_IDS['live_reports'], f"Cases_Attended_{yr:04d}-{mo:02d}.csv",
        ['CASES/COMPLAINS', 'STUDENT', 'PERSONNEL', 'TOTAL'],
        rows, _mirror_lock('cases_attended'),
    )


def _mirror_non_consultations() -> None:
    from .models import Consultation
    qs = Consultation.objects.select_related('patient').prefetch_related('treatments').filter(
        status='Non-Consultation'
    ).order_by('-date', '-timeIn')[:2000]
    rows, seen_ids, seen_sem = [], set(), set()
    for c in qs:
        complaint = (c.complaint or '').strip()
        if '[CONVERTED]' in complaint:
            continue
        cid = str(c.id)
        sem = (str(c.patient_id), str(c.date)[:10], str(c.timeIn or ''), complaint, str(c.status or ''))
        if cid in seen_ids or sem in seen_sem:
            continue
        seen_ids.add(cid)
        seen_sem.add(sem)
        p = getattr(c, 'patient', None)
        treats = list(c.treatments.all())
        items_str = ', '.join(t.medicineName for t in treats) if treats else 'BP measurement / Rest'
        qty_sum = sum(t.quantity for t in treats) if treats else '-'
        rows.append([
            str(c.date), str(c.timeIn or ''), str(c.patient_id), p.name if p else '',
            p.category if p else '', complaint or c.purposeOfVisit or '',
            items_str, qty_sum, c.assistingNurse or c.doctorName or 'Nurse',
        ])
    _upsert_drive_csv(
        SUBFOLDER_IDS['live_reports'], 'Non_Consultation_Report.csv',
        ['Date', 'Time_In', 'Patient_ID', 'Patient_Name', 'Category',
         'Reason_Complaint', 'Item_or_Service', 'Qty', 'Dispensed_By'],
        rows, _mirror_lock('non_consultation'),
    )


def _find_inventory_item(item_name: str, medicines: list):
    target = (item_name or '').lower().strip()
    if not target:
        return None
    for m in medicines:
        if (m.name or '').lower().strip() == target:
            return m
    for m in medicines:
        mn = (m.name or '').lower().strip()
        if mn and (target.startswith(mn) or mn.startswith(target)):
            return m
    first_two = ' '.join(target.split()[:2])
    if len(first_two) > 3:
        for m in medicines:
            if first_two in (m.name or '').lower().strip():
                return m
    first_word = (target.split() or [''])[0]
    if len(first_word) > 3:
        for m in medicines:
            if first_word in (m.name or '').lower().strip():
                return m
    return None


def _format_exp_display(raw: str | None, default_exp: str = '') -> str:
    val = (raw or '').strip() or default_exp
    if not val:
        return ''
    if val.upper() in ('NO STOCK', 'CONSUMED'):
        return val.upper()
    m = re.match(r'^(\d{4})-(\d{2})(?:-\d{2})?$', val)
    if m:
        mo_idx = int(m.group(2)) - 1
        if 0 <= mo_idx < 12:
            short_m = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][mo_idx]
            return f"{short_m}-{m.group(1)[2:]}"
    return val


def _mirror_inventory(year: int | None = None, month: int | None = None) -> None:
    from .models import MedicineItem, StockHistory
    now = _manila_now()
    yr = year or now.year
    mo = month or now.month
    days_in_month = calendar.monthrange(yr, mo)[1]
    ym_prefix = f"{yr:04d}-{mo:02d}"

    medicines = list(MedicineItem.objects.prefetch_related('stockHistory').all().order_by('category', 'name'))
    cons_list = _dedup_month_consultations(yr, mo)

    def _compute_monthly_row(no: int, tpl_name: str, default_exp: str = '', include_status: bool = True) -> list:
        actual = _find_inventory_item(tpl_name, medicines)
        c_days = [0] * 31
        if actual:
            hist_entries = [h for h in actual.stockHistory.all() if h.type == 'dispense']
            for h in hist_entries:
                d_str = _fmt_manila(h.date, '%Y-%m-%d') if hasattr(h.date, 'strftime') else str(h.date or '')[:10]
                if d_str.startswith(ym_prefix):
                    day = int(d_str[8:10])
                    if 1 <= day <= days_in_month:
                        c_days[day - 1] += int(h.qty or 0)
            med_clean = (actual.name or '').lower().strip()
            for cons in cons_list:
                d_str = str(cons.date)[:10]
                if d_str.startswith(ym_prefix):
                    day = int(d_str[8:10])
                    if 1 <= day <= days_in_month:
                        for t in cons.treatments.all():
                            if (t.medicineName or '').lower().strip() == med_clean:
                                already = any(
                                    (_fmt_manila(h.date, '%Y-%m-%d') if hasattr(h.date, 'strftime') else str(h.date or '')[:10]) == d_str
                                    and int(h.qty or 0) == int(t.quantity or 0)
                                    for h in hist_entries
                                )
                                if not already:
                                    c_days[day - 1] += int(t.quantity or 0)

        total = sum(c_days[:days_in_month])
        base_stock = int(actual.stock or 0) if actual else 0
        explicit_beg = int(actual.beginningQty) if (actual and actual.beginningQty is not None) else None
        beg = explicit_beg if (explicit_beg is not None and explicit_beg > base_stock + total) else (base_stock + total)
        end = max(0, beg - total)
        is_consumed = beg > 0 and end <= 0 and total > 0
        is_no_stock = (not is_consumed) and ((end <= 0 or base_stock <= 0) if actual else (end <= 0))

        day_cells = []
        for s_idx, e_idx in ((0, 5), (5, 10), (10, 15), (15, 20), (20, 25), (25, 31)):
            sub = 0
            for i in range(s_idx, e_idx):
                val = 0 if (i + 1) > days_in_month else c_days[i]
                sub += val
                day_cells.append(val if ((i + 1) <= days_in_month and val > 0) else '')
            day_cells.append(sub)

        row = [no, tpl_name, beg, *day_cells, end, total]
        if include_status:
            raw_exp = getattr(actual, 'expirationDate', None) if actual else None
            status_text = 'CONSUMED' if is_consumed else ('NO STOCK' if is_no_stock else _format_exp_display(raw_exp, default_exp))
            row.append(status_text)
        return row

    day_headers = [
        '1', '2', '3', '4', '5', 'Total_1_5',
        '6', '7', '8', '9', '10', 'Total_6_10',
        '11', '12', '13', '14', '15', 'Total_11_15',
        '16', '17', '18', '19', '20', 'Total_16_20',
        '21', '22', '23', '24', '25', 'Total_21_25',
        '26', '27', '28', '29', '30', '31', 'Total_26_31',
    ]

    med_rows = [_compute_monthly_row(no, name, exp, True) for no, name, exp in _MEDICINE_INVENTORY_TEMPLATE]
    med_tpl_names = {name.lower().strip() for _, name, _ in _MEDICINE_INVENTORY_TEMPLATE}
    for m in medicines:
        if m.category == 'Supply':
            continue
        matched = _find_inventory_item(m.name, [type('T', (), {'name': n}) for _, n, _ in _MEDICINE_INVENTORY_TEMPLATE])
        if (m.name or '').lower().strip() not in med_tpl_names and not matched:
            med_rows.append(_compute_monthly_row(len(med_rows) + 1, m.name, '', True))

    _upsert_drive_csv(
        SUBFOLDER_IDS['live_reports'], 'Inventory_Medicines.csv',
        ['No.', 'Medicines', 'Beg. Inv.', *day_headers, 'End. Inv.', 'Sum Total Consumption', 'Status / Expiration Date'],
        med_rows, _mirror_lock('inventory_meds'),
    )

    sup_rows = [_compute_monthly_row(no, name, '', False) for no, name in _SUPPLIES_INVENTORY_TEMPLATE]
    sup_tpl_names = {name.lower().strip() for _, name in _SUPPLIES_INVENTORY_TEMPLATE}
    for m in medicines:
        if m.category != 'Supply':
            continue
        matched = _find_inventory_item(m.name, [type('T', (), {'name': n}) for _, n in _SUPPLIES_INVENTORY_TEMPLATE])
        if (m.name or '').lower().strip() not in sup_tpl_names and not matched:
            sup_rows.append(_compute_monthly_row(len(sup_rows) + 1, m.name, '', False))

    _upsert_drive_csv(
        SUBFOLDER_IDS['live_reports'], 'Inventory_Supplies.csv',
        ['No.', 'Supplies', 'Beg. Inv.', *day_headers, 'End. Inv.', 'Sum Total Consumption'],
        sup_rows, _mirror_lock('inventory_supplies'),
    )

    hist_rows = [
        [_fmt_manila(h.date, '%Y-%m-%d %I:%M %p'), str(h.medicine_id), h.medicine.name, h.medicine.category, h.type, h.qty, h.note or '']
        for h in StockHistory.objects.select_related('medicine').all().order_by('-date')[:2000]
    ]
    _upsert_drive_csv(
        SUBFOLDER_IDS['live_reports'], 'Inventory_Stock_History.csv',
        ['Date_Time', 'Medicine_ID', 'Medicine_Name', 'Category', 'Type', 'Qty', 'Note'],
        hist_rows, _mirror_lock('inventory_hist'),
    )


def _mirror_purchase_requests() -> None:
    from .models import PurchaseRequest
    rows = [
        [
            str(r.id), str(r.date), r.medicine, r.description or '', r.unit or '',
            r.requestedQty, r.receivedQty, str(r.unitPrice) if r.unitPrice is not None else '',
            r.prfNo or '', r.status,
        ]
        for r in PurchaseRequest.objects.all().order_by('-date', 'medicine')[:2000]
    ]
    _upsert_drive_csv(
        SUBFOLDER_IDS['live_reports'], 'Purchase_Requests.csv',
        ['Request_ID', 'Date', 'Medicine', 'Description', 'Unit',
         'Requested_Qty', 'Received_Qty', 'Unit_Price', 'PRF_No', 'Status'],
        rows, _mirror_lock('purchases'),
    )


def _mirror_appointments() -> None:
    from .models import AppointmentRequest
    rows = [
        [
            str(r.id), str(r.patient_id), r.patient.name if r.patient else '',
            r.patient.category if r.patient else 'Student', r.visit_type, r.reason,
            str(r.scheduled_date or r.preferred_date or ''), r.scheduled_time or r.preferred_time or '',
            r.status, _fmt_manila(r.updated_at, '%Y-%m-%d %I:%M %p'),
        ]
        for r in AppointmentRequest.objects.select_related('patient').filter(
            status__in=('Approved', 'Completed')
        ).order_by('-created_at')[:2000]
    ]
    _upsert_drive_csv(
        SUBFOLDER_IDS['live_reports'], 'Appointments_Report.csv',
        ['Appointment_ID', 'Patient_ID', 'Patient_Name', 'Category', 'Visit_Type',
         'Reason', 'Scheduled_Date', 'Scheduled_Time', 'Status', 'Updated_At'],
        rows, _mirror_lock('appointments'),
    )


def _mirror_telemedicine() -> None:
    from .models import TelemedicineRequest
    rows = [
        [
            str(r.id), str(r.patient_id), r.patient.name if r.patient else '',
            r.patient.category if r.patient else 'Student', r.reason,
            str(r.scheduled_date or r.preferred_date or ''), r.scheduled_time or r.preferred_time or '',
            r.meeting_link or '', r.status, _fmt_manila(r.updated_at, '%Y-%m-%d %I:%M %p'),
        ]
        for r in TelemedicineRequest.objects.select_related('patient').filter(
            status__in=('Approved', 'Completed')
        ).order_by('-created_at')[:2000]
    ]
    _upsert_drive_csv(
        SUBFOLDER_IDS['live_reports'], 'Telemedicine_Report.csv',
        ['Telemed_ID', 'Patient_ID', 'Patient_Name', 'Category', 'Reason',
         'Scheduled_Date', 'Scheduled_Time', 'Meeting_Link', 'Status', 'Updated_At'],
        rows, _mirror_lock('telemedicine'),
    )


def _mirror_medical_certificates() -> None:
    from .models import MedicalCertificate
    rows = [
        [
            str(r.id), str(r.date), str(r.patient_id), r.patient.name if r.patient else '',
            r.purpose, r.diagnosis or '', r.recommendation or '',
            r.doctor or '', r.issuedBy or '', r.notes or '',
        ]
        for r in MedicalCertificate.objects.select_related('patient').all().order_by('-date')[:2000]
    ]
    _upsert_drive_csv(
        SUBFOLDER_IDS['live_reports'], 'Medical_Certificates.csv',
        ['Certificate_ID', 'Date', 'Patient_ID', 'Patient_Name', 'Purpose',
         'Diagnosis', 'Recommendation', 'Doctor', 'Issued_By', 'Notes'],
        rows, _mirror_lock('medcerts'),
    )


_MIRRORS = {
    'patients': _mirror_patients,
    'daily_report': _mirror_daily_report,
    'weekly_census': _mirror_weekly_census,
    'cases_attended': _mirror_cases_attended,
    'non_consultation': _mirror_non_consultations,
    'inventory': _mirror_inventory,
    'purchases': _mirror_purchase_requests,
    'appointments': _mirror_appointments,
    'telemedicine': _mirror_telemedicine,
    'medcerts': _mirror_medical_certificates,
}


def run_mirror(key: str) -> None:
    fn = _MIRRORS.get(key)
    if not fn:
        return
    try:
        fn()
    except Exception as exc:
        logger.warning(f"Live Drive mirror '{key}' warning: {exc}")
    finally:
        try:
            from django.conf import settings as dj_settings
            if dj_settings.configured:
                from django.db import close_old_connections
                close_old_connections()
        except Exception:
            pass


# ponytail: 1.5s process-local debounce timers + single lock per mirror key; use Celery/Redis if horizontally scaled
def schedule_mirror(*keys: str, delay: float = 1.5) -> None:
    for key in keys:
        if key not in _MIRRORS:
            continue
        with _MIRROR_SCHED_LOCK:
            prev = _MIRROR_TIMERS.pop(key, None)
            if prev is not None:
                prev.cancel()
            timer = threading.Timer(delay, run_mirror, args=(key,))
            timer.daemon = True
            _MIRROR_TIMERS[key] = timer
            timer.start()


def sync_all_report_mirrors() -> None:
    for key in _MIRRORS:
        run_mirror(key)


def sync_month_report_mirrors(year: int, month: int) -> None:
    for fn in (_mirror_daily_report, _mirror_weekly_census, _mirror_cases_attended, _mirror_inventory):
        try:
            fn(year, month)
        except Exception as exc:
            logger.warning(f"Month mirror {fn.__name__}({year}-{month:02d}) warning: {exc}")
        finally:
            try:
                from django.conf import settings as dj_settings
                if dj_settings.configured:
                    from django.db import close_old_connections
                    close_old_connections()
            except Exception:
                pass


_ARCHIVE_FOLDER_LABELS = {
    'live_reports': 'Reports (Live Auto-Sync)',
    'live_logs': 'Consultations (Daily Log)',
    'archives': 'Reports (Exported Excel)',
    'patients': 'Patients (Masterlist)',
    'audit_logs': 'Settings (Audit Logs)',
}


def list_drive_archives(folder_keys: tuple[str, ...] = ('live_reports', 'live_logs', 'archives', 'patients')) -> list[dict]:
    """Lists Drive backup folders/files from the requested SUBFOLDER_IDS keys."""
    global _SEEDED_MIRRORS
    creds = _get_credentials()
    if not creds or not creds.token:
        return []

    results: list[dict] = []
    live_reports_count = 0
    live_report_names: set[str] = set()
    for folder_key in folder_keys:
        folder_id = SUBFOLDER_IDS.get(folder_key)
        folder_label = _ARCHIVE_FOLDER_LABELS.get(folder_key, folder_key)
        if not folder_id:
            continue
        try:
            q = f"'{folder_id}' in parents and trashed = false"
            resp = http_requests.get(
                'https://www.googleapis.com/drive/v3/files',
                params={
                    'q': q,
                    'fields': 'files(id,name,mimeType,sha256Checksum,description,createdTime,modifiedTime)',
                    'orderBy': 'modifiedTime desc',
                    'pageSize': 100,
                },
                headers={'Authorization': f'Bearer {creds.token}'},
                timeout=8,
            )
            if resp.status_code != 200:
                continue
            for f in (resp.json() or {}).get('files', []):
                desc = f.get('description') or ''
                file_mime = f.get('mimeType') or ''
                fname = f.get('name', 'Archive')
                if file_mime == _FOLDER_MIME and not fname.endswith(('.csv', '.json', '.xlsx')) and not desc.startswith(('CURA_ZLIB:', 'CURA_ZLIB_MULTI:')):
                    continue
                sha = ''
                if desc.startswith(('CURA_ZLIB:', 'CURA_ZLIB_MULTI:')):
                    parts = desc.split(':', 3)
                    if len(parts) >= 3:
                        sha = parts[2]
                elif f.get('sha256Checksum'):
                    sha = f['sha256Checksum'][:16]
                elif desc:
                    sha = hashlib.sha256(desc.encode('utf-8')).hexdigest()[:16]
                fid = f['id']
                if folder_key == 'live_reports':
                    live_reports_count += 1
                    live_report_names.add(fname)
                results.append({
                    'id': fid,
                    'name': fname,
                    'category': folder_label,
                    'folder_key': folder_key,
                    'sha256': sha,
                    'created_at': f.get('modifiedTime') or f.get('createdTime') or '',
                    'download_url': f"{BACKEND_PUBLIC_URL}/api/reports/archives/{fid}/download/",
                    'drive_url': _drive_url(fid, file_mime),
                })
        except Exception as exc:
            logger.warning(f"Failed listing {folder_key}: {exc}")

    missing_core_mirrors = (
        live_reports_count == 0
        or 'Inventory_Supplies.csv' not in live_report_names
        or not any(n.startswith('Weekly_Census_') for n in live_report_names)
    )
    if 'live_reports' in folder_keys and missing_core_mirrors and not _SEEDED_MIRRORS:
        with _MIRROR_SCHED_LOCK:
            if not _SEEDED_MIRRORS:
                _SEEDED_MIRRORS = True
                threading.Thread(target=sync_all_report_mirrors, daemon=True).start()

    return results
