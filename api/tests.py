import io
import os
import unittest
from unittest.mock import MagicMock, patch
from PIL import Image
from api import gdrive_service as gs


class DriveOAuthTests(unittest.TestCase):
    def setUp(self):
        gs._CREDS = None
        gs._MEDIA_CACHE.clear()

    def test_oauth_credentials_and_real_file_upload_download_list(self):
        def ok_refresh(self, _req):
            self.token = 'oauth-tok'

        with patch.dict(os.environ, {
            'GOOGLE_OAUTH_CLIENT_ID': 'cid',
            'GOOGLE_OAUTH_CLIENT_SECRET': 'csec',
            'GOOGLE_OAUTH_REFRESH_TOKEN': 'rtok',
        }, clear=False), patch.object(gs.UserCredentials, 'refresh', ok_refresh):
            creds = gs._get_credentials()
            self.assertTrue(gs._using_user_oauth(creds))

        posted = {}
        def mock_post(url, headers=None, data=None, json=None, timeout=None):
            posted['url'] = url
            posted['data'] = data
            r = MagicMock(status_code=200)
            r.json.return_value = {'id': 'real_1', 'name': 'Report.csv', 'mimeType': 'text/csv', 'createdTime': '2026-11-13T09:00:00Z'}
            return r

        with patch.object(creds, 'refresh', ok_refresh), \
             patch('api.gdrive_service.http_requests.post', side_effect=mock_post):
            info = gs.store_archive_file(b'a,b\n1,2\n', 'Report.csv', 'text/csv; charset=utf-8', 'audit_logs', 'ADMIN', 'Test')
            self.assertEqual(info['drive_url'], 'https://drive.google.com/file/d/real_1/view')
            self.assertIn('uploadType=multipart', posted['url'])
            self.assertNotIn(b'application/vnd.google-apps.folder', posted['data'])

        gs._MEDIA_CACHE.clear()
        def mock_get(url, params=None, headers=None, timeout=None):
            r = MagicMock(status_code=200)
            if 'alt=media' in url:
                r.content = b'a,b\n1,2\n'
            elif '/files/' in url:
                r.json.return_value = {'name': 'Report.csv', 'mimeType': 'text/csv', 'description': ''}
            else:
                r.json.return_value = {'files': [
                    {'id': 'real_1', 'name': 'Report.csv', 'mimeType': 'text/csv', 'sha256Checksum': '0123456789abcdef99', 'description': '', 'createdTime': '2026-11-13T09:00:00Z'},
                ]}
            return r

        with patch.object(creds, 'refresh', ok_refresh), \
             patch('api.gdrive_service.http_requests.get', side_effect=mock_get):
            raw, mime = gs.fetch_media_from_drive('real_1')
            self.assertEqual((raw, mime), (b'a,b\n1,2\n', 'text/csv; charset=utf-8'))
            listed = gs.list_drive_archives(('audit_logs',))
            self.assertEqual(listed[0]['drive_url'], 'https://drive.google.com/file/d/real_1/view')
            self.assertEqual(listed[0]['sha256'], '0123456789abcdef')

    def test_daily_csv_seeds_legacy_folder_and_updates_real_file(self):
        creds = gs.UserCredentials(token='t', refresh_token='r', token_uri='https://oauth2.googleapis.com/token', client_id='c', client_secret='s')
        posted, patched = {}, {}

        def mock_get_seed(url, params=None, headers=None, timeout=None):
            r = MagicMock(status_code=200)
            r.json.return_value = {'files': [{'id': 'old', 'name': 'Audit_Trail_2026-11-13.csv', 'mimeType': gs._FOLDER_MIME, 'description': 'A,B\n1,2\n'}]}
            return r

        def mock_post(url, headers=None, data=None, json=None, timeout=None):
            posted['data'] = data
            r = MagicMock(status_code=200)
            r.json.return_value = {'id': 'new_csv'}
            return r

        with patch('api.gdrive_service.http_requests.get', side_effect=mock_get_seed), \
             patch('api.gdrive_service.http_requests.post', side_effect=mock_post):
            gs._append_daily_csv_oauth(creds, 'audit_dir', 'Audit_Trail_2026-11-13.csv', ['A', 'B'], ['3', '4'], gs._AUDIT_LOCK)
            self.assertIn(b'A,B\n1,2\n3,4\n', posted['data'])

        def mock_get_update(url, params=None, headers=None, timeout=None):
            r = MagicMock(status_code=200)
            if 'alt=media' in url:
                r.content = b'A,B\n1,2\n3,4\n'
            else:
                r.json.return_value = {'files': [{'id': 'new_csv', 'name': 'Audit_Trail_2026-11-13.csv', 'mimeType': 'text/csv', 'description': ''}]}
            return r

        def mock_patch(url, headers=None, data=None, json=None, timeout=None):
            patched['url'], patched['data'] = url, data
            return MagicMock(status_code=200)

        with patch('api.gdrive_service.http_requests.get', side_effect=mock_get_update), \
             patch('api.gdrive_service.http_requests.patch', side_effect=mock_patch):
            gs._append_daily_csv_oauth(creds, 'audit_dir', 'Audit_Trail_2026-11-13.csv', ['A', 'B'], ['5', '6'], gs._AUDIT_LOCK)
            self.assertIn('new_csv?uploadType=media', patched['url'])
            self.assertEqual(patched['data'], b'A,B\n1,2\n3,4\n5,6\n')

    def test_offload_image_uses_readable_patient_filename(self):
        creds = gs.UserCredentials(token='t', refresh_token='r', token_uri='https://oauth2.googleapis.com/token', client_id='c', client_secret='s')
        gs._CREDS = creds
        buf = io.BytesIO()
        Image.new('RGB', (10, 10), (10, 20, 30)).save(buf, format='PNG')
        posted = {}

        def mock_post(url, headers=None, data=None, json=None, timeout=None):
            posted['data'] = data
            r = MagicMock(status_code=200)
            r.json.return_value = {'id': 'img_1'}
            return r

        with patch('api.gdrive_service.http_requests.post', side_effect=mock_post):
            out = gs.offload_image(buf.getvalue(), category='prescription', label='2023001322_DELA CRUZ')
            self.assertTrue(out.endswith('/api/media/img_1/'))
            self.assertIn(b'_2023001322_DELA_CRUZ_Prescription.webp', posted['data'])



