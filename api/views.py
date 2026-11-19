import os
import uuid
from decouple import config
from django.http import JsonResponse
from django.contrib.auth.models import User
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from .auth_utils import (
    USER_MGMT_SENTINEL,
    IsClinicStaff,
    IsStaffOrReadOnly,
    get_env_mgmt_passwords,
    get_user_patients,
    hash_staff_pin,
    is_main_cura_login_password,
    is_staff_account,
    is_valid_mgmt_password,
    mint_tokens_for_user,
    populate_token_claims,
    set_refresh_cookie,
    verify_refresh_token_signature,
    verify_staff_pin,
)
from django.conf import settings
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated, AllowAny, SAFE_METHODS
from rest_framework.decorators import action, api_view, permission_classes as drf_permission_classes
from google.oauth2 import id_token
from google.auth.transport import requests
from django.db.models import Exists, OuterRef

from .models import (
    Patient, Consultation, Treatment, MedicineItem, StockHistory,
    PurchaseRequest, PurchaseHistory, MedicalCertificate,
    Bed, BedHistory, HospitalTransfer, AppNotification, OTPVerification,
    LoginAttempt
)
from .serializers import (
    PatientSerializer, ConsultationSerializer, TreatmentSerializer,
    MedicineItemSerializer, StockHistorySerializer, PurchaseRequestSerializer,
    PurchaseHistorySerializer, MedicalCertificateSerializer, BedSerializer,
    BedHistorySerializer, HospitalTransferSerializer, AppNotificationSerializer
)

import random
import secrets
from django.utils import timezone
from django.db import transaction
from datetime import timedelta

_GOOGLE_AUTH_REQ = requests.Request()


def _rekey_patient_safely(patient: Patient, new_id: str) -> Patient:
    """Safely changes a Patient's primary key without cascade-deleting related clinical history."""
    clean_new_id = str(new_id or '').strip()
    if not clean_new_id or clean_new_id == patient.id:
        return patient
    old_id = patient.id
    with transaction.atomic():
        existing_target = Patient.objects.select_for_update().filter(id=clean_new_id).first()
        if existing_target and existing_target.id != old_id:
            target_id = existing_target.id
        else:
            patient.pk = clean_new_id
            patient.id = clean_new_id
            patient.save()
            target_id = clean_new_id

        from .models import (
            TelemedicineRequest, AppointmentRequest, PatientQueue, MedicalCertificateRequest
        )
        Consultation.objects.filter(patient_id=old_id).update(patient_id=target_id)
        MedicalCertificate.objects.filter(patient_id=old_id).update(patient_id=target_id)
        HospitalTransfer.objects.filter(patient_id=old_id).update(patient_id=target_id)
        TelemedicineRequest.objects.filter(patient_id=old_id).update(patient_id=target_id)
        AppointmentRequest.objects.filter(patient_id=old_id).update(patient_id=target_id)
        PatientQueue.objects.filter(patient_id=old_id).update(patient_id=target_id)
        MedicalCertificateRequest.objects.filter(patient_id=old_id).update(patient_id=target_id)
        AppNotification.objects.filter(patient_id=str(old_id)).update(patient_id=str(target_id))
        Bed.objects.filter(patientId=str(old_id)).update(patientId=str(target_id))
        BedHistory.objects.filter(patientId=str(old_id)).update(patientId=str(target_id))
        Patient.objects.filter(id=old_id).delete()
        return Patient.objects.get(id=target_id)


class GoogleLoginView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        token = request.data.get('id_token')
        if not token:
            return Response({'error': 'id_token is required'}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            # Specify the CLIENT_ID of the app that accesses the backend:
            CLIENT_ID = "289437991360-4ge9cgmpvjmr68pfprsvfimau1i4batr.apps.googleusercontent.com"
            FIREBASE_PROJECT_ID = "cura-clinic-65b94"
            
            idinfo = None
            req = _GOOGLE_AUTH_REQ

            # 1. Try verifying as Google OAuth2 ID token
            try:
                idinfo = id_token.verify_oauth2_token(token, req, CLIENT_ID)
            except Exception as e1:
                # 2. Try verifying as Google OAuth2 ID token without audience constraint
                try:
                    idinfo = id_token.verify_oauth2_token(token, req)
                except Exception as e2:
                    # 3. Verify as cryptographic Firebase Auth ID token
                    try:
                        idinfo = id_token.verify_firebase_token(token, req, audience=FIREBASE_PROJECT_ID)
                    except Exception as e3:
                        try:
                            idinfo = id_token.verify_firebase_token(token, req)
                        except Exception as e4:
                            import logging
                            logging.getLogger(__name__).warning(
                                "Google OAuth verification failed: %s | %s | %s | %s", e1, e2, e3, e4
                            )
                            return Response({
                                'error': 'Invalid token',
                                'details': 'Google sign-in verification failed. Please try again.'
                            }, status=status.HTTP_401_UNAUTHORIZED)

            email = (idinfo.get('email') or '').strip().lower()
            if not email:
                return Response({'error': 'Email not found in authentication token'}, status=status.HTTP_400_BAD_REQUEST)

            first_name = idinfo.get('given_name') or ''
            last_name = idinfo.get('family_name') or ''
            full_name = f"{first_name} {last_name}".strip()
            if not full_name:
                full_name = (idinfo.get('name') or request.data.get('name') or email.split('@')[0]).upper()

            # Determine category based on email domain
            if email.endswith('.student@ua.edu.ph') or email.endswith('@student.ua.edu.ph') or '.student@' in email:
                category = 'Student'
            elif email.endswith('@ua.edu.ph'):
                category = 'Employee'
            else:
                category = 'Outsider'

            action = request.data.get('action') or request.data.get('mode') or 'signin'

            # Check existing records in clinic (Patient or User)
            patient = Patient.objects.filter(email__iexact=email).first()
            user = User.objects.filter(username__iexact=email).first() or User.objects.filter(email__iexact=email).first()

            # Case B: User is attempting to Register / Create Account
            if action in ['register', 'create_account']:
                if user:
                    return Response({
                        'error': 'This Google account is already registered. Please sign in.',
                        'code': 'ACCOUNT_ALREADY_EXISTS',
                        'already_exists': True,
                        'email': email,
                        'name': full_name,
                        'category': category,
                    }, status=status.HTTP_400_BAD_REQUEST)
                else:
                    with transaction.atomic():
                        OTPVerification.objects.filter(email__iexact=email).delete()
                        OTPVerification.objects.create(
                            email=email,
                            otp=str(secrets.randbelow(900000) + 100000),
                            expires_at=timezone.now() + timedelta(minutes=30),
                            is_verified=True,
                        )
                    patient_data = None
                    if patient:
                        patient_data = {
                            'id': patient.id,
                            'name': patient.name,
                            'category': patient.category,
                            'studentId': getattr(patient, 'studentId', '') or patient.id,
                            'course': getattr(patient, 'course', ''),
                            'yearLevel': getattr(patient, 'yearLevel', ''),
                        }
                    return Response({
                        'message': 'Google account verified. Please complete your registration form.',
                        'verified': True,
                        'is_new': True,
                        'email': email,
                        'name': full_name,
                        'first_name': first_name,
                        'last_name': last_name,
                        'category': patient.category if patient else category,
                        'patient_id': patient.id if patient else '',
                        'exists_in_clinic': bool(patient),
                        'patient': patient_data
                    }, status=status.HTTP_200_OK)

            # Case A: User is attempting to Sign In
            if not user:
                # If patient record already exists in clinic with this Google email, auto-claim & sign in seamlessly
                if patient:
                    user = User.objects.create_user(
                        username=email,
                        email=email,
                        first_name=first_name or patient.name,
                        last_name=last_name,
                    )
                else:
                    return Response({
                        'error': 'No account found for this Google account. Please create an account first.',
                        'code': 'ACCOUNT_NOT_FOUND',
                        'not_found': True,
                        'email': email,
                        'name': full_name,
                        'first_name': first_name,
                        'last_name': last_name,
                        'category': category,
                    }, status=status.HTTP_404_NOT_FOUND)

            # If registered user exists (for sign in flow):
            if not user.email:
                user.email = email
            if not user.first_name and (first_name or (patient and patient.name)):
                user.first_name = first_name or patient.name
            if not user.last_name and last_name:
                user.last_name = last_name
            user.save()

            if not patient:
                patient = Patient.objects.create(
                    id=uuid.uuid4().hex[:8],
                    name=full_name,
                    category=category,
                    contact='Not Provided',
                    birthday='2000-01-01',
                    age=0,
                    email=email
                )
            else:
                # Update category if upgraded (e.g. from Outsider to Student/Employee)
                if patient.category == 'Outsider' and category in ['Student', 'Employee']:
                    patient.category = category
                    patient.save()

            # Generate JWT tokens for the user
            refresh, roles = mint_tokens_for_user(user)

            return Response({
                'refresh': str(refresh),
                'access': str(refresh.access_token),
                'roles': roles,
                'username': user.username,
                'user': {
                    'email': user.email,
                    'first_name': user.first_name or full_name,
                    'last_name': user.last_name,
                    'name': patient.name if patient else full_name,
                    'is_new': False,
                    'category': patient.category,
                    'patient_id': patient.id,
                }
            }, status=status.HTTP_200_OK)

        except Exception as e:
            return Response({'error': 'Google authentication failed', 'details': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class CheckEmailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        return self.post(request)

    def post(self, request):
        params = request.data if request.data else request.query_params
        email = (params.get('email') or request.query_params.get('email') or '').strip().lower()
        patient_id = (
            params.get('patient_id') or 
            params.get('student_id') or 
            params.get('employee_id') or 
            params.get('outsider_id') or 
            params.get('id') or
            request.query_params.get('patient_id') or
            request.query_params.get('id') or ''
        ).strip()
        patient_name = (params.get('name') or request.query_params.get('name') or '').strip()

        if not email and not patient_id:
            return Response({'error': 'Email or ID is required'}, status=400)
        
        patient = None
        matched_by = None

        if email:
            patient = Patient.objects.filter(email__iexact=email).first()
            if patient:
                matched_by = 'email'

        if not patient and patient_id:
            patient = Patient.objects.filter(id__iexact=patient_id).first()
            if patient:
                matched_by = 'id'

        if not patient and patient_name and not patient_id:
            patient = Patient.objects.filter(name__iexact=patient_name.upper()).first()
            if patient:
                matched_by = 'name'

        claimed = False
        if patient:
            if patient.email:
                claimed = (
                    User.objects.filter(username__iexact=patient.email).exists() or 
                    User.objects.filter(email__iexact=patient.email).exists()
                )
            if not claimed:
                claimed = User.objects.filter(username__iexact=patient.id).exists()
        
        patient_data = None
        missing_fields = []
        if patient:
            raw_bday = str(patient.birthday) if patient.birthday else ''
            bday_valid = raw_bday and raw_bday != '2000-01-01'
            raw_contact = patient.contact or ''
            contact_valid = raw_contact and raw_contact != 'Not Provided'

            patient_data = {
                'id': patient.id,
                'name': patient.name,
                'category': patient.category,
                'contact': raw_contact if contact_valid else '',
                'birthday': raw_bday if bday_valid else '',
                'age': patient.age or 0,
                'sex': patient.sex or '',
                'studentCategory': patient.studentCategory or '',
                'gradeLevel': patient.gradeLevel or '',
                'guardianName': patient.guardianName or '',
                'course': patient.course or '',
                'yearLevel': patient.yearLevel or '',
                'position': patient.position or '',
                'department': patient.department or '',
                'address': patient.address or '',
                'emergencyContact': patient.emergencyContact or '',
                'emergencyPhone': patient.emergencyPhone or '',
                'avatar': patient.avatar or '',
            }

            if not contact_valid:
                missing_fields.append('contact')
            if not bday_valid:
                missing_fields.append('birthday')
            if not patient.sex:
                missing_fields.append('sex')
            if not patient.emergencyContact:
                missing_fields.append('emergencyContact')
            if not patient.emergencyPhone:
                missing_fields.append('emergencyPhone')
            if patient.category == 'Student':
                if not patient.studentCategory:
                    missing_fields.append('studentCategory')
                if patient.studentCategory == 'College':
                    if not patient.course:
                        missing_fields.append('course')
                    if not patient.yearLevel:
                        missing_fields.append('yearLevel')
                elif patient.studentCategory:
                    if not patient.gradeLevel:
                        missing_fields.append('gradeLevel')
            elif patient.category == 'Employee':
                if not patient.position:
                    missing_fields.append('position')
                if not patient.department:
                    missing_fields.append('department')
            elif patient.category == 'Outsider':
                if not patient.address:
                    missing_fields.append('address')

        return Response({
            'exists': bool(patient),
            'claimed': claimed,
            'matched_by': matched_by,
            'patient': patient_data,
            'missing_fields': missing_fields
        })

def _send_clinic_email(to_email: str, subject: str, text_body: str, html_body: str = '') -> bool:
    """Deliver transactional clinic email via Brevo -> Resend -> Gmail SMTP fallback."""
    import logging
    clean_to = (to_email or '').strip()
    if not clean_to:
        return False
    log = logging.getLogger(__name__)
    html_content = html_body or f"<p>{text_body}</p>"

    brevo_key = getattr(settings, 'BREVO_API_KEY', '')
    if brevo_key:
        try:
            import requests as http_requests
            sender_email = getattr(settings, 'BREVO_SENDER_EMAIL', 'curaclinic.ua@gmail.com')
            r = http_requests.post(
                'https://api.brevo.com/v3/smtp/email',
                headers={'api-key': brevo_key, 'Content-Type': 'application/json'},
                json={
                    'sender': {'name': 'CURA Clinic', 'email': sender_email},
                    'to': [{'email': clean_to}],
                    'subject': subject,
                    'htmlContent': html_content,
                },
                timeout=5,
            )
            if r.status_code in (200, 201):
                return True
        except Exception as e:
            log.warning(f"Brevo email API error: {e}")

    resend_key = getattr(settings, 'RESEND_API_KEY', '')
    if resend_key:
        try:
            import requests as http_requests
            r = http_requests.post(
                'https://api.resend.com/emails',
                headers={'Authorization': f'Bearer {resend_key}', 'Content-Type': 'application/json'},
                json={
                    'from': 'CURA Clinic <onboarding@resend.dev>',
                    'to': [clean_to],
                    'subject': subject,
                    'html': html_content,
                },
                timeout=5,
            )
            if r.status_code in (200, 201):
                return True
        except Exception as e:
            log.warning(f"Resend email API error: {e}")

    try:
        from django.core.mail import send_mail
        send_mail(subject, text_body, settings.DEFAULT_FROM_EMAIL, [clean_to], fail_silently=False, html_message=html_content)
        return True
    except Exception as mail_err:
        log.warning(f"SMTP send failed (Render port block): {mail_err}")
        return False


class RequestOTPView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        email = request.data.get('email', '').strip().lower()
        patient_id = (
            request.data.get('patient_id') or 
            request.data.get('student_id') or 
            request.data.get('employee_id') or 
            request.data.get('outsider_id') or 
            request.data.get('id') or ''
        ).strip()
        allow_new = request.data.get('allow_new', False) or request.data.get('is_google', False)
        if not email:
            return Response({'error': 'Email is required'}, status=400)
        
        # If user entered an ID or username instead of email, attempt to resolve their registered email
        if '@' not in email:
            found_user = User.objects.filter(username__iexact=email).first()
            if found_user and found_user.email:
                email = found_user.email.strip().lower()
            else:
                found_patient = Patient.objects.filter(id__iexact=email).first()
                if found_patient and found_patient.email:
                    email = found_patient.email.strip().lower()

        patient_exists = Patient.objects.filter(email__iexact=email).exists()
        if not patient_exists and patient_id:
            patient_exists = Patient.objects.filter(id__iexact=patient_id).exists()

        user_exists = User.objects.filter(username__iexact=email).exists() or User.objects.filter(email__iexact=email).exists()
        if not allow_new and not patient_exists and not user_exists:
            return Response({'error': 'Patient record not found.'}, status=404)
        
        otp = str(secrets.randbelow(900000) + 100000)
        with transaction.atomic():
            OTPVerification.objects.filter(email__iexact=email).delete()
            OTPVerification.objects.create(
                email=email,
                otp=otp,
                expires_at=timezone.now() + timedelta(minutes=10),
                is_verified=False
            )
        
        subject = f"CURA Clinic - Verification Code: {otp}"
        message = (
            f"Hello,\n\n"
            f"Your CURA Clinic verification code is: {otp}\n\n"
            f"This code will expire in 10 minutes.\n"
            f"If you did not request this verification code, please ignore this email.\n\n"
            f"— University of the Assumption Medical-Dental Clinic (CURA)"
        )
        html_message = f"""
        <div style="font-family: Arial, sans-serif; max-width: 500px; margin: 0 auto; padding: 24px; border: 1px solid #e2e8f0; border-radius: 16px; background-color: #ffffff;">
          <div style="text-align: center; margin-bottom: 20px;">
            <h2 style="color: #0284c7; margin: 0; font-size: 24px;">CURA Clinic</h2>
            <p style="color: #64748b; font-size: 13px; margin-top: 4px;">University of the Assumption Medical-Dental Clinic</p>
          </div>
          <p style="color: #334155; font-size: 15px; margin-bottom: 8px;">Hello,</p>
          <p style="color: #334155; font-size: 14px; line-height: 1.5; margin-bottom: 16px;">
            Your one-time verification code for CURA is:
          </p>
          <div style="text-align: center; margin: 24px 0;">
            <span style="display: inline-block; font-size: 32px; font-weight: bold; letter-spacing: 6px; color: #0284c7; background: #f0f9ff; padding: 12px 24px; border-radius: 12px; border: 1px dashed #38bdf8;">
              {otp}
            </span>
          </div>
          <p style="color: #64748b; font-size: 13px; line-height: 1.4;">
            This code will expire in <strong>10 minutes</strong>. Do not share this code with anyone.
          </p>
          <hr style="border: none; border-top: 1px solid #f1f5f9; margin: 24px 0;" />
          <p style="color: #94a3b8; font-size: 11px; text-align: center; margin: 0;">
            University of the Assumption · Unisite Subdivision, Del Pilar, City of San Fernando, Pampanga
          </p>
        </div>
        """
        if not _send_clinic_email(email, subject, message, html_message):
            return Response(
                {'error': 'Failed to send verification code email. Please try again later.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response({'message': 'OTP sent successfully', 'email': email}, status=status.HTTP_200_OK)

class VerifyOTPView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        email = request.data.get('email', '').strip().lower()
        otp = request.data.get('otp', '').strip()
        patient_id = (
            request.data.get('patient_id') or 
            request.data.get('student_id') or 
            request.data.get('employee_id') or 
            request.data.get('outsider_id') or 
            request.data.get('id') or ''
        ).strip()

        if not email or not otp:
            return Response({'error': 'Email and OTP are required'}, status=400)

        otp_attempt_key = f"otp:{email}"
        attempt, _ = LoginAttempt.objects.get_or_create(identifier=otp_attempt_key)
        if attempt.is_locked():
            return Response({
                'error': f'Too many failed attempts. Please wait {max(1, attempt.remaining_seconds())}s before trying again.'
            }, status=status.HTTP_429_TOO_MANY_REQUESTS)
        
        try:
            verification = OTPVerification.objects.filter(email__iexact=email, otp=otp, is_verified=False).latest('id')
            if timezone.now() > verification.expires_at:
                return Response({'error': 'OTP expired'}, status=400)
            
            if attempt.failed_attempts > 0 or attempt.locked_until:
                attempt.failed_attempts = 0
                attempt.locked_until = None
                attempt.save()

            # Check if user already exists
            user = User.objects.filter(username__iexact=email).first() or User.objects.filter(email__iexact=email).first()
            patient = Patient.objects.filter(email__iexact=email).first()
            if not patient and patient_id:
                candidate = Patient.objects.filter(id__iexact=patient_id).first()
                if candidate and (not candidate.email or candidate.email.strip().lower() == email):
                    patient = candidate

            patient_name = patient.name if patient else (user.first_name if user else email.split('@')[0])
            resolved_patient_id = patient.id if patient else (user.username if user else patient_id)

            res_payload = {
                'message': 'OTP verified',
                'is_verified': True,
                'claimed': bool(user),
            }

            # Mark verified (single-use for VerifyOTPView since query requires is_verified=False;
            # preserved until SetPasswordView consumes it for Forgot Password / Account Claim)
            verification.is_verified = True
            verification.save(update_fields=['is_verified'])

            if user:
                refresh, _ = mint_tokens_for_user(user)
                res_payload.update({
                    'refresh': str(refresh),
                    'access': str(refresh.access_token),
                    'user': {
                        'id': resolved_patient_id,
                        'email': user.email or email,
                        'name': patient_name,
                        'is_new': False,
                    }
                })

            return Response(res_payload)
        except OTPVerification.DoesNotExist:
            attempt.failed_attempts += 1
            if attempt.failed_attempts >= 5:
                attempt.locked_until = timezone.now() + timedelta(minutes=5)
            attempt.save()
            return Response({'error': 'Invalid OTP'}, status=400)

def _record_privacy_consent(request_data, patient_id, subject_name, channel='Mobile App'):
    if not request_data.get('privacy_accepted'):
        return
    version = str(request_data.get('privacy_policy_version') or '2026-06')[:32]
    try:
        from .models import AuditLog
        AuditLog.objects.create(
            staff=None,
            staff_name_snapshot=(subject_name or patient_id or 'PATIENT')[:150],
            staff_role_snapshot='Patient',
            action='PRIVACY_CONSENT_ACCEPTED',
            target_type='Patient',
            target_id=str(patient_id or '')[:120],
            details=f'Accepted RA 10173 Data Privacy Policy (v{version}) via {channel}',
        )
    except Exception:
        pass


class RegisterView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        email = request.data.get('email')
        password = request.data.get('password')
        role = request.data.get('role', 'Outsider')
        id_number = request.data.get('id_number')
        
        if not email or not password:
            return Response({'error': 'Email and password are required'}, status=400)
            
        clean_email = email.strip().lower()
        if User.objects.filter(username__iexact=clean_email).exists() or User.objects.filter(email__iexact=clean_email).exists():
            return Response({'error': 'User already exists'}, status=400)
            
        # Create User
        user = User.objects.create_user(username=clean_email, email=clean_email, password=password)
        
        # Auto-infer category based on email
        if clean_email.endswith('.student@ua.edu.ph') or clean_email.endswith('@student.ua.edu.ph') or '.student@' in clean_email:
            category = 'Student'
        elif clean_email.endswith('@ua.edu.ph'):
            category = 'Employee'
        else:
            category = role or 'Outsider'

        name_prefix = (request.data.get('name') or (clean_email.split('@')[0] if '@' in clean_email else clean_email)).upper()
        
        patient = Patient.objects.filter(email__iexact=clean_email).first()
        if not patient and id_number:
            candidate = Patient.objects.filter(id__iexact=str(id_number).strip()).first()
            if candidate:
                if candidate.email and candidate.email.strip().lower() != clean_email:
                    return Response({'error': 'This ID belongs to another registered account.'}, status=403)
                patient = candidate

        if not patient:
            patient = Patient.objects.create(
                id=id_number or uuid.uuid4().hex[:8],
                name=name_prefix,
                category=category,
                contact='Not Provided',
                birthday='2000-01-01',
                age=0,
                email=clean_email
            )
        else:
            if not patient.email:
                patient.email = clean_email
            if name_prefix and (patient.name == 'Not Provided' or not patient.name):
                patient.name = name_prefix
            if category:
                patient.category = category
            patient.save()
            if id_number and patient.id != id_number and not Patient.objects.filter(id=id_number).exists():
                patient = _rekey_patient_safely(patient, id_number)
        
        # Return JWT token
        refresh = RefreshToken.for_user(user)
        refresh['username'] = user.username
        refresh['roles'] = list(user.groups.values_list('name', flat=True))
        _record_privacy_consent(request.data, patient.id, patient.name, 'Mobile Registration')
        
        return Response({
            'refresh': str(refresh),
            'access': str(refresh.access_token),
            'user': {'id': patient.id, 'email': user.email, 'name': patient.name or name_prefix, 'is_new': True}
        }, status=status.HTTP_201_CREATED)

from rest_framework.permissions import IsAuthenticated

class CompleteProfileView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        data = request.data
        user_email = (user.email or user.username or '').strip().lower()
        
        try:
            patient = Patient.objects.filter(email__iexact=user_email).first()
            if not patient and user.username:
                patient = Patient.objects.filter(email__iexact=user.username.strip()).first()
            
            patient_id = data.get('id') or data.get('student_id') or data.get('employee_id')
            if not patient and patient_id:
                candidate = Patient.objects.filter(id__iexact=str(patient_id).strip()).first()
                if candidate:
                    if candidate.email and candidate.email.strip().lower() != user_email:
                        return Response({'error': 'This ID is already linked to another account.'}, status=403)
                    candidate.email = user.email or user.username
                    candidate.save()
                    patient = candidate

            if not patient:
                # Auto-create patient record if missing
                patient = Patient.objects.create(
                    id=uuid.uuid4().hex[:8],
                    name=(user.first_name or user.username or 'User').strip().upper(),
                    category='Outsider',
                    contact='Not Provided',
                    birthday='2000-01-01',
                    age=0,
                    email=user.email or user.username
                )
        except Exception as e:
            return Response({'error': f'Patient lookup error: {str(e)}'}, status=500)

        # Handle ID (primary key) change if a new ID was provided (e.g. STU-2026-001)
        new_id = str(data.get('id') or '').strip()
        if new_id and new_id != patient.id:
            existing_target = Patient.objects.filter(id=new_id).first()
            if existing_target and existing_target.id != patient.id:
                if existing_target.email and existing_target.email.strip().lower() != user_email:
                    return Response({'error': 'This ID belongs to another registered account.'}, status=403)
                existing_target.email = user.email or user.username
                existing_target.save()
                patient = _rekey_patient_safely(patient, existing_target.id)
            else:
                patient = _rekey_patient_safely(patient, new_id)

        # Update other fields
        fields_to_update = [
            'name', 'category', 'contact', 'birthday', 'age', 'sex', 
            'emergencyContact', 'emergencyPhone', 'course', 'yearLevel', 
            'position', 'department', 'address', 'studentCategory', 
            'guardianName', 'gradeLevel'
        ]
        
        for field in fields_to_update:
            if field in data:
                val = data[field]
                if field in ['name', 'guardianName', 'emergencyContact'] and isinstance(val, str):
                    val = val.strip().upper()
                setattr(patient, field, val)
                
        # Also update the user's name if provided
        if 'name' in data and data['name']:
            user.first_name = str(data['name']).strip().upper()
            user.save()

        patient.save()

        # Update user's is_new status so they don't see onboarding again
        return Response({
            'message': 'Profile completed successfully.',
            'patient_id': patient.id,
            'user': {'id': patient.id, 'email': user.email, 'name': patient.name, 'is_new': False}
        })

class SetPasswordView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        email = request.data.get('email', '').strip().lower()
        password = request.data.get('password')
        patient_id = (
            request.data.get('patient_id') or 
            request.data.get('student_id') or 
            request.data.get('employee_id') or 
            request.data.get('outsider_id') or 
            request.data.get('id') or ''
        ).strip()
        
        try:
            verification = OTPVerification.objects.filter(email__iexact=email, is_verified=True).latest('id')
            if timezone.now() > verification.expires_at:
                return Response({'error': 'Session expired'}, status=400)
            
            user = User.objects.filter(email__iexact=email).first() or User.objects.filter(username__iexact=email).first()
            if user and is_staff_account(user):
                return Response(
                    {'error': 'Clinic staff accounts must change passwords via Clinic Settings.'},
                    status=status.HTTP_403_FORBIDDEN,
                )
            if not user:
                user = User.objects.create_user(username=email, email=email)
            user.set_password(password)
            user.save()
            
            verification.delete() # Cleanup
            
            refresh, _ = mint_tokens_for_user(user)

            patient = Patient.objects.filter(email__iexact=email).first()
            if not patient and patient_id:
                candidate = Patient.objects.filter(id__iexact=patient_id).first()
                if candidate and (not candidate.email or candidate.email.strip().lower() == email):
                    patient = candidate

            if patient:
                # Claim the web patient record by attaching user email only if unclaimed
                if not patient.email:
                    patient.email = email
                    patient.save()

            patient_name = patient.name if patient else email.split('@')[0]
            resolved_patient_id = patient.id if patient else user.username
            _record_privacy_consent(request.data, resolved_patient_id, patient_name, 'Mobile Account Claim')
            return Response({
                'refresh': str(refresh),
                'access': str(refresh.access_token),
                'user': {'id': resolved_patient_id, 'email': user.email, 'name': patient_name, 'is_new': False}
            })
            
        except OTPVerification.DoesNotExist:
            return Response({'error': 'Email not verified or session expired'}, status=400)

class ChangePasswordView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        email = request.data.get('email', '').strip()
        current_password = request.data.get('current_password') or ''
        new_password = request.data.get('new_password') or ''

        if not email or not new_password:
            return Response({'error': 'Email and new password are required.'}, status=status.HTTP_400_BAD_REQUEST)
        if len(new_password) < 6:
            return Response({'error': 'New password must be at least 6 characters.'}, status=status.HTTP_400_BAD_REQUEST)

        user = User.objects.filter(email__iexact=email).first() or User.objects.filter(username__iexact=email).first()
        if not user:
            return Response({'error': 'User account not found.'}, status=status.HTTP_404_NOT_FOUND)

        staff_account = is_staff_account(user)
        if staff_account:
            admin_password = (request.data.get('admin_password') or '').strip()
            if not current_password or not user.check_password(current_password):
                return Response({'error': 'Current password is incorrect or missing.'}, status=status.HTTP_400_BAD_REQUEST)
            if not is_valid_mgmt_password(admin_password):
                return Response(
                    {'error': 'Valid User Management Master Key is required to change the CURA login password.'},
                    status=status.HTTP_403_FORBIDDEN,
                )
        else:
            is_self_authenticated = bool(
                request.user
                and request.user.is_authenticated
                and (
                    request.user.is_superuser
                    or request.user.pk == user.pk
                    or (request.user.email or '').strip().lower() == email.lower()
                )
            )

            if not is_self_authenticated:
                if not current_password or not user.check_password(current_password):
                    return Response({'error': 'Current password is incorrect or missing.'}, status=status.HTTP_400_BAD_REQUEST)
            elif current_password and not user.check_password(current_password):
                return Response({'error': 'Current password is incorrect.'}, status=status.HTTP_400_BAD_REQUEST)

        user.set_password(new_password)
        user.save()

        payload = {'success': True, 'message': 'Password updated successfully.'}
        if isinstance(user, User) and (staff_account or is_self_authenticated or current_password):
            refresh, _ = mint_tokens_for_user(user)
            payload['access'] = str(refresh.access_token)
            response = Response(payload, status=status.HTTP_200_OK)
            if staff_account:
                set_refresh_cookie(response, str(refresh))
            return response
        return Response(payload, status=status.HTTP_200_OK)

class DeleteAccountView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        return self.delete(request)

    def delete(self, request):
        user = request.user
        if not user or not user.is_authenticated:
            return Response({'error': 'Authentication required.'}, status=status.HTTP_401_UNAUTHORIZED)
        if user.is_staff or user.is_superuser:
            return Response({'error': 'Clinic admin accounts cannot be deleted from mobile.'}, status=status.HTTP_403_FORBIDDEN)
        email = (user.email or user.username or '').strip()
        try:
            from .models import AuditLog
            AuditLog.objects.create(
                staff=None,
                staff_name_snapshot=email[:150] or 'PATIENT',
                staff_role_snapshot='Patient',
                action='ACCOUNT_DELETED',
                target_type='User',
                target_id=str(user.pk),
                details=f'Mobile user self-deleted account ({email})',
            )
        except Exception:
            pass
        user.delete()
        return Response({'success': True, 'message': 'Account and associated personal data deleted.'}, status=status.HTTP_200_OK)


def health(request):
    return JsonResponse({
        "status": "ok",
        "message": "CURA Backend Running"
    })

class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    default_error_messages = {
        'no_active_account': 'Incorrect username or password. Please verify your credentials and try again.'
    }

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        populate_token_claims(token, user)
        return token

    def validate(self, attrs):
        from rest_framework.exceptions import AuthenticationFailed
        username_or_email = attrs.get(self.username_field)
        normalized_identifier = str(username_or_email or '').strip().lower()

        if username_or_email:
            username_or_email = str(username_or_email).strip()
            from django.db.models import Q
            matched_user = User.objects.filter(
                Q(username__iexact=username_or_email) | Q(email__iexact=username_or_email)
            ).first()
            if matched_user:
                attrs[self.username_field] = matched_user.username
                normalized_identifier = matched_user.username.lower()

        MAX_ATTEMPTS = 5
        LOCKOUT_MINUTES = 1
        now = timezone.now()

        login_attempt = None
        if normalized_identifier:
            try:
                login_attempt, _ = LoginAttempt.objects.get_or_create(identifier=normalized_identifier)
                if login_attempt.locked_until:
                    if now < login_attempt.locked_until:
                        rem_seconds = int((login_attempt.locked_until - now).total_seconds())
                        time_str = f"{rem_seconds} second{'s' if rem_seconds != 1 else ''}" if rem_seconds < 60 else f"{max(1, (rem_seconds + 59) // 60)} minute{'s' if rem_seconds >= 120 else ''}"
                        raise AuthenticationFailed({
                            'detail': "Account temporarily locked.",
                            'attempts_remaining': 0,
                            'max_attempts': MAX_ATTEMPTS,
                            'is_locked': True,
                            'lockout_seconds': rem_seconds,
                        })
                    else:
                        login_attempt.failed_attempts = 0
                        login_attempt.locked_until = None
                        login_attempt.save()
                elif login_attempt.failed_attempts > 0 and login_attempt.last_attempt_at:
                    if (now - login_attempt.last_attempt_at) > timedelta(minutes=15):
                        login_attempt.failed_attempts = 0
                        login_attempt.save()
            except AuthenticationFailed:
                raise
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(f"LoginAttempt check error: {e}")

        # Attempt authentication via SimpleJWT
        try:
            data = super().validate(attrs)
        except Exception as exc:
            # Re-raise if already an explicit lockout exception
            if isinstance(exc, AuthenticationFailed) and isinstance(exc.detail, dict):
                if str(exc.detail.get('is_locked', '')).lower() in ('true', '1'):
                    raise

            if normalized_identifier:
                try:
                    if not login_attempt:
                        login_attempt, _ = LoginAttempt.objects.get_or_create(identifier=normalized_identifier)

                    if login_attempt.failed_attempts > 0 and login_attempt.last_attempt_at:
                        if (now - login_attempt.last_attempt_at) > timedelta(minutes=15):
                            login_attempt.failed_attempts = 0

                    login_attempt.failed_attempts += 1
                    if login_attempt.failed_attempts >= MAX_ATTEMPTS:
                        login_attempt.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
                        login_attempt.save()
                        raise AuthenticationFailed({
                            'detail': "Account temporarily locked.",
                            'attempts_remaining': 0,
                            'max_attempts': MAX_ATTEMPTS,
                            'is_locked': True,
                            'lockout_seconds': LOCKOUT_MINUTES * 60,
                        })
                    else:
                        remaining = max(1, MAX_ATTEMPTS - login_attempt.failed_attempts)
                        login_attempt.save()
                        msg = "Incorrect username or password. Please verify your credentials and try again."
                        raise AuthenticationFailed({
                            'detail': msg,
                            'attempts_remaining': remaining,
                            'max_attempts': MAX_ATTEMPTS,
                            'is_locked': False,
                        })
                except AuthenticationFailed:
                    raise
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).warning(f"LoginAttempt record error: {e}")

            raise AuthenticationFailed({
                'detail': "Incorrect username or password. Please verify your credentials and try again.",
                'attempts_remaining': 4,
                'max_attempts': MAX_ATTEMPTS,
                'is_locked': False,
            })

        # Successful login: reset failed attempts
        if login_attempt:
            try:
                if login_attempt.failed_attempts > 0 or login_attempt.locked_until:
                    login_attempt.failed_attempts = 0
                    login_attempt.locked_until = None
                    login_attempt.save()
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(f"LoginAttempt reset error: {e}")

        roles = list(self.user.groups.values_list('name', flat=True))
        if self.user.is_superuser:
            roles.append('Admin')
        data['roles'] = roles
        data['username'] = self.user.username

        patient = None
        if not (self.user.is_staff or self.user.is_superuser):
            patient = Patient.objects.filter(email__iexact=self.user.email).first() or Patient.objects.filter(id__iexact=self.user.username).first()
        data['user'] = {
            'id': patient.id if patient else self.user.username,
            'name': patient.name if patient else (self.user.first_name or self.user.username),
            'email': self.user.email or self.user.username,
            'category': patient.category if patient else 'Outsider'
        }
        return data

class CustomTokenObtainPairView(TokenObtainPairView):
    authentication_classes = []
    permission_classes = [AllowAny]
    serializer_class = CustomTokenObtainPairSerializer

    def handle_exception(self, exc):
        from rest_framework.exceptions import AuthenticationFailed
        if isinstance(exc, AuthenticationFailed):
            if isinstance(exc.detail, dict):
                raw_locked = str(exc.detail.get('is_locked', '')).lower()
                is_locked = raw_locked in ('true', '1')

                try:
                    rem = int(exc.detail.get('attempts_remaining', 0))
                except (ValueError, TypeError):
                    rem = 0

                try:
                    max_att = int(exc.detail.get('max_attempts', 5))
                except (ValueError, TypeError):
                    max_att = 5

                resp_payload = {
                    'detail': str(exc.detail.get('detail', 'Incorrect username or password.')),
                    'attempts_remaining': rem,
                    'max_attempts': max_att,
                    'is_locked': is_locked,
                }
                if 'lockout_seconds' in exc.detail:
                    try:
                        resp_payload['lockout_seconds'] = int(exc.detail['lockout_seconds'])
                    except (ValueError, TypeError):
                        pass

                status_code = status.HTTP_429_TOO_MANY_REQUESTS if is_locked else status.HTTP_401_UNAUTHORIZED
                return Response(resp_payload, status=status_code)

            elif isinstance(exc.detail, str):
                return Response({
                    'detail': "Incorrect username or password. Please verify your credentials and try again.",
                    'attempts_remaining': 4,
                    'max_attempts': 5,
                    'is_locked': False,
                }, status=status.HTTP_401_UNAUTHORIZED)

        return super().handle_exception(exc)

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        if response.status_code == 200:
            refresh_token = response.data.get('refresh')

            if refresh_token:
                set_refresh_cookie(response, refresh_token)
                # Do not return the refresh token in the JSON response
                del response.data['refresh']

        return response

class CustomTokenRefreshView(TokenRefreshView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs):
        cookie_refresh = request.COOKIES.get(settings.SIMPLE_JWT['AUTH_COOKIE'])
        body_refresh = request.data.get('refresh') if hasattr(request.data, 'get') else None
        refresh_token = cookie_refresh or body_refresh

        if refresh_token and not verify_refresh_token_signature(refresh_token):
            response = Response(
                {'detail': 'Session expired due to password change. Please log in again.', 'code': 'token_not_valid'},
                status=status.HTTP_401_UNAUTHORIZED,
            )
            response.delete_cookie(settings.SIMPLE_JWT['AUTH_COOKIE'])
            return response

        if refresh_token and not body_refresh:
            if hasattr(request.data, '_mutable'):
                request.data._mutable = True
                request.data['refresh'] = refresh_token
            elif isinstance(request.data, dict):
                request.data['refresh'] = refresh_token

        response = super().post(request, *args, **kwargs)

        if response.status_code == 200:
            new_refresh = response.data.get('refresh') or refresh_token
            if new_refresh:
                set_refresh_cookie(response, new_refresh)
                if cookie_refresh and not body_refresh and 'refresh' in response.data:
                    del response.data['refresh']
                elif body_refresh and 'refresh' not in response.data:
                    response.data['refresh'] = new_refresh

        return response

class LogoutView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs):
        response = Response({"detail": "Successfully logged out."}, status=status.HTTP_200_OK)
        response.delete_cookie(settings.SIMPLE_JWT['AUTH_COOKIE'])
        return response


class PatientScopedMixin:
    """Restrict non-staff users to their own linked Patient rows and block cross-patient writes."""
    patient_lookup_field = 'patient'
    allow_patient_create = False
    allow_patient_update = False

    def get_permissions(self):
        if not self.allow_patient_create and getattr(self, 'action', None) in ('create', 'destroy'):
            return [IsClinicStaff()]
        if not self.allow_patient_update and getattr(self, 'action', None) in ('update', 'partial_update'):
            return [IsClinicStaff()]
        return [IsAuthenticated()]

    def _get_owned_patient_ids(self, user):
        return list(get_user_patients(user).values_list('id', flat=True))

    def _assert_owned_patient_id(self, request, raw_patient_id):
        if is_staff_account(request.user):
            return
        own_ids = {str(pid).strip().lower() for pid in self._get_owned_patient_ids(request.user)}
        target = str(raw_patient_id or '').strip().lower()
        if not target or target not in own_ids:
            raise PermissionDenied('You can only access or modify your own patient record.')

    def filter_queryset_for_user(self, qs):
        user = getattr(self.request, 'user', None)
        if is_staff_account(user):
            return qs
        own_ids = self._get_owned_patient_ids(user)
        if not own_ids:
            return qs.none()
        return qs.filter(**{f'{self.patient_lookup_field}__in': own_ids})


# --- Phase 4 ViewSets ---

class PatientViewSet(PatientScopedMixin, viewsets.ModelViewSet):
    queryset = Patient.objects.all()
    serializer_class = PatientSerializer
    patient_lookup_field = 'id'
    allow_patient_update = True

    def get_queryset(self):
        qs = Patient.objects.annotate(
            is_claimed=Exists(User.objects.filter(username__iexact=OuterRef('email')))
        )
        qs = self.filter_queryset_for_user(qs)
        email = self.request.query_params.get('email')
        if email:
            return qs.filter(email__iexact=email.strip())
        return qs

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        name = serializer.validated_data.get('name', '').strip().upper()
        birthday = serializer.validated_data.get('birthday')
        if name and birthday:
            existing = Patient.objects.filter(name__iexact=name, birthday=birthday).first()
            if existing:
                return Response(self.get_serializer(existing).data, status=status.HTTP_200_OK)

        return super().create(request, *args, **kwargs)

    @action(detail=False, methods=['patch', 'post'], url_path='update-by-email')
    def update_by_email(self, request):
        email = request.data.get('email', '').strip()
        if not email:
            return Response({'error': 'Email required'}, status=status.HTTP_400_BAD_REQUEST)
        if not is_staff_account(request.user):
            user_email = (getattr(request.user, 'email', '') or '').strip().lower()
            user_name = (getattr(request.user, 'username', '') or '').strip().lower()
            if email.lower() not in (user_email, user_name):
                return Response({'error': 'Forbidden'}, status=status.HTTP_403_FORBIDDEN)
        patient = Patient.objects.filter(email__iexact=email).first()
        if not patient:
            return Response({'error': 'Patient not found'}, status=status.HTTP_404_NOT_FOUND)
        serializer = self.get_serializer(patient, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_200_OK)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class ConsultationViewSet(PatientScopedMixin, viewsets.ModelViewSet):
    queryset = Consultation.objects.prefetch_related('treatments').all().order_by('-date', '-timeIn')
    serializer_class = ConsultationSerializer

    def get_queryset(self):
        qs = Consultation.objects.prefetch_related('treatments').order_by('-date', '-timeIn')
        qs = self.filter_queryset_for_user(qs)
        patient_id = self.request.query_params.get('patient')
        if patient_id:
            return qs.filter(patient_id=patient_id)
        return qs

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        if not serializer.is_valid():
            import sys
            sys.stderr.write(f"Validation Error: {serializer.errors}\n")
            sys.stderr.flush()
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        # Idempotency / Duplicate prevention
        patient = serializer.validated_data.get('patient')
        date = serializer.validated_data.get('date')
        time_in = serializer.validated_data.get('timeIn')
        complaint = serializer.validated_data.get('complaint', '')
        stat = serializer.validated_data.get('status')

        existing = Consultation.objects.filter(
            patient=patient,
            date=date,
            timeIn=time_in,
            complaint=complaint,
            status=stat
        ).first()

        if existing:
            return Response(self.get_serializer(existing).data, status=status.HTTP_200_OK)

        return super().create(request, *args, **kwargs)

class TreatmentViewSet(PatientScopedMixin, viewsets.ModelViewSet):
    queryset = Treatment.objects.all()
    serializer_class = TreatmentSerializer
    patient_lookup_field = 'consultation__patient'
    allow_patient_update = True

    def get_queryset(self):
        return self.filter_queryset_for_user(Treatment.objects.all())

def _apply_medicine_stock_delta(med, qty, tx_type, note):
    if tx_type == 'add':
        med.stock = max(0, (med.stock or 0)) + qty
        if med.beginningQty is None:
            med.beginningQty = med.stock
    else:
        actual_dispensed = min(max(0, med.stock or 0), qty)
        med.stock = max(0, (med.stock or 0)) - qty
        med.dispensed = (med.dispensed or 0) + (actual_dispensed if actual_dispensed > 0 else qty)

    threshold = med.threshold if med.threshold is not None else 15
    if med.stock <= 0:
        med.status = 'Out of Stock'
    elif med.stock <= threshold:
        med.status = 'Low Stock'
    else:
        med.status = 'Normal'

    med.save()
    StockHistory.objects.create(
        medicine=med,
        qty=qty,
        type=tx_type,
        note=note or ('Stock added' if tx_type == 'add' else 'Dispensed'),
    )


class MedicineItemViewSet(viewsets.ModelViewSet):
    queryset = MedicineItem.objects.prefetch_related('stockHistory').all()
    serializer_class = MedicineItemSerializer
    permission_classes = [IsClinicStaff]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        name = serializer.validated_data.get('name', '').strip()
        if name:
            existing = MedicineItem.objects.filter(name__iexact=name).first()
            if existing:
                return Response(self.get_serializer(existing).data, status=status.HTTP_200_OK)
        return super().create(request, *args, **kwargs)

    @action(detail=True, methods=['patch', 'post'], url_path='stock')
    def update_stock(self, request, pk=None):
        try:
            qty = int(request.data.get('qty', 0))
        except (ValueError, TypeError):
            return Response({'error': 'Valid integer qty is required.'}, status=status.HTTP_400_BAD_REQUEST)
        if qty <= 0:
            return Response({'error': 'qty must be greater than 0.'}, status=status.HTTP_400_BAD_REQUEST)

        tx_type = (request.data.get('type') or 'dispense').strip().lower()
        if tx_type not in ('add', 'dispense'):
            return Response({'error': "type must be 'add' or 'dispense'."}, status=status.HTTP_400_BAD_REQUEST)
        note = (request.data.get('note') or '').strip()

        with transaction.atomic():
            try:
                med = MedicineItem.objects.select_for_update().get(pk=pk)
            except MedicineItem.DoesNotExist:
                return Response({'error': 'Medicine not found.'}, status=status.HTTP_404_NOT_FOUND)

            _apply_medicine_stock_delta(med, qty, tx_type, note)

        try:
            from .gdrive_service import schedule_mirror
            schedule_mirror('inventory')
        except Exception:
            pass

        return Response(self.get_serializer(med).data, status=status.HTTP_200_OK)

class StockHistoryViewSet(viewsets.ModelViewSet):
    queryset = StockHistory.objects.all()
    serializer_class = StockHistorySerializer
    permission_classes = [IsClinicStaff]

class PurchaseRequestViewSet(viewsets.ModelViewSet):
    queryset = PurchaseRequest.objects.prefetch_related('history').all()
    serializer_class = PurchaseRequestSerializer
    permission_classes = [IsClinicStaff]

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if (instance.receivedQty or 0) > 0:
            return Response(
                {'error': 'Cannot delete a purchase request that already has received deliveries. Archive or keep it for audit history.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=['post'], url_path='receive')
    def receive(self, request, pk=None):
        try:
            qty = int(request.data.get('qty', 0))
        except (ValueError, TypeError):
            return Response({'error': 'Valid integer qty is required.'}, status=status.HTTP_400_BAD_REQUEST)
        if qty <= 0:
            return Response({'error': 'qty must be greater than 0.'}, status=status.HTTP_400_BAD_REQUEST)

        raw_date = (request.data.get('date') or '').strip()
        rx_date = raw_date or timezone.localdate().isoformat()
        note = (request.data.get('note') or '').strip()

        med_updated = None
        with transaction.atomic():
            try:
                pr = PurchaseRequest.objects.select_for_update().get(pk=pk)
            except PurchaseRequest.DoesNotExist:
                return Response({'error': 'Purchase request not found.'}, status=status.HTTP_404_NOT_FOUND)

            if pr.status == 'Voided':
                return Response({'error': 'Cannot receive stock for a voided purchase request.'}, status=status.HTTP_400_BAD_REQUEST)

            pr.receivedQty = (pr.receivedQty or 0) + qty
            pr.status = 'Complete' if pr.receivedQty >= (pr.requestedQty or 0) else 'Partial'
            pr.save()

            PurchaseHistory.objects.create(
                request=pr,
                date=rx_date,
                qty=qty,
                note=note or f'Received delivery (+{qty} units)',
            )

            med_name = (pr.medicine or '').strip()
            if med_name:
                med = MedicineItem.objects.select_for_update().filter(name__iexact=med_name).first()
                if med:
                    _apply_medicine_stock_delta(med, qty, 'add', f'From PR {pr.prfNo or pr.id}')
                    med_updated = med

        if med_updated is not None:
            try:
                from .gdrive_service import schedule_mirror
                schedule_mirror('inventory')
            except Exception:
                pass

        return Response(
            {
                'request': PurchaseRequestSerializer(pr).data,
                'medicine': MedicineItemSerializer(med_updated).data if med_updated is not None else None,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=['post'], url_path='void-prf')
    def void_prf(self, request):
        ids = request.data.get('ids') or []
        prf_no = (request.data.get('prfNo') or '').strip()
        reason = (request.data.get('reason') or '').strip()
        staff_name = (request.data.get('staffName') or request.data.get('staff_name') or '').strip()

        if not isinstance(ids, list) and not prf_no:
            return Response({'error': 'ids list or prfNo is required.'}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            qs = PurchaseRequest.objects.select_for_update()
            if isinstance(ids, list) and len(ids) > 0:
                qs = qs.filter(id__in=ids)
            elif prf_no:
                qs = qs.filter(prfNo=prf_no)
            else:
                return Response({'error': 'No matching purchase requests found.'}, status=status.HTTP_400_BAD_REQUEST)

            items = list(qs)
            if not items:
                return Response({'error': 'No matching purchase requests found.'}, status=status.HTTP_404_NOT_FOUND)

            if any((item.receivedQty or 0) > 0 for item in items):
                return Response(
                    {'error': 'Cannot void a PRF that already has received deliveries. Keep it as Partial/Complete for inventory audit.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            today_str = timezone.localdate().isoformat()
            void_note = f"Voided PRF{f' ({reason})' if reason else ''}{f' by {staff_name}' if staff_name else ''}"
            for item in items:
                item.status = 'Voided'
                item.save()
                PurchaseHistory.objects.create(
                    request=item,
                    date=today_str,
                    qty=0,
                    note=void_note,
                )

        refreshed = PurchaseRequest.objects.prefetch_related('history').filter(id__in=[item.id for item in items])
        return Response(PurchaseRequestSerializer(refreshed, many=True).data, status=status.HTTP_200_OK)

class PurchaseHistoryViewSet(viewsets.ModelViewSet):
    queryset = PurchaseHistory.objects.all()
    serializer_class = PurchaseHistorySerializer
    permission_classes = [IsClinicStaff]

class MedicalCertificateViewSet(PatientScopedMixin, viewsets.ModelViewSet):
    queryset = MedicalCertificate.objects.select_related('patient').all()
    serializer_class = MedicalCertificateSerializer

    def get_queryset(self):
        qs = self.filter_queryset_for_user(MedicalCertificate.objects.select_related('patient').all())
        patient_id = self.request.query_params.get('patient')
        if patient_id:
            return qs.filter(patient_id=patient_id)
        return qs

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        patient = serializer.validated_data.get('patient')
        date = serializer.validated_data.get('date')
        purpose = serializer.validated_data.get('purpose', '')
        diagnosis = serializer.validated_data.get('diagnosis', '')

        existing = next(
            (
                cert for cert in MedicalCertificate.objects.filter(
                    patient=patient,
                    date=date,
                    purpose=purpose,
                )
                if (cert.diagnosis or '') == (diagnosis or '')
            ),
            None,
        )

        if existing:
            return Response(self.get_serializer(existing).data, status=status.HTTP_200_OK)

        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        instance = serializer.save()
        try:
            from .models import AppNotification
            p = instance.patient
            AppNotification.objects.create(
                type='certificate',
                message=f'Your Medical Certificate ({instance.purpose}) has been issued by {instance.doctor or "Clinic Physician"}.',
                patientName=p.name,
                patient_id=str(p.id),
                read=False
            )
        except Exception as e:
            print("Certificate notification note:", e)

class BedViewSet(viewsets.ModelViewSet):
    queryset = Bed.objects.prefetch_related('history').all().order_by('bedNumber')
    serializer_class = BedSerializer

    def get_permissions(self):
        if getattr(self, 'action', None) in ('list', 'retrieve'):
            return [IsAuthenticated()]
        return [IsClinicStaff()]

    def list(self, request, *args, **kwargs):
        if not Bed.objects.exists():
            beds_to_create = [Bed(bedNumber=i, status='Available') for i in range(1, 9)]
            Bed.objects.bulk_create(beds_to_create)
        return super().list(request, *args, **kwargs)

class BedHistoryViewSet(viewsets.ModelViewSet):
    queryset = BedHistory.objects.all()
    serializer_class = BedHistorySerializer
    permission_classes = [IsClinicStaff]

class HospitalTransferViewSet(viewsets.ModelViewSet):
    queryset = HospitalTransfer.objects.all()
    serializer_class = HospitalTransferSerializer
    permission_classes = [IsClinicStaff]

class AppNotificationViewSet(viewsets.ModelViewSet):
    queryset = AppNotification.objects.all().order_by('-time')
    serializer_class = AppNotificationSerializer
    permission_classes = [IsAuthenticated]

    PATIENT_TYPES = ['appointment_update', 'telemedicine_update', 'certificate']

    def _scope_for_audience(self, qs, audience, request):
        from django.db.models import Q
        if not is_staff_account(getattr(request, 'user', None)):
            own_patients = list(get_user_patients(getattr(request, 'user', None)).values('id', 'name'))
            own_ids = [str(p['id']).strip() for p in own_patients if p.get('id')]
            own_names = [str(p['name']).strip() for p in own_patients if p.get('name')]
            qs = qs.exclude(type__in=['appointment_request', 'telemedicine_request', 'medcert_request', 'stock'])
            query = Q()
            if own_ids:
                query |= Q(patient_id__in=own_ids)
            for pname in own_names:
                query |= Q(patientName__iexact=pname)
            return qs.filter(query) if query else qs.none()
        if audience in ('admin', 'web'):
            return qs.exclude(type__in=self.PATIENT_TYPES).exclude(
                Q(type='general', message__icontains="it's your turn") |
                Q(type='general', message__icontains='your medical certificate')
            )
        if audience in ('mobile', 'patient'):
            patient_id = request.query_params.get('patient_id') or request.data.get('patient_id')
            patient_name = request.query_params.get('patient_name') or request.data.get('patient_name')
            qs = qs.exclude(type__in=['appointment_request', 'telemedicine_request', 'medcert_request', 'stock'])
            if patient_id:
                query = Q(patient_id=str(patient_id).strip())
                if patient_name:
                    query |= Q(patientName__iexact=str(patient_name).strip())
                return qs.filter(query)
            if patient_name:
                return qs.filter(patientName__iexact=str(patient_name).strip())
            return qs.filter(patient_id__isnull=True, patientName__isnull=True)
        return qs

    def get_queryset(self):
        qs = AppNotification.objects.all().order_by('-time')
        audience = self.request.query_params.get('audience') or self.request.query_params.get('target')
        return self._scope_for_audience(qs, audience, self.request)

    @action(detail=False, methods=['post', 'patch'])
    def mark_all_read(self, request):
        audience = request.query_params.get('audience') or request.query_params.get('target') or request.data.get('audience')
        qs = self._scope_for_audience(AppNotification.objects.filter(read=False), audience, request)
        qs.update(read=True)
        return Response({'status': 'all marked as read'})

    @action(detail=False, methods=['post', 'delete'])
    def clear_all(self, request):
        audience = request.query_params.get('audience') or request.query_params.get('target') or request.data.get('audience')
        qs = self._scope_for_audience(AppNotification.objects.all(), audience, request)
        deleted_count, _ = qs.delete()
        return Response({'status': 'all notifications cleared', 'deleted': deleted_count})

from .models import TelemedicineRequest, TelemedicineAvailability, AppointmentAvailability
from .serializers import TelemedicineRequestSerializer

_FEATURE_UNLOCKED_MARKER = '__FEATURE_UNLOCKED__'


def _load_json_list(raw):
    import json
    try:
        val = json.loads(raw) if raw else []
        return val if isinstance(val, list) else []
    except Exception:
        return []


def _is_feature_locked(model_cls) -> bool:
    try:
        record = model_cls.objects.first()
    except Exception:
        return True
    if not record:
        return True
    return _FEATURE_UNLOCKED_MARKER not in _load_json_list(getattr(record, 'unavailable_times', '[]'))


def _availability_response(model_cls, request):
    import json
    try:
        record = model_cls.objects.first()
        if not record:
            record = model_cls.objects.create(unavailable_dates='[]', unavailable_times='[]')
    except Exception:
        return Response({'unavailable_dates': [], 'unavailable_times': [], 'is_locked': True})

    dates = _load_json_list(record.unavailable_dates)
    raw_times = _load_json_list(getattr(record, 'unavailable_times', '[]'))
    unlocked = _FEATURE_UNLOCKED_MARKER in raw_times

    if request.method == 'POST':
        if 'unavailable_dates' in request.data:
            incoming_dates = request.data.get('unavailable_dates', [])
            dates = incoming_dates if isinstance(incoming_dates, list) else []
            record.unavailable_dates = json.dumps(dates)

        if 'is_locked' in request.data:
            raw_lock = request.data.get('is_locked')
            locked = raw_lock if isinstance(raw_lock, bool) else str(raw_lock).strip().lower() in ('true', '1', 'yes')
            unlocked = not locked

        base_times = (
            [t for t in (request.data.get('unavailable_times') or []) if t != _FEATURE_UNLOCKED_MARKER]
            if 'unavailable_times' in request.data and isinstance(request.data.get('unavailable_times'), list)
            else [t for t in raw_times if t != _FEATURE_UNLOCKED_MARKER]
        )
        record.unavailable_times = json.dumps(base_times + ([_FEATURE_UNLOCKED_MARKER] if unlocked else []))
        record.save()
        return Response({
            'status': 'success',
            'unavailable_dates': dates,
            'unavailable_times': base_times,
            'is_locked': not unlocked,
        })

    req_model = TelemedicineRequest if model_cls is TelemedicineAvailability else AppointmentRequest
    try:
        booked_slots = [
            {
                'preferred_date': str(r.preferred_date) if r.preferred_date else '',
                'preferred_time': r.preferred_time or '',
                'scheduled_date': str(r.scheduled_date) if r.scheduled_date else '',
                'scheduled_time': r.scheduled_time or '',
                'status': r.status or '',
            }
            for r in req_model.objects.exclude(status__in=['Rejected', 'Completed', 'Cancelled']).only(
                'preferred_date', 'preferred_time', 'scheduled_date', 'scheduled_time', 'status'
            )
        ]
    except Exception:
        booked_slots = []

    clean_times = [t for t in raw_times if t != _FEATURE_UNLOCKED_MARKER]
    return Response({
        'unavailable_dates': dates,
        'unavailable_times': clean_times,
        'is_locked': not unlocked,
        'booked_slots': booked_slots,
    })


class TelemedicineRequestViewSet(PatientScopedMixin, viewsets.ModelViewSet):
    queryset = TelemedicineRequest.objects.select_related('patient').all().order_by('-created_at')
    serializer_class = TelemedicineRequestSerializer
    allow_patient_create = True

    def get_permissions(self):
        action_name = getattr(self, 'action', None)
        if action_name == 'availability':
            return [AllowAny()] if self.request.method in SAFE_METHODS else [IsClinicStaff()]
        if action_name == 'approve':
            return [IsClinicStaff()]
        return super().get_permissions()

    def get_queryset(self):
        qs = self.filter_queryset_for_user(
            TelemedicineRequest.objects.select_related('patient').all().order_by('-created_at')
        )
        patient_id = self.request.query_params.get('patient')
        if patient_id:
            return qs.filter(patient_id=patient_id)
        return qs

    def create(self, request, *args, **kwargs):
        if _is_feature_locked(TelemedicineAvailability):
            return Response(
                {'error': 'Telemedicine is currently locked (Coming Soon).'},
                status=status.HTTP_403_FORBIDDEN,
            )
        self._assert_owned_patient_id(request, request.data.get('patient'))
        response = super().create(request, *args, **kwargs)
        if response.status_code == 201:
            from .models import AppNotification, Patient
            patient_id = response.data.get('patient')
            patient = Patient.objects.filter(id=patient_id).first()
            p_name = patient.name if patient else patient_id
            AppNotification.objects.create(
                type='telemedicine_request',
                message=f"New Telemedicine Request from {p_name}",
                patientName=p_name,
                patient_id=patient_id
            )
        return response

    @action(detail=True, methods=['patch'])
    def approve(self, request, pk=None):
        telemed_request = self.get_object()
        
        # Admin provides these fields in the request body
        scheduled_date = request.data.get('scheduled_date')
        scheduled_time = request.data.get('scheduled_time')
        meeting_link = request.data.get('meeting_link')
        secondary_link = request.data.get('secondary_link')
        status = request.data.get('status', 'Approved')
        
        telemed_request.status = status
        
        if status == 'Approved':
            if scheduled_date:
                telemed_request.scheduled_date = scheduled_date
            if scheduled_time:
                telemed_request.scheduled_time = scheduled_time
            
            clean_room_id = str(telemed_request.id).replace('-', '')[:8]
            if not meeting_link or not str(meeting_link).startswith('http') or 'meet.jit.si' in str(meeting_link):
                meeting_link = f"{settings.FRONTEND_PUBLIC_URL}/call/CURA-Telemed-{clean_room_id}"
            
            telemed_request.meeting_link = meeting_link
            if secondary_link is not None:
                telemed_request.secondary_link = secondary_link
                
            telemed_request.save()
            
            _send_clinic_email(
                getattr(telemed_request.patient, 'email', ''),
                'Telemedicine Consultation Approved',
                f"Your telemedicine consultation is approved for {scheduled_date} at {scheduled_time}. Meeting Link: {meeting_link}",
            )
            from .models import AppNotification
            AppNotification.objects.create(
                type='telemedicine_update',
                message=f"Your telemedicine consultation is approved for {scheduled_date} at {scheduled_time}.",
                patientName=telemed_request.patient.name,
                patient_id=telemed_request.patient.id
            )
            
        elif status == 'Rejected':
            telemed_request.save()
            _send_clinic_email(
                getattr(telemed_request.patient, 'email', ''),
                'Telemedicine Consultation Update',
                'Your telemedicine consultation request has been rejected. Please contact the clinic for more details.',
            )
            from .models import AppNotification
            AppNotification.objects.create(
                type='telemedicine_update',
                message=f"Your telemedicine consultation request has been rejected.",
                patientName=telemed_request.patient.name,
                patient_id=telemed_request.patient.id
            )
            
        serializer = self.get_serializer(telemed_request)
        return Response(serializer.data)

    @action(detail=False, methods=['get', 'post'])
    def availability(self, request):
        return _availability_response(TelemedicineAvailability, request)



from .models import AppointmentRequest
from .serializers import AppointmentRequestSerializer

class AppointmentRequestViewSet(PatientScopedMixin, viewsets.ModelViewSet):
    queryset = AppointmentRequest.objects.select_related('patient').all().order_by('-created_at')
    serializer_class = AppointmentRequestSerializer
    allow_patient_create = True

    def get_permissions(self):
        action_name = getattr(self, 'action', None)
        if action_name == 'availability':
            return [AllowAny()] if self.request.method in SAFE_METHODS else [IsClinicStaff()]
        if action_name == 'approve':
            return [IsClinicStaff()]
        return super().get_permissions()

    def get_queryset(self):
        qs = self.filter_queryset_for_user(
            AppointmentRequest.objects.select_related('patient').all().order_by('-created_at')
        )
        patient_id = self.request.query_params.get('patient')
        if patient_id:
            return qs.filter(patient_id=patient_id)
        return qs

    def create(self, request, *args, **kwargs):
        if _is_feature_locked(AppointmentAvailability):
            return Response(
                {'error': 'Appointments are currently locked (Coming Soon).'},
                status=status.HTTP_403_FORBIDDEN,
            )
        self._assert_owned_patient_id(request, request.data.get('patient'))
        response = super().create(request, *args, **kwargs)
        if response.status_code == 201:
            from .models import AppNotification, Patient
            patient_id = response.data.get('patient')
            patient = Patient.objects.filter(id=patient_id).first()
            p_name = patient.name if patient else patient_id
            AppNotification.objects.create(
                type='appointment_request',
                message=f"New Appointment Request from {p_name}",
                patientName=p_name,
                patient_id=patient_id
            )
        return response

    @action(detail=True, methods=['patch'])
    def approve(self, request, pk=None):
        appointment = self.get_object()
        
        scheduled_date = request.data.get('scheduled_date')
        scheduled_time = request.data.get('scheduled_time')
        status = request.data.get('status', 'Approved')
        
        appointment.status = status
        
        if status == 'Approved':
            if scheduled_date:
                appointment.scheduled_date = scheduled_date
            if scheduled_time:
                appointment.scheduled_time = scheduled_time
                
            appointment.save()
            
            _send_clinic_email(
                getattr(appointment.patient, 'email', ''),
                'In-Person Appointment Approved',
                f"Your appointment is approved for {scheduled_date} at {scheduled_time}.",
            )
            from .models import AppNotification
            AppNotification.objects.create(
                type='appointment_update',
                message=f"Your appointment is approved for {scheduled_date} at {scheduled_time}.",
                patientName=appointment.patient.name,
                patient_id=appointment.patient.id
            )
            
        elif status == 'Rejected':
            appointment.save()
            _send_clinic_email(
                getattr(appointment.patient, 'email', ''),
                'Appointment Update',
                'Your appointment request has been rejected. Please contact the clinic for more details.',
            )
            from .models import AppNotification
            AppNotification.objects.create(
                type='appointment_update',
                message=f"Your appointment request has been rejected.",
                patientName=appointment.patient.name,
                patient_id=appointment.patient.id
            )
            
        serializer = self.get_serializer(appointment)
        return Response(serializer.data)

    @action(detail=False, methods=['get', 'post'])
    def availability(self, request):
        return _availability_response(AppointmentAvailability, request)



from .models import PatientQueue
from .serializers import PatientQueueSerializer
from django.db import transaction

class PatientQueueViewSet(PatientScopedMixin, viewsets.ModelViewSet):
    serializer_class = PatientQueueSerializer
    allow_patient_create = True

    def get_permissions(self):
        if getattr(self, 'action', None) in ('notify', 'complete', 'update', 'partial_update', 'destroy'):
            return [IsClinicStaff()]
        return [IsAuthenticated()]

    def get_queryset(self):
        req_date = self.request.query_params.get('date')
        patient_id = self.request.query_params.get('patient')
        qs = PatientQueue.objects.select_related('patient')
        if patient_id:
            qs = qs.filter(patient_id=patient_id)
        if self.request.query_params.get('all') == 'true':
            return qs.all().order_by('queue_number')
        if req_date:
            return qs.filter(date=req_date).order_by('queue_number')
        return qs.filter(date=timezone.localdate()).order_by('queue_number')

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        patient_id = request.data.get('patient')
        if not patient_id:
            return Response({"error": "Patient ID is required"}, status=status.HTTP_400_BAD_REQUEST)
        self._assert_owned_patient_id(request, patient_id)
        
        from .models import Patient
        
        try:
            patient = Patient.objects.select_for_update().get(id=patient_id)
        except Patient.DoesNotExist:
            return Response({"error": "Patient not found"}, status=status.HTTP_400_BAD_REQUEST)

        # Check if patient already in queue and not done
        today = timezone.localdate()
        existing = PatientQueue.objects.select_for_update().filter(
            patient_id=patient_id, status__in=['waiting', 'called'], date=today
        ).first()
        if existing:
            serializer = self.get_serializer(existing)
            return Response(serializer.data, status=status.HTTP_200_OK)
            
        # Lock today's queue rows so concurrent check-ins never get duplicate queue_numbers
        last_queue = PatientQueue.objects.select_for_update().filter(date=today).order_by('-queue_number').first()
        queue_number = 1 if not last_queue else last_queue.queue_number + 1
        
        queue = PatientQueue.objects.create(
            patient=patient,
            queue_number=queue_number,
            status='waiting',
            date=today
        )
        serializer = self.get_serializer(queue)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['patch', 'post'])
    def notify(self, request, pk=None):
        queue = self.get_object()
        if queue.status != 'waiting':
            return Response({"error": "Only waiting patients can be notified"}, status=status.HTTP_400_BAD_REQUEST)
            
        queue.status = 'called'
        queue.save()
        
        from .models import AppNotification
        AppNotification.objects.create(
            type='general',
            message=f"It's your turn! Please proceed to the clinic counter.",
            patientName=queue.patient.name,
            patient_id=queue.patient.id
        )
        
        serializer = self.get_serializer(queue)
        return Response(serializer.data)

    @action(detail=True, methods=['patch', 'post'])
    def complete(self, request, pk=None):
        queue = self.get_object()
        queue.status = 'done'
        queue.save()
        serializer = self.get_serializer(queue)
        return Response(serializer.data)

    @action(detail=True, methods=['patch', 'post'])
    def cancel(self, request, pk=None):
        queue = self.get_object()
        self._assert_owned_patient_id(request, queue.patient_id)
        queue.status = 'done'
        queue.save()
        serializer = self.get_serializer(queue)
        return Response(serializer.data)

from .models import ClinicAdvisory
from .serializers import ClinicAdvisorySerializer

class ClinicAdvisoryViewSet(viewsets.ModelViewSet):
    permission_classes = [IsStaffOrReadOnly]
    queryset = ClinicAdvisory.objects.all()
    serializer_class = ClinicAdvisorySerializer

    def list(self, request, *args, **kwargs):
        advisory = ClinicAdvisory.objects.first()
        if not advisory:
            advisory = ClinicAdvisory.objects.create(
                status='Closed',
                message='Welcome to the University Clinic! Standard operating hours are 8:00 AM to 5:00 PM.'
            )
        serializer = self.get_serializer(advisory)
        return Response(serializer.data)

    def create(self, request, *args, **kwargs):
        status_val = request.data.get('status', 'Closed')
        message_val = request.data.get('message', '')
        advisory = ClinicAdvisory.objects.first()
        if advisory:
            advisory.status = status_val
            advisory.message = message_val
            advisory.save()
        else:
            advisory = ClinicAdvisory.objects.create(status=status_val, message=message_val)
        serializer = self.get_serializer(advisory)
        return Response(serializer.data, status=status.HTTP_200_OK)

from .models import MedicalCertificateRequest
from .serializers import MedicalCertificateRequestSerializer

class MedicalCertificateRequestViewSet(PatientScopedMixin, viewsets.ModelViewSet):
    queryset = MedicalCertificateRequest.objects.select_related('patient').all().order_by('-created_at')
    serializer_class = MedicalCertificateRequestSerializer
    allow_patient_create = True

    def get_permissions(self):
        if getattr(self, 'action', None) == 'approve':
            return [IsClinicStaff()]
        return super().get_permissions()

    def get_queryset(self):
        qs = self.filter_queryset_for_user(
            MedicalCertificateRequest.objects.select_related('patient').all().order_by('-created_at')
        )
        patient_id = self.request.query_params.get('patient')
        if patient_id:
            return qs.filter(patient_id=patient_id)
        return qs

    def create(self, request, *args, **kwargs):
        self._assert_owned_patient_id(request, request.data.get('patient'))
        response = super().create(request, *args, **kwargs)
        if response.status_code == 201:
            patient_id = response.data.get('patient')
            patient = Patient.objects.filter(id=patient_id).first()
            p_name = patient.name if patient else patient_id
            AppNotification.objects.create(
                type='medcert_request',
                message=f"New Medical Certificate Request from {p_name}",
                patientName=p_name,
                patient_id=patient_id
            )
        return response

    @action(detail=True, methods=['patch'])
    @transaction.atomic
    def approve(self, request, pk=None):
        medcert_req = MedicalCertificateRequest.objects.select_for_update().select_related('patient').get(pk=pk)
        was_already_approved = medcert_req.status == 'Approved'
        
        status_val = request.data.get('status', 'Approved')
        diagnosis = request.data.get('diagnosis') or medcert_req.diagnosis or medcert_req.complaint
        recommendations = request.data.get('recommendations') or medcert_req.recommendations or 'Fit to resume regular activities.'
        doctor = request.data.get('doctor') or 'JOHNNY MICHAEL P. MANGULABNAN, MD'
        remarks = request.data.get('remarks', '')
        
        medcert_req.status = status_val
        if diagnosis:
            medcert_req.diagnosis = diagnosis
        if recommendations:
            medcert_req.recommendations = recommendations
        if doctor:
            medcert_req.doctor = doctor
        if remarks:
            medcert_req.remarks = remarks
        medcert_req.save()

        if status_val == 'Approved' and not was_already_approved:
            cert_date = timezone.localdate()
            MedicalCertificate.objects.create(
                patient=medcert_req.patient,
                date=cert_date,
                purpose=medcert_req.purpose or 'Medical Certificate issuance',
                diagnosis=diagnosis or medcert_req.complaint,
                recommendation=recommendations,
                doctor=doctor,
                issuedBy='UA CLINIC ADMIN',
                notes=remarks or f"Issued from request #{str(medcert_req.id)[:8]}"
            )
            
            AppNotification.objects.create(
                type='certificate',
                message="Your medical certificate request has been approved and issued.",
                patientName=medcert_req.patient.name,
                patient_id=medcert_req.patient.id
            )
        elif status_val == 'Rejected':
            AppNotification.objects.create(
                type='certificate',
                message=f"Your medical certificate request has been declined. {remarks}".strip(),
                patientName=medcert_req.patient.name,
                patient_id=medcert_req.patient.id
            )

        serializer = self.get_serializer(medcert_req)
        return Response(serializer.data)


from django.contrib.auth.hashers import check_password
from .models import ClinicStaff, AuditLog
from .serializers import ClinicStaffSerializer, AuditLogSerializer


STAFF_PIN_SENTINEL = '__CURA_STAFF_PIN_LOCKOUT__'


def _get_lockout_attempt(request, prefix, max_attempts=5):
    from datetime import timedelta
    now = timezone.now()
    ip = (request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() or request.META.get('REMOTE_ADDR') or 'global')
    attempt, _ = LoginAttempt.objects.get_or_create(identifier=f"{prefix}:{ip}"[:255])

    if attempt.is_locked():
        return attempt, Response(
            {
                'valid': False,
                'is_locked': True,
                'attempts_remaining': 0,
                'max_attempts': max_attempts,
                'lockout_seconds': max(1, attempt.remaining_seconds()),
                'error': 'Too many failed attempts. Please wait before trying again.',
            },
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    if (attempt.locked_until and not attempt.is_locked()) or (
        attempt.failed_attempts > 0 and attempt.last_attempt_at and (now - attempt.last_attempt_at) > timedelta(minutes=15)
    ):
        attempt.failed_attempts = 0
        attempt.locked_until = None
        attempt.save()

    return attempt, None


def _clear_lockout_attempt(attempt):
    if attempt.failed_attempts > 0 or attempt.locked_until:
        attempt.failed_attempts = 0
        attempt.locked_until = None
        attempt.save()


def _record_lockout_failure(attempt, invalid_error, max_attempts=5, lockout_minutes=1, extra=None):
    from datetime import timedelta
    payload_extra = extra or {}
    attempt.failed_attempts += 1
    if attempt.failed_attempts >= max_attempts:
        attempt.locked_until = timezone.now() + timedelta(minutes=lockout_minutes)
        attempt.save()
        return Response(
            {
                'valid': False,
                **payload_extra,
                'is_locked': True,
                'attempts_remaining': 0,
                'max_attempts': max_attempts,
                'lockout_seconds': lockout_minutes * 60,
                'error': 'Too many failed attempts. Please wait before trying again.',
            },
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    attempt.save()
    return Response(
        {
            'valid': False,
            **payload_extra,
            'is_locked': False,
            'attempts_remaining': max(1, max_attempts - attempt.failed_attempts),
            'max_attempts': max_attempts,
            'error': invalid_error,
        },
        status=status.HTTP_401_UNAUTHORIZED,
    )


class ClinicStaffViewSet(viewsets.ModelViewSet):
    queryset = ClinicStaff.objects.all().order_by('name')
    serializer_class = ClinicStaffSerializer

    def get_authenticators(self):
        if self.request and getattr(self.request, 'method', 'GET') == 'GET':
            return []
        path = (getattr(self.request, 'path', '') or '').lower()
        if any(seg in path for seg in ('verify-admin', 'change-admin-password', 'verify-pin')):
            return []
        return super().get_authenticators()

    def get_permissions(self):
        if getattr(self, 'action', None) in ('list', 'retrieve', 'verify_admin', 'change_admin_password', 'verify_pin'):
            return [AllowAny()]
        return [IsClinicStaff()]

    @action(detail=False, methods=['post'], url_path='verify-admin')
    def verify_admin(self, request):
        password = (request.data.get('password') or '').strip()
        if not password:
            return Response({'valid': False, 'error': 'Password is required.'}, status=status.HTTP_400_BAD_REQUEST)

        attempt, locked_response = _get_lockout_attempt(request, USER_MGMT_SENTINEL)
        if locked_response is not None:
            return locked_response

        env_configured = bool(get_env_mgmt_passwords())
        if is_valid_mgmt_password(password):
            _clear_lockout_attempt(attempt)
            return Response({'valid': True, 'env_configured': env_configured})

        return _record_lockout_failure(
            attempt,
            'Incorrect User Management password.',
            extra={'env_configured': env_configured},
        )

    @action(detail=False, methods=['post'], url_path='change-admin-password')
    def change_admin_password(self, request):
        current_password = (request.data.get('current_password') or '').strip()
        new_password = (request.data.get('new_password') or '').strip()

        if not new_password:
            return Response(
                {'error': 'New password is required.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        if len(new_password) < 6:
            return Response(
                {'error': 'New User Management password must be at least 6 characters.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        if is_main_cura_login_password(new_password):
            return Response(
                {'error': 'User Management password must be separate from the main CURA login password.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        if not current_password or not is_valid_mgmt_password(current_password):
            return Response(
                {'error': 'Current or recovery password is incorrect.'},
                status=status.HTTP_401_UNAUTHORIZED
            )

        sentinel, _ = User.objects.get_or_create(
            username=USER_MGMT_SENTINEL,
            defaults={'is_active': False, 'is_staff': False, 'is_superuser': False}
        )
        sentinel.is_active = False
        sentinel.set_password(new_password)
        sentinel.save()

        return Response({'success': True, 'message': 'User Management password updated successfully.'})

    @action(detail=False, methods=['post'], url_path='verify-pin')
    def verify_pin(self, request):
        pin = (request.data.get('pin') or '').strip()
        if not pin or len(pin) != 4 or not pin.isdigit():
            return Response(
                {'valid': False, 'error': 'Please enter a valid 4-digit PIN.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        attempt, locked_response = _get_lockout_attempt(request, STAFF_PIN_SENTINEL)
        if locked_response is not None:
            return locked_response

        active_staff = sorted(
            ClinicStaff.objects.filter(is_active=True).only('id', 'name', 'role', 'pin_hash', 'allowed_pages'),
            key=lambda s: not (s.pin_hash or '').startswith('hmac_sha256$'),
        )
        for staff_member in active_staff:
            matched, needs_rehash = verify_staff_pin(pin, staff_member.pin_hash)
            if matched:
                if needs_rehash:
                    staff_member.pin_hash = hash_staff_pin(pin)
                    staff_member.save(update_fields=['pin_hash'])
                _clear_lockout_attempt(attempt)
                return Response({
                    'valid': True,
                    'staff': {
                        'id': str(staff_member.id),
                        'name': staff_member.name,
                        'role': staff_member.role,
                        'allowed_pages': staff_member.allowed_pages,
                    }
                })

        return _record_lockout_failure(attempt, 'Invalid or inactive 4-digit PIN.')


class AuditLogViewSet(viewsets.ModelViewSet):
    queryset = AuditLog.objects.exclude(action='SYSTEM_GDRIVE_CONFIG').order_by('-timestamp')
    serializer_class = AuditLogSerializer
    permission_classes = [IsClinicStaff]

    def get_queryset(self):
        qs = AuditLog.objects.exclude(action='SYSTEM_GDRIVE_CONFIG').order_by('-timestamp')
        if getattr(self, 'action', None) == 'list':
            return qs[:500]
        return qs

    @action(detail=False, methods=['get', 'post'], url_path='drive-archives')
    def drive_archives(self, request):
        from .gdrive_service import SUBFOLDER_IDS, _fmt_manila, list_drive_archives, store_archive_file
        audit_folder_id = SUBFOLDER_IDS['audit_logs']
        folder_url = f"https://drive.google.com/drive/folders/{audit_folder_id}"

        if request.method == 'GET':
            return Response({
                'folder_id': audit_folder_id,
                'folder_url': folder_url,
                'archives': list_drive_archives(folder_keys=('audit_logs',)),
            }, status=status.HTTP_200_OK)

        import csv
        import io
        staff_name = (request.data.get('staff_name') or 'ADMIN').strip()
        today_str = str(timezone.localdate())
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(['Timestamp', 'Staff_Name', 'Staff_Role', 'Action', 'Target_Type', 'Target_ID', 'Details'])
        # ponytail: capped at 5000 rows (~1-2MB CSV) to bound snapshot memory/runtime per request
        for log in AuditLog.objects.exclude(action='SYSTEM_GDRIVE_CONFIG').order_by('-timestamp')[:5000]:
            writer.writerow([
                _fmt_manila(log.timestamp),
                log.staff_name_snapshot or '',
                log.staff_role_snapshot or '',
                log.action or '',
                log.target_type or '',
                log.target_id or '',
                log.details or '',
            ])

        info = store_archive_file(
            raw_bytes=buf.getvalue().encode('utf-8'),
            filename=f"CURA_Audit_Logs_Snapshot_{today_str}.csv",
            mime_type='text/csv; charset=utf-8',
            folder_key='audit_logs',
            staff_name=staff_name,
            report_title=f"Audit Logs Snapshot ({today_str})",
        )
        if not info:
            return Response({'error': 'Could not sync audit logs to Google Drive.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response({**info, 'folder_url': folder_url}, status=status.HTTP_200_OK)


class MediaUploadView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from .gdrive_service import offload_image
        category = (request.data.get('category') or 'avatar').strip().lower()
        if category not in ('avatar', 'prescription', 'fetcher_id'):
            category = 'avatar'
        label = (request.data.get('label') or request.data.get('patient_id') or 'upload').strip()

        uploaded_file = request.FILES.get('file') or request.FILES.get('image')
        raw_input = uploaded_file.read() if uploaded_file else (request.data.get('image') or request.data.get('file'))
        if not raw_input:
            return Response({'error': 'No image provided.'}, status=status.HTTP_400_BAD_REQUEST)

        url = offload_image(raw_input, category=category, label=label)
        if not url:
            return Response({'error': 'Failed to process image.'}, status=status.HTTP_400_BAD_REQUEST)

        patient_id = (request.data.get('patient_id') or '').strip()
        if patient_id and category == 'avatar':
            Patient.objects.filter(id=patient_id).update(avatar=url)

        return Response({'url': url, 'category': category}, status=status.HTTP_200_OK)


class MediaConfigView(APIView):
    permission_classes = [IsClinicStaff]

    def post(self, request):
        from .gdrive_service import set_runtime_credentials
        info = request.data.get('credentials') or request.data
        if isinstance(info, str):
            import json
            info = json.loads(info)
        if not isinstance(info, dict) or info.get('client_email') != 'cura-uploader@cura-clinic-storage.iam.gserviceaccount.com':
            return Response({'error': 'Invalid service account payload.'}, status=status.HTTP_403_FORBIDDEN)
        try:
            set_runtime_credentials(info)
            return Response({'status': 'ok', 'client_email': info.get('client_email')})
        except Exception as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)


def serve_drive_media(request, drive_id):
    from django.http import HttpResponse, Http404
    from .gdrive_service import fetch_media_from_drive
    result = fetch_media_from_drive(drive_id)
    if not result:
        raise Http404("Media not found")
    raw_bytes, mime_type = result
    resp = HttpResponse(raw_bytes, content_type=mime_type)
    resp['Cache-Control'] = 'public, max-age=31536000, immutable'
    resp['Access-Control-Allow-Origin'] = '*'
    return resp


class ReportArchiveView(APIView):
    permission_classes = [IsClinicStaff]

    def get(self, request):
        from .gdrive_service import list_drive_archives
        return Response({'archives': list_drive_archives()}, status=status.HTTP_200_OK)

    def post(self, request):
        import base64
        from .gdrive_service import store_archive_file
        filename = (request.data.get('filename') or f"CURA_Report_{timezone.localdate()}.xlsx").strip()
        title = (request.data.get('title') or filename).strip()
        staff_name = (request.data.get('staff_name') or 'SYSTEM').strip()
        folder_key = (request.data.get('folder') or 'archives').strip()
        mime_type = (request.data.get('mime_type') or 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet').strip()

        uploaded_file = request.FILES.get('file')
        if uploaded_file:
            raw_bytes = uploaded_file.read()
        else:
            b64 = (request.data.get('file_b64') or request.data.get('data') or '').strip()
            if not b64:
                return Response({'error': 'No report file provided.'}, status=status.HTTP_400_BAD_REQUEST)
            if ';base64,' in b64:
                b64 = b64.split(';base64,', 1)[1]
            raw_bytes = base64.b64decode(b64)

        info = store_archive_file(
            raw_bytes=raw_bytes,
            filename=filename,
            mime_type=mime_type,
            folder_key=folder_key,
            staff_name=staff_name,
            report_title=title,
        )
        if not info:
            return Response({'error': 'Could not archive report to Google Drive.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response(info, status=status.HTTP_200_OK)


class ReportSyncSnapshotView(APIView):
    permission_classes = [IsClinicStaff]

    def post(self, request):
        import csv
        import io
        from .gdrive_service import store_archive_file
        staff_name = (request.data.get('staff_name') or 'SYSTEM').strip()
        today_str = str(timezone.localdate())

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            'Consultation_ID', 'Date', 'Time_In', 'Time_Out', 'Patient_ID', 'Patient_Name',
            'Category', 'Course_Dept', 'Status', 'Complaint', 'Categories', 'Doctor',
            'Assisting_Nurse', 'Treatments', 'Early_Dismissal', 'Destination', 'Nurse_Notes'
        ])
        for c in Consultation.objects.select_related('patient').prefetch_related('treatments').order_by('-date', '-timeIn')[:2000]:
            p = c.patient
            treats = '; '.join(f"{t.medicineName} ({t.quantity} {t.unit})" for t in c.treatments.all())
            writer.writerow([
                str(c.id), str(c.date), str(c.timeIn or ''), str(c.timeOut or ''),
                str(p.id if p else c.patient_id), str(p.name if p else ''),
                str(p.category if p else ''), str((p.course or p.department or p.gradeLevel or '') if p else ''),
                str(c.status or ''), str(c.complaint or c.purposeOfVisit or ''),
                ', '.join(c.categories or []), str(c.doctorName or ''),
                str(c.assistingNurse or ''), treats,
                'Yes' if c.earlyDismissal else 'No', str(c.dismissalDestination or ''),
                str(c.nurseNotes or c.operationalNotes or '')
            ])

        raw_csv = buf.getvalue().encode('utf-8')
        filename = f"CURA_Full_Clinic_Snapshot_{today_str}.csv"
        info = store_archive_file(
            raw_bytes=raw_csv,
            filename=filename,
            mime_type='text/csv; charset=utf-8',
            folder_key='archives',
            staff_name=staff_name,
            report_title=f"Full Clinic Snapshot ({today_str})",
        )
        if not info:
            return Response({'error': 'Snapshot sync failed.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response(info, status=status.HTTP_200_OK)


@api_view(['GET'])
@drf_permission_classes([IsClinicStaff])
def download_drive_archive(request, drive_id):
    from django.http import HttpResponse, Http404
    from .gdrive_service import fetch_media_from_drive
    result = fetch_media_from_drive(drive_id)
    if not result:
        raise Http404("Archive not found")
    raw_bytes, mime_type = result
    ext = '.csv' if 'csv' in mime_type else ('.xlsx' if 'spreadsheet' in mime_type or 'excel' in mime_type else '.bin')
    resp = HttpResponse(raw_bytes, content_type=mime_type)
    resp['Content-Disposition'] = f'attachment; filename="CURA_Archive_{drive_id[:8]}{ext}"'
    resp['Access-Control-Allow-Origin'] = '*'
    return resp




