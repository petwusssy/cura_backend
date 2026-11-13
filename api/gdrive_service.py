import base64
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
from datetime import datetime
from pathlib import Path
import requests as http_requests
from PIL import Image
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as UserCredentials
from google.auth.transport.requests import Request as GoogleAuthRequest

logger = logging.getLogger(__name__)

ROOT_FOLDER_ID = os.environ.get('GOOGLE_DRIVE_FOLDER_ID', '1lc-gyNPuUUjQUL1s07-fMcBumyWnSEIW')
SUBFOLDER_IDS = {
    'avatar': '1rDLDeJ1NHbVcUFEEFnNMtpvts0jhP70Y',
    'prescription': '1Pw0ABWq5rGfQdkXEoEKOesfSgwudGRc_',
    'fetcher_id': '1M4NMSmFdjCTCct_cdZ0jpSIt_-lEWSiD',
    'live_logs': '1S52a2jI60YjFOzyal5RPrpOMOlLpltmT',
    'archives': '19PlSdFmCDCKLnt0dvDyRLo40COrTYwLQ',
    'audit_logs': os.environ.get('GOOGLE_DRIVE_AUDIT_FOLDER_ID', '1qRJu1lh8pOmwJ8W_ugpc_OEt8ML7cnie'),
}
BACKEND_PUBLIC_URL = os.environ.get('BACKEND_PUBLIC_URL', 'https://cura-backend-dvj5.onrender.com').rstrip('/')
_FOLDER_MIME = 'application/vnd.google-apps.folder'

# ponytail: in-process dict cache (512 items), swap for Redis/CDN only if multi-worker memory matters
_MEDIA_CACHE: dict[str, tuple[bytes, str]] = {}
_CREDS = None
_LOCK = threading.Lock()
_AUDIT_LOCK = threading.Lock()
_CONSULT_LOCK = threading.Lock()


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
    if len(_MEDIA_CACHE) >= 512:
        _MEDIA_CACHE.pop(next(iter(_MEDIA_CACHE)))
    _MEDIA_CACHE[drive_id] = (raw_bytes, mime_type)


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
                _MEDIA_CACHE.pop(real_file['id'], None)
        else:
            _upload_drive_file(creds, filename, csv_bytes, 'text/csv', folder_id)


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
    if drive_id in _MEDIA_CACHE:
        return _MEDIA_CACHE[drive_id]

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
        'created_at': node.get('createdTime') or datetime.utcnow().isoformat() + 'Z',
        'download_url': f"{BACKEND_PUBLIC_URL}/api/reports/archives/{drive_id}/download/",
        'drive_url': _drive_url(drive_id, node_mime),
    }

    try:
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

        date_str = str(payload.get('date') or datetime.now().strftime('%Y-%m-%d'))[:10]
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
            _MEDIA_CACHE.pop(ledger_id, None)

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
            'synced_at': datetime.utcnow().isoformat() + 'Z',
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

        date_str = str(payload.get('date') or datetime.now().strftime('%Y-%m-%d'))[:10]
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
                _MEDIA_CACHE.pop(ledger_id, None)
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
        ts = getattr(audit_log, 'timestamp', None) or datetime.utcnow()
        payload = {
            'date': ts.strftime('%Y-%m-%d') if hasattr(ts, 'strftime') else datetime.now().strftime('%Y-%m-%d'),
            'timestamp': ts.isoformat() if hasattr(ts, 'isoformat') else str(ts),
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


_ARCHIVE_FOLDER_LABELS = {
    'live_logs': 'Live Daily Log',
    'archives': 'Official Report Archive',
    'audit_logs': 'Audit Log Backup',
}


def list_drive_archives(folder_keys: tuple[str, ...] = ('live_logs', 'archives')) -> list[dict]:
    """Lists Drive backup folders/files from the requested SUBFOLDER_IDS keys."""
    creds = _get_credentials()
    if not creds or not creds.token:
        return []

    results: list[dict] = []
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
                    'pageSize': 50,
                },
                headers={'Authorization': f'Bearer {creds.token}'},
                timeout=8,
            )
            if resp.status_code != 200:
                continue
            for f in (resp.json() or {}).get('files', []):
                desc = f.get('description') or ''
                file_mime = f.get('mimeType') or ''
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
                results.append({
                    'id': fid,
                    'name': f.get('name', 'Archive'),
                    'category': folder_label,
                    'folder_key': folder_key,
                    'sha256': sha,
                    'created_at': f.get('modifiedTime') or f.get('createdTime') or '',
                    'download_url': f"{BACKEND_PUBLIC_URL}/api/reports/archives/{fid}/download/",
                    'drive_url': _drive_url(fid, file_mime),
                })
        except Exception as exc:
            logger.warning(f"Failed listing {folder_key}: {exc}")

    return results
