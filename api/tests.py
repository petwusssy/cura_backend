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

    def test_drive_timestamps_use_manila_time(self):
        from datetime import datetime, timezone as dt_tz
        # 2026-11-14 23:45 UTC == 2026-11-15 07:45 AM Asia/Manila (+08:00)
        utc_dt = datetime(2026, 11, 14, 23, 45, 12, tzinfo=dt_tz.utc)
        self.assertEqual(gs._fmt_manila(utc_dt, '%Y-%m-%d'), '2026-11-15')
        self.assertEqual(gs._fmt_manila(utc_dt, '%Y-%m-%d %I:%M %p'), '2026-11-15 07:45 AM')
        self.assertEqual(gs._fmt_manila(utc_dt), '2026-11-15 07:45:12 AM')

        captured = {}
        class FakeThread:
            def __init__(self, target=None, args=(), daemon=False):
                captured['payload'] = args[0]
            def start(self):
                pass
        fake_log = MagicMock(
            action='STAFF_LOGIN', staff_name_snapshot='NURSE', staff_role_snapshot='Head Nurse',
            target_type='Staff', target_id='1', details='ok', timestamp=utc_dt,
        )
        with patch('api.gdrive_service.threading.Thread', FakeThread):
            gs.trigger_live_audit_sync(fake_log)
        self.assertEqual(captured['payload']['date'], '2026-11-15')
        self.assertEqual(captured['payload']['timestamp'], '2026-11-15 07:45:12 AM')


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

    def test_change_admin_password_rejects_blank_current_password(self):
        from rest_framework.test import APIRequestFactory
        from api.views import ClinicStaffViewSet

        factory = APIRequestFactory()
        view = ClinicStaffViewSet.as_view({'post': 'change_admin_password'})
        req = factory.post('/api/staff/change-admin-password/', {
            'current_password': '',
            'new_password': 'HackedPassword123',
        }, format='json')
        resp = view(req)
        self.assertEqual(resp.status_code, 401)

    def test_google_login_rejects_unsigned_forged_jwt(self):
        from rest_framework.test import APIRequestFactory
        from api.views import GoogleLoginView

        factory = APIRequestFactory()
        req = factory.post('/api/auth/google/', {
            'id_token': 'eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.eyJpc3MiOiJodHRwczovL3NlY3VyZXRva2VuLmdvb2dsZS5jb20vY3VyYS1jbGluaWMtNjViOTQiLCJhdWQiOiJjdXJhLWNsaW5pYy02NWI5NCIsImVtYWlsIjoiadmin@ua.edu.phIn0.',
        }, format='json')
        resp = GoogleLoginView.as_view()(req)
        self.assertEqual(resp.status_code, 401)

    def test_audit_log_queryset_only_slices_on_list(self):
        from api.views import AuditLogViewSet

        vs_list = AuditLogViewSet()
        vs_list.action = 'list'
        self.assertTrue(vs_list.get_queryset().query.is_sliced)

        vs_detail = AuditLogViewSet()
        vs_detail.action = 'retrieve'
        self.assertFalse(vs_detail.get_queryset().query.is_sliced)

    def test_notification_delete_and_web_clear_all_preserve_patient_rows(self):
        from rest_framework.test import APIRequestFactory
        from api.models import AppNotification
        from api.views import AppNotificationViewSet

        web_notif = AppNotification.objects.create(
            type='appointment_request',
            message='New appointment request from Juan',
            patientName='JUAN',
        )
        patient_turn = AppNotification.objects.create(
            type='general',
            message="It's your turn! Please proceed to the clinic counter.",
            patientName='JUAN',
            patient_id='STU-100',
        )
        patient_update = AppNotification.objects.create(
            type='appointment_update',
            message='Your appointment has been approved.',
            patientName='JUAN',
            patient_id='STU-100',
        )

        factory = APIRequestFactory()
        # Individual DELETE removes row permanently
        del_req = factory.delete(f'/api/notifications/{web_notif.id}/')
        del_view = AppNotificationViewSet.as_view({'delete': 'destroy'})
        del_resp = del_view(del_req, pk=str(web_notif.id))
        self.assertEqual(del_resp.status_code, 204)
        self.assertFalse(AppNotification.objects.filter(pk=web_notif.id).exists())

        # Create another web notification, clear_all?audience=web should delete web only
        web_notif_2 = AppNotification.objects.create(
            type='medcert_request',
            message='New medcert request',
            patientName='JUAN',
        )
        clear_req = factory.post('/api/notifications/clear_all/?audience=web')
        clear_view = AppNotificationViewSet.as_view({'post': 'clear_all'})
        clear_resp = clear_view(clear_req)
        self.assertEqual(clear_resp.status_code, 200)
        self.assertFalse(AppNotification.objects.filter(pk=web_notif_2.id).exists())
        self.assertTrue(AppNotification.objects.filter(pk=patient_turn.id).exists())
        self.assertTrue(AppNotification.objects.filter(pk=patient_update.id).exists())

    def test_verify_admin_rate_limits_after_five_failures(self):
        from rest_framework.test import APIRequestFactory
        from api.models import LoginAttempt
        from api.views import ClinicStaffViewSet

        LoginAttempt.objects.filter(identifier='__cura_user_mgmt__:10.0.0.9').delete()
        factory = APIRequestFactory()
        view = ClinicStaffViewSet.as_view({'post': 'verify_admin'})
        try:
            for expected_remaining in (4, 3, 2, 1):
                req = factory.post('/api/staff/verify-admin/', {'password': 'wrong'}, format='json', REMOTE_ADDR='10.0.0.9')
                resp = view(req)
                self.assertEqual(resp.status_code, 401)
                self.assertEqual(resp.data['attempts_remaining'], expected_remaining)
                self.assertFalse(resp.data['is_locked'])

            req_5 = factory.post('/api/staff/verify-admin/', {'password': 'wrong'}, format='json', REMOTE_ADDR='10.0.0.9')
            resp_5 = view(req_5)
            self.assertEqual(resp_5.status_code, 429)
            self.assertTrue(resp_5.data['is_locked'])
            self.assertEqual(resp_5.data['attempts_remaining'], 0)
        finally:
            LoginAttempt.objects.filter(identifier='__cura_user_mgmt__:10.0.0.9').delete()

    def test_staff_password_change_requires_master_key_and_invalidates_old_tokens(self):
        import os
        from unittest.mock import patch
        from django.contrib.auth.models import User
        from rest_framework.exceptions import AuthenticationFailed
        from rest_framework.test import APIRequestFactory, force_authenticate
        from rest_framework_simplejwt.tokens import AccessToken
        from api.auth_utils import (
            VerifiedJWTAuthentication,
            mint_tokens_for_user,
            verify_refresh_token_signature,
        )
        from api.views import ChangePasswordView, CustomTokenRefreshView

        User.objects.filter(username='__test_staff_pwd__').delete()
        staff_user = User.objects.create_user(
            username='__test_staff_pwd__',
            email='stafftest@ua.edu.ph',
            password='OldClinicPass!123',
            is_staff=True,
        )
        try:
            old_refresh, _ = mint_tokens_for_user(staff_user)
            old_access_str = str(old_refresh.access_token)
            old_refresh_str = str(old_refresh)

            jwt_auth = VerifiedJWTAuthentication()
            self.assertEqual(jwt_auth.get_user(AccessToken(old_access_str)).pk, staff_user.pk)
            self.assertTrue(verify_refresh_token_signature(old_refresh_str))

            factory = APIRequestFactory()
            change_view = ChangePasswordView.as_view()

            with patch.dict(os.environ, {'USER_MANAGEMENT_PASSWORD': 'MasterKey999!'}, clear=False):
                # Missing or wrong Master Key must return 403 even if authenticated as self
                bad_req = factory.post('/api/auth/change-password/', {
                    'email': '__test_staff_pwd__',
                    'current_password': 'OldClinicPass!123',
                    'new_password': 'NewClinicPass!456',
                    'admin_password': 'wrong-master-key',
                }, format='json')
                force_authenticate(bad_req, user=staff_user)
                bad_resp = change_view(bad_req)
                self.assertEqual(bad_resp.status_code, 403)
                staff_user.refresh_from_db()
                self.assertTrue(staff_user.check_password('OldClinicPass!123'))

                # Valid current password + valid Master Key succeeds and mints fresh tokens
                ok_req = factory.post('/api/auth/change-password/', {
                    'email': '__test_staff_pwd__',
                    'current_password': 'OldClinicPass!123',
                    'new_password': 'NewClinicPass!456',
                    'admin_password': 'MasterKey999!',
                }, format='json')
                force_authenticate(ok_req, user=staff_user)
                ok_resp = change_view(ok_req)
                self.assertEqual(ok_resp.status_code, 200)
                self.assertIn('access', ok_resp.data)

            staff_user.refresh_from_db()
            self.assertTrue(staff_user.check_password('NewClinicPass!456'))

            # Old access and refresh tokens (on other clinic PCs) are immediately rejected
            with self.assertRaises(AuthenticationFailed):
                jwt_auth.get_user(AccessToken(old_access_str))
            self.assertFalse(verify_refresh_token_signature(old_refresh_str))

            refresh_req = factory.post('/api/auth/refresh/', {'refresh': old_refresh_str}, format='json')
            refresh_resp = CustomTokenRefreshView.as_view()(refresh_req)
            self.assertEqual(refresh_resp.status_code, 401)

            # Head Nurse's rotated access token remains valid
            new_user = jwt_auth.get_user(AccessToken(ok_resp.data['access']))
            self.assertEqual(new_user.pk, staff_user.pk)
        finally:
            User.objects.filter(username='__test_staff_pwd__').delete()

    def test_telemedicine_and_appointments_default_locked_and_toggleable(self):
        from rest_framework.test import APIRequestFactory
        from api.models import AppointmentAvailability, TelemedicineAvailability
        from api.views import AppointmentRequestViewSet, TelemedicineRequestViewSet

        factory = APIRequestFactory()
        for url, vs, avail_cls in (
            ('/api/telemedicine/', TelemedicineRequestViewSet, TelemedicineAvailability),
            ('/api/appointments/', AppointmentRequestViewSet, AppointmentAvailability),
        ):
            existing = avail_cls.objects.first()
            snapshot = (existing.unavailable_dates, existing.unavailable_times) if existing else None
            avail_cls.objects.all().delete()
            try:
                avail_view = vs.as_view({'get': 'availability', 'post': 'availability'})
                create_view = vs.as_view({'post': 'create'})

                # Locked by default on fresh deploy
                get_resp = avail_view(factory.get(f'{url}availability/'))
                self.assertEqual(get_resp.status_code, 200)
                self.assertTrue(get_resp.data['is_locked'])
                self.assertEqual(get_resp.data['unavailable_times'], [])

                # Direct booking rejected while locked
                blocked_create = create_view(factory.post(url, {'patient': 'STU-1'}, format='json'))
                self.assertEqual(blocked_create.status_code, 403)

                # Save blocked dates/times preserves lock state
                save_dates = avail_view(factory.post(f'{url}availability/', {
                    'unavailable_dates': ['2026-12-01'],
                    'unavailable_times': ['8:00 AM - 9:00 AM'],
                }, format='json'))
                self.assertTrue(save_dates.data['is_locked'])

                # Unlocking preserves saved unavailable_dates/times and hides sentinel
                unlock_resp = avail_view(factory.post(f'{url}availability/', {'is_locked': False}, format='json'))
                self.assertFalse(unlock_resp.data['is_locked'])
                self.assertEqual(unlock_resp.data['unavailable_dates'], ['2026-12-01'])
                self.assertEqual(unlock_resp.data['unavailable_times'], ['8:00 AM - 9:00 AM'])

                # Re-locking works cleanly
                lock_resp = avail_view(factory.post(f'{url}availability/', {'is_locked': True}, format='json'))
                self.assertTrue(lock_resp.data['is_locked'])
                self.assertEqual(lock_resp.data['unavailable_times'], ['8:00 AM - 9:00 AM'])
                self.assertIn('booked_slots', get_resp.data)
            finally:
                avail_cls.objects.all().delete()
                if snapshot is not None:
                    avail_cls.objects.create(unavailable_dates=snapshot[0], unavailable_times=snapshot[1])

    def test_otp_failure_returns_503_and_forgot_password_flow_works(self):
        from django.contrib.auth.models import User
        from rest_framework.test import APIRequestFactory
        from api.models import LoginAttempt, OTPVerification
        from api.views import RequestOTPView, VerifyOTPView, SetPasswordView

        factory = APIRequestFactory()
        email = '__test_otp_patient__@ua.edu.ph'
        User.objects.filter(username=email).delete()
        OTPVerification.objects.filter(email=email).delete()
        LoginAttempt.objects.filter(identifier=f'otp:{email}').delete()
        u = User.objects.create_user(username=email, email=email, password='OldPassword1')
        try:
            with patch('api.views._send_clinic_email', return_value=False):
                fail_resp = RequestOTPView.as_view()(factory.post('/api/auth/request-otp/', {'email': email}, format='json'))
                self.assertEqual(fail_resp.status_code, 503)

            with patch('api.views._send_clinic_email', return_value=True):
                ok_resp = RequestOTPView.as_view()(factory.post('/api/auth/request-otp/', {'email': email}, format='json'))
                self.assertEqual(ok_resp.status_code, 200)

            code = OTPVerification.objects.filter(email=email).latest('id').otp
            verify_resp = VerifyOTPView.as_view()(factory.post('/api/auth/verify-otp/', {'email': email, 'otp': code}, format='json'))
            self.assertEqual(verify_resp.status_code, 200)

            # OTP replay to verify-otp must fail
            replay_resp = VerifyOTPView.as_view()(factory.post('/api/auth/verify-otp/', {'email': email, 'otp': code}, format='json'))
            self.assertEqual(replay_resp.status_code, 400)

            # Forgot password Step 3 (SetPasswordView) succeeds and consumes OTP
            reset_resp = SetPasswordView.as_view()(factory.post('/api/auth/set-password/', {'email': email, 'password': 'NewPassword2'}, format='json'))
            self.assertEqual(reset_resp.status_code, 200)
            self.assertIn('access', reset_resp.data)
            u.refresh_from_db()
            self.assertTrue(u.check_password('NewPassword2'))
            self.assertFalse(OTPVerification.objects.filter(email=email).exists())
        finally:
            OTPVerification.objects.filter(email=email).delete()
            LoginAttempt.objects.filter(identifier=f'otp:{email}').delete()
            User.objects.filter(username=email).delete()

    def test_verify_pin_rate_limits_after_five_failures(self):
        from django.contrib.auth.hashers import make_password
        from rest_framework.test import APIRequestFactory
        from api.models import ClinicStaff, LoginAttempt
        from api.views import ClinicStaffViewSet, STAFF_PIN_SENTINEL

        factory = APIRequestFactory()
        view = ClinicStaffViewSet.as_view({'post': 'verify_pin'})
        LoginAttempt.objects.filter(identifier__startswith=STAFF_PIN_SENTINEL).delete()
        staff = ClinicStaff.objects.create(
            name='__TEST_PIN_NURSE__',
            role='Nurse',
            pin_hash=make_password('4829'),
            is_active=True,
        )
        try:
            for _ in range(4):
                resp = view(factory.post('/api/staff/verify-pin/', {'pin': '0000'}, format='json'))
                self.assertEqual(resp.status_code, 401)
                self.assertFalse(resp.data['is_locked'])

            fifth = view(factory.post('/api/staff/verify-pin/', {'pin': '0000'}, format='json'))
            self.assertEqual(fifth.status_code, 429)
            self.assertTrue(fifth.data['is_locked'])

            blocked = view(factory.post('/api/staff/verify-pin/', {'pin': '4829'}, format='json'))
            self.assertEqual(blocked.status_code, 429)
            self.assertTrue(blocked.data['is_locked'])
        finally:
            ClinicStaff.objects.filter(id=staff.id).delete()
            LoginAttempt.objects.filter(identifier__startswith=STAFF_PIN_SENTINEL).delete()

    def test_purchase_request_atomic_receive_void_and_delete_guard(self):
        from datetime import date
        from rest_framework.test import APIRequestFactory
        from api.models import MedicineItem, PurchaseHistory, PurchaseRequest, StockHistory
        from api.views import PurchaseRequestViewSet

        factory = APIRequestFactory()
        med = MedicineItem.objects.create(
            name='__TEST_PARACETAMOL__',
            stock=10,
            threshold=15,
            status='Low Stock',
            unit='Tablet',
        )
        req_receive = PurchaseRequest.objects.create(
            medicine='__TEST_PARACETAMOL__',
            requestedQty=20,
            receivedQty=0,
            date=date(2026, 11, 15),
            status='Pending',
            prfNo='PRF - 2026 - 999',
        )
        req_void = PurchaseRequest.objects.create(
            medicine='__TEST_PARACETAMOL__',
            requestedQty=10,
            receivedQty=0,
            date=date(2026, 11, 15),
            status='Pending',
            prfNo='PRF - 2026 - 998',
        )
        try:
            receive_view = PurchaseRequestViewSet.as_view({'post': 'receive'})
            delete_view = PurchaseRequestViewSet.as_view({'delete': 'destroy'})
            void_view = PurchaseRequestViewSet.as_view({'post': 'void_prf'})

            rx_resp = receive_view(
                factory.post(f'/api/purchase-requests/{req_receive.id}/receive/', {
                    'qty': 10,
                    'date': '2026-11-15',
                    'note': 'First box',
                }, format='json'),
                pk=str(req_receive.id),
            )
            self.assertEqual(rx_resp.status_code, 200)

            req_receive.refresh_from_db()
            med.refresh_from_db()
            self.assertEqual(req_receive.receivedQty, 10)
            self.assertEqual(req_receive.status, 'Partial')
            self.assertEqual(med.stock, 20)
            self.assertEqual(med.status, 'Normal')
            self.assertEqual(PurchaseHistory.objects.filter(request=req_receive, qty=10).count(), 1)
            self.assertEqual(StockHistory.objects.filter(medicine=med, type='add', qty=10).count(), 1)

            # Received items cannot be hard-deleted or voided
            del_resp = delete_view(factory.delete(f'/api/purchase-requests/{req_receive.id}/'), pk=str(req_receive.id))
            self.assertEqual(del_resp.status_code, 400)

            void_bad = void_view(factory.post('/api/purchase-requests/void-prf/', {
                'ids': [str(req_receive.id)],
                'reason': 'Duplicate',
            }, format='json'))
            self.assertEqual(void_bad.status_code, 400)

            # Untouched Pending PRF can be voided cleanly while preserving PRF history
            void_ok = void_view(factory.post('/api/purchase-requests/void-prf/', {
                'ids': [str(req_void.id)],
                'prfNo': 'PRF - 2026 - 998',
                'reason': 'Wrong vendor',
                'staffName': 'Nurse Joy',
            }, format='json'))
            self.assertEqual(void_ok.status_code, 200)
            req_void.refresh_from_db()
            self.assertEqual(req_void.status, 'Voided')
            self.assertTrue(PurchaseHistory.objects.filter(request=req_void, note__icontains='Voided PRF').exists())

            # Voided PRF rejects future deliveries
            rx_voided = receive_view(
                factory.post(f'/api/purchase-requests/{req_void.id}/receive/', {'qty': 5}, format='json'),
                pk=str(req_void.id),
            )
            self.assertEqual(rx_voided.status_code, 400)
        finally:
            PurchaseHistory.objects.filter(request__in=[req_receive, req_void]).delete()
            PurchaseRequest.objects.filter(id__in=[req_receive.id, req_void.id]).delete()
            StockHistory.objects.filter(medicine=med).delete()
            MedicineItem.objects.filter(id=med.id).delete()

    def test_medcert_fields_are_encrypted_at_rest_and_transparent_via_api(self):
        import uuid
        from datetime import date
        from django.db import connection
        from rest_framework.test import APIRequestFactory
        from api.models import AppNotification, MedicalCertificate, Patient
        from api.views import MedicalCertificateViewSet

        factory = APIRequestFactory()
        patient = Patient.objects.create(
            id='__TEST_MC_ENC__',
            name='JUAN DELA CRUZ',
            category='Student',
            contact='09170000000',
            birthday=date(2004, 1, 15),
            age=22,
        )
        legacy_id = uuid.uuid4()
        try:
            create_view = MedicalCertificateViewSet.as_view({'post': 'create'})
            payload = {
                'patient': patient.id,
                'date': '2026-11-15',
                'purpose': 'Class Excuse',
                'diagnosis': 'Acute Tonsillitis',
                'recommendation': 'Rest for 2 days',
                'doctor': 'Dr. Santos',
                'issuedBy': 'UA CLINIC',
                'notes': 'Temp 38.5C',
            }
            resp = create_view(factory.post('/api/certificates/', payload, format='json'))
            self.assertEqual(resp.status_code, 201)
            self.assertEqual(resp.data['diagnosis'], 'Acute Tonsillitis')
            self.assertEqual(resp.data['recommendation'], 'Rest for 2 days')
            self.assertEqual(resp.data['notes'], 'Temp 38.5C')

            cert_id = resp.data['id']

            # Duplicate check must still match encrypted diagnosis cleanly
            dup_resp = create_view(factory.post('/api/certificates/', payload, format='json'))
            self.assertEqual(dup_resp.status_code, 200)
            self.assertEqual(dup_resp.data['id'], cert_id)

            # Raw SQL row at rest must contain ciphertext, never plaintext
            table = MedicalCertificate._meta.db_table
            with connection.cursor() as cursor:
                cursor.execute(
                    f'SELECT diagnosis, recommendation, notes FROM "{table}" WHERE id = %s',
                    [str(cert_id).replace('-', '') if connection.vendor == 'sqlite' else str(cert_id)],
                )
                raw_diag, raw_rec, raw_notes = cursor.fetchone()
            self.assertTrue(raw_diag.startswith('enc:v1:'))
            self.assertNotIn('Acute Tonsillitis', raw_diag)
            self.assertTrue(raw_rec.startswith('enc:v1:'))
            self.assertNotIn('Rest for 2 days', raw_rec)
            self.assertTrue(raw_notes.startswith('enc:v1:'))

            # ORM & API transparently decrypt back to plaintext
            loaded = MedicalCertificate.objects.get(pk=cert_id)
            self.assertEqual(loaded.diagnosis, 'Acute Tonsillitis')
            self.assertEqual(loaded.recommendation, 'Rest for 2 days')
            self.assertEqual(loaded.notes, 'Temp 38.5C')

            # Legacy unencrypted rows remain readable without migration errors
            with connection.cursor() as cursor:
                cursor.execute(
                    f'INSERT INTO "{table}" (id, patient_id, date, purpose, diagnosis, recommendation, notes) '
                    'VALUES (%s, %s, %s, %s, %s, %s, %s)',
                    [
                        legacy_id.hex if connection.vendor == 'sqlite' else str(legacy_id),
                        patient.id,
                        '2026-10-01',
                        'Legacy Clearance',
                        'Migraine',
                        'Hydrate',
                        'Legacy plain row',
                    ],
                )
            legacy_loaded = MedicalCertificate.objects.get(pk=legacy_id)
            self.assertEqual(legacy_loaded.diagnosis, 'Migraine')
            self.assertEqual(legacy_loaded.notes, 'Legacy plain row')

            # Tampered ciphertext fails integrity check cleanly instead of returning forged data
            with connection.cursor() as cursor:
                cursor.execute(
                    f'UPDATE "{table}" SET diagnosis = %s WHERE id = %s',
                    ['enc:v1:tampered_invalid_ciphertext', str(cert_id).replace('-', '') if connection.vendor == 'sqlite' else str(cert_id)],
                )
            tampered = MedicalCertificate.objects.get(pk=cert_id)
            self.assertEqual(tampered.diagnosis, '[Encrypted]')
        finally:
            AppNotification.objects.filter(patient_id=patient.id).delete()
            MedicalCertificate.objects.filter(patient=patient).delete()
            Patient.objects.filter(id=patient.id).delete()





