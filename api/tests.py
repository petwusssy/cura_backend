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

    def test_upsert_drive_csv_creates_and_overwrites_in_place(self):
        creds = gs.UserCredentials(token='t', refresh_token='r', token_uri='https://oauth2.googleapis.com/token', client_id='c', client_secret='s')
        gs._CREDS = creds
        posted, patched = {}, {}

        def mock_get_empty(url, params=None, headers=None, timeout=None):
            r = MagicMock(status_code=200)
            r.json.return_value = {'files': []}
            return r

        def mock_post(url, headers=None, data=None, json=None, timeout=None):
            posted['data'] = data
            r = MagicMock(status_code=200)
            r.json.return_value = {'id': 'm1'}
            return r

        with patch('api.gdrive_service.http_requests.get', side_effect=mock_get_empty), \
             patch('api.gdrive_service.http_requests.post', side_effect=mock_post):
            gs._upsert_drive_csv('lr_dir', 'Appointments_Report.csv', ['ID', 'Status'], [['a1', 'Pending']], gs._mirror_lock('t_up'))
            self.assertIn(b'ID,Status\na1,Pending\n', posted['data'])

        def mock_get_exists(url, params=None, headers=None, timeout=None):
            r = MagicMock(status_code=200)
            r.json.return_value = {'files': [{'id': 'm1', 'name': 'Appointments_Report.csv', 'mimeType': 'text/csv'}]}
            return r

        def mock_patch(url, headers=None, data=None, json=None, timeout=None):
            patched['url'], patched['data'] = url, data
            return MagicMock(status_code=200)

        with patch('api.gdrive_service.http_requests.get', side_effect=mock_get_exists), \
             patch('api.gdrive_service.http_requests.patch', side_effect=mock_patch):
            gs._upsert_drive_csv('lr_dir', 'Appointments_Report.csv', ['ID', 'Status'], [['a1', 'Approved']], gs._mirror_lock('t_up'))
            self.assertIn('m1?uploadType=media', patched['url'])
            self.assertEqual(patched['data'], b'ID,Status\na1,Approved\n')

    def test_list_drive_archives_skips_plain_folders_and_schedule_mirror_debounces(self):
        creds = gs.UserCredentials(token='t', refresh_token='r', token_uri='https://oauth2.googleapis.com/token', client_id='c', client_secret='s')
        gs._CREDS = creds

        def mock_get(url, params=None, headers=None, timeout=None):
            r = MagicMock(status_code=200)
            r.json.return_value = {'files': [
                {'id': 'sub_dir', 'name': 'Patient Profile Photos', 'mimeType': gs._FOLDER_MIME, 'description': 'Patient profile photos', 'createdTime': '2026-11-13T09:00:00Z'},
                {'id': 'csv_1', 'name': 'Patients_Masterlist.csv', 'mimeType': 'text/csv', 'description': '', 'createdTime': '2026-11-13T09:01:00Z'},
            ]}
            return r

        with patch('api.gdrive_service.http_requests.get', side_effect=mock_get):
            items = gs.list_drive_archives(('patients',))
            self.assertEqual([x['name'] for x in items], ['Patients_Masterlist.csv'])
            self.assertEqual(items[0]['category'], 'Patients (Masterlist)')

        ran = []
        with patch.dict(gs._MIRRORS, {'appointments': lambda: ran.append('appointments')}):
            gs.schedule_mirror('appointments', 'appointments', delay=0.05)
            gs.time.sleep(0.15)
            self.assertEqual(ran, ['appointments'])


class PrivacySecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
        import django
        django.setup()

    def test_security_and_cors_defaults(self):
        from django.conf import settings as dj_settings
        self.assertFalse(dj_settings.CORS_ALLOW_ALL_ORIGINS)
        self.assertTrue(dj_settings.SECURE_CONTENT_TYPE_NOSNIFF)
        self.assertEqual(dj_settings.X_FRAME_OPTIONS, 'DENY')
        self.assertEqual(dj_settings.SECURE_REFERRER_POLICY, 'strict-origin-when-cross-origin')
        self.assertTrue(dj_settings.FRONTEND_PUBLIC_URL.startswith('https://'))
        self.assertTrue(dj_settings.BACKEND_PUBLIC_URL.startswith('https://'))
        self.assertGreaterEqual(len(dj_settings.CSRF_TRUSTED_ORIGINS), 1)

    def test_record_privacy_consent_only_when_accepted(self):
        from api import views
        with patch('api.models.AuditLog.objects.create') as mock_create:
            views._record_privacy_consent({}, 'STU-1', 'JANE DOE', 'Mobile Registration')
            mock_create.assert_not_called()

            views._record_privacy_consent(
                {'privacy_accepted': True, 'privacy_policy_version': '2026-06'},
                'STU-1',
                'JANE DOE',
                'Mobile Registration',
            )
            mock_create.assert_called_once()
            kwargs = mock_create.call_args.kwargs
            self.assertEqual(kwargs['action'], 'PRIVACY_CONSENT_ACCEPTED')
            self.assertEqual(kwargs['target_id'], 'STU-1')
            self.assertIn('2026-06', kwargs['details'])

    def test_change_password_rejects_unauthenticated_blank_current_password(self):
        from rest_framework.test import APIRequestFactory
        from api.views import ChangePasswordView

        factory = APIRequestFactory()
        fake_user = MagicMock(pk=7, email='student@ua.edu.ph')
        fake_user.check_password.side_effect = lambda pw: pw == 'OldPass123'

        qs = MagicMock()
        qs.first.return_value = fake_user

        with patch('api.views.User.objects.filter', return_value=qs):
            req_blank = factory.post('/api/auth/change-password/', {
                'email': 'student@ua.edu.ph',
                'current_password': '',
                'new_password': 'NewPass123',
            }, format='json')
            resp_blank = ChangePasswordView.as_view()(req_blank)
            self.assertEqual(resp_blank.status_code, 400)
            fake_user.set_password.assert_not_called()

            req_ok = factory.post('/api/auth/change-password/', {
                'email': 'student@ua.edu.ph',
                'current_password': 'OldPass123',
                'new_password': 'NewPass123',
            }, format='json')
            resp_ok = ChangePasswordView.as_view()(req_ok)
            self.assertEqual(resp_ok.status_code, 200)
            fake_user.set_password.assert_called_once_with('NewPass123')
            fake_user.save.assert_called_once()




