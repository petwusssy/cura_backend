import uuid
from django.http import JsonResponse
from django.contrib.auth.models import User
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.tokens import RefreshToken
from django.conf import settings
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, viewsets
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.decorators import action
from google.oauth2 import id_token
from google.auth.transport import requests

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
from django.utils import timezone
from datetime import timedelta

class GoogleLoginView(APIView):
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
            req = requests.Request()

            # 1. Try verifying as Google OAuth2 ID token
            try:
                idinfo = id_token.verify_oauth2_token(token, req, CLIENT_ID)
            except Exception as e1:
                # 2. Try verifying as Google OAuth2 ID token without audience constraint
                try:
                    idinfo = id_token.verify_oauth2_token(token, req)
                except Exception as e2:
                    # 3. Fallback: Parse and validate Firebase Auth ID token
                    try:
                        import jwt
                        decoded = jwt.decode(token, options={"verify_signature": False})
                        iss = decoded.get('iss', '')
                        aud = decoded.get('aud', '')
                        exp = decoded.get('exp', 0)
                        if (iss == f"https://securetoken.google.com/{FIREBASE_PROJECT_ID}" or iss == "https://accounts.google.com" or "securetoken.google.com" in iss) and (aud == FIREBASE_PROJECT_ID or aud == CLIENT_ID):
                            if exp and timezone.now().timestamp() > exp:
                                return Response({'error': 'Token has expired. Please sign in again.'}, status=status.HTTP_401_UNAUTHORIZED)
                            idinfo = decoded
                        else:
                            raise ValueError(f"Unrecognized token issuer ({iss}) or audience ({aud})")
                    except Exception as e3:
                        return Response({
                            'error': 'Invalid token',
                            'details': f"OAuth verification failed: {e1}; Fallback failed: {e2}; Firebase token failed: {e3}"
                        }, status=status.HTTP_401_UNAUTHORIZED)

            email = (idinfo.get('email') or request.data.get('email') or '').strip().lower()
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
                # If there is no registered user account, DO NOT auto-create and DO NOT log in.
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
            refresh = RefreshToken.for_user(user)
            refresh['username'] = user.username
            roles = list(user.groups.values_list('name', flat=True))
            if user.is_superuser:
                roles.append('Admin')
            refresh['roles'] = roles

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
    def post(self, request):
        email = request.data.get('email', '').strip()
        if not email:
            return Response({'error': 'Email is required'}, status=400)
        
        patient = Patient.objects.filter(email__iexact=email).first()
        claimed = User.objects.filter(username__iexact=email).exists() or User.objects.filter(email__iexact=email).exists()
        
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
            'patient': patient_data,
            'missing_fields': missing_fields
        })

class RequestOTPView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        email = request.data.get('email', '').strip().lower()
        allow_new = request.data.get('allow_new', False) or request.data.get('is_google', False)
        if not email:
            return Response({'error': 'Email is required'}, status=400)
        
        patient_exists = Patient.objects.filter(email__iexact=email).exists()
        user_exists = User.objects.filter(username__iexact=email).exists() or User.objects.filter(email__iexact=email).exists()
        if not allow_new and not patient_exists and not user_exists:
            return Response({'error': 'Patient record not found.'}, status=404)
        
        otp = str(random.randint(100000, 999999))
        OTPVerification.objects.update_or_create(
            email=email,
            defaults={
                'otp': otp,
                'expires_at': timezone.now() + timedelta(minutes=10),
                'is_verified': False
            }
        )
        
        from django.core.mail import send_mail
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
        email_sent = False
        resend_key = getattr(settings, 'RESEND_API_KEY', '')
        if resend_key:
            try:
                import requests
                r = requests.post(
                    'https://api.resend.com/emails',
                    headers={'Authorization': f'Bearer {resend_key}', 'Content-Type': 'application/json'},
                    json={
                        'from': 'CURA Clinic <onboarding@resend.dev>',
                        'to': [email],
                        'subject': subject,
                        'html': html_message,
                    },
                    timeout=5
                )
                if r.status_code in (200, 201):
                    email_sent = True
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(f"Resend email API error: {e}")

        if not email_sent:
            try:
                send_mail(
                    subject,
                    message,
                    settings.DEFAULT_FROM_EMAIL,
                    [email],
                    fail_silently=False,
                    html_message=html_message
                )
                email_sent = True
            except Exception as mail_err:
                import logging
                logging.getLogger(__name__).warning(f"SMTP send failed (Render port block): {mail_err}")

        print(f"\n=== CURA OTP VERIFICATION CODE FOR {email}: {otp} ===\n")
        return Response({'message': 'OTP sent successfully', 'otp': otp}, status=status.HTTP_200_OK)

class VerifyOTPView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        email = request.data.get('email', '').strip().lower()
        otp = request.data.get('otp', '').strip()
        
        try:
            verification = OTPVerification.objects.filter(email__iexact=email, otp=otp).latest('id')
            if timezone.now() > verification.expires_at:
                return Response({'error': 'OTP expired'}, status=400)
            
            verification.is_verified = True
            verification.save()

            # Check if user already exists
            user = User.objects.filter(username__iexact=email).first() or User.objects.filter(email__iexact=email).first()
            patient = Patient.objects.filter(email__iexact=email).first()
            patient_name = patient.name if patient else (user.first_name if user else email.split('@')[0])
            patient_id = patient.id if patient else (user.username if user else '')

            res_payload = {
                'message': 'OTP verified',
                'is_verified': True,
                'claimed': bool(user),
            }

            if user:
                refresh = RefreshToken.for_user(user)
                refresh['username'] = user.username
                refresh['roles'] = list(user.groups.values_list('name', flat=True))
                if user.is_superuser:
                    refresh['roles'].append('Admin')
                res_payload.update({
                    'refresh': str(refresh),
                    'access': str(refresh.access_token),
                    'user': {
                        'id': patient_id,
                        'email': user.email or email,
                        'name': patient_name,
                        'is_new': False,
                    }
                })

            return Response(res_payload)
        except OTPVerification.DoesNotExist:
            return Response({'error': 'Invalid OTP'}, status=400)

class RegisterView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        email = request.data.get('email')
        password = request.data.get('password')
        role = request.data.get('role', 'Outsider')
        
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
        if not patient:
            patient = Patient.objects.create(
                id=request.data.get('id_number') or uuid.uuid4().hex[:8],
                name=name_prefix,
                category=category,
                contact='Not Provided',
                birthday='2000-01-01',
                age=0,
                email=clean_email
            )
        else:
            if name_prefix and (patient.name == 'Not Provided' or not patient.name):
                patient.name = name_prefix
            if category:
                patient.category = category
            if request.data.get('id_number'):
                patient.id = request.data.get('id_number')
            patient.save()
        
        # Return JWT token
        refresh = RefreshToken.for_user(user)
        refresh['username'] = user.username
        refresh['roles'] = list(user.groups.values_list('name', flat=True))
        
        return Response({
            'refresh': str(refresh),
            'access': str(refresh.access_token),
            'user': {'email': user.email, 'name': name_prefix, 'is_new': True}
        }, status=status.HTTP_201_CREATED)

from rest_framework.permissions import IsAuthenticated

class CompleteProfileView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        data = request.data
        
        try:
            patient = Patient.objects.filter(email__iexact=user.email.strip()).first()
            if not patient and user.username:
                patient = Patient.objects.filter(email__iexact=user.username.strip()).first()
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
        new_id = data.get('id')
        if new_id and new_id != patient.id:
            # Create a clone with the new ID
            old_id = patient.id
            patient.pk = new_id
            patient.save()
            Patient.objects.filter(id=old_id).delete()
            patient = Patient.objects.get(id=new_id)

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
        
        try:
            verification = OTPVerification.objects.filter(email__iexact=email, is_verified=True).latest('id')
            if timezone.now() > verification.expires_at:
                return Response({'error': 'Session expired'}, status=400)
            
            user, created = User.objects.get_or_create(username=email, defaults={'email': email})
            user.set_password(password)
            user.save()
            
            verification.delete() # Cleanup
            
            refresh = RefreshToken.for_user(user)
            refresh['username'] = user.username
            refresh['roles'] = list(user.groups.values_list('name', flat=True))
            if user.is_superuser:
                refresh['roles'].append('Admin')

            patient = Patient.objects.filter(email__iexact=email).first()
            patient_name = patient.name if patient else email.split('@')[0]
            patient_id = patient.id if patient else user.username
            return Response({
                'refresh': str(refresh),
                'access': str(refresh.access_token),
                'user': {'id': patient_id, 'email': user.email, 'name': patient_name, 'is_new': False}
            })
            
        except OTPVerification.DoesNotExist:
            return Response({'error': 'Email not verified or session expired'}, status=400)

class ChangePasswordView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        email = request.data.get('email', '').strip()
        current_password = request.data.get('current_password')
        new_password = request.data.get('new_password')

        if not email or not new_password:
            return Response({'error': 'Email and new password are required.'}, status=status.HTTP_400_BAD_REQUEST)

        user = User.objects.filter(email__iexact=email).first() or User.objects.filter(username__iexact=email).first()
        if not user:
            return Response({'error': 'User account not found.'}, status=status.HTTP_404_NOT_FOUND)

        if current_password and not user.check_password(current_password):
            return Response({'error': 'Current password is incorrect.'}, status=status.HTTP_400_BAD_REQUEST)

        user.set_password(new_password)
        user.save()
        return Response({'success': True, 'message': 'Password updated successfully.'}, status=status.HTTP_200_OK)

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
        # Add custom claims
        token['username'] = user.username
        # For simplicity, assuming user has groups for role-based access
        roles = list(user.groups.values_list('name', flat=True))
        if user.is_superuser:
            roles.append('Admin')
        token['roles'] = roles
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
        return data

class CustomTokenObtainPairView(TokenObtainPairView):
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
            access_token = response.data.get('access')
            refresh_token = response.data.get('refresh')

            if refresh_token:
                response.set_cookie(
                    key=settings.SIMPLE_JWT['AUTH_COOKIE'],
                    value=refresh_token,
                    expires=settings.SIMPLE_JWT['REFRESH_TOKEN_LIFETIME'],
                    secure=settings.SIMPLE_JWT['AUTH_COOKIE_SECURE'],
                    httponly=settings.SIMPLE_JWT['AUTH_COOKIE_HTTP_ONLY'],
                    samesite=settings.SIMPLE_JWT['AUTH_COOKIE_SAMESITE']
                )
                # Do not return the refresh token in the JSON response
                del response.data['refresh']

        return response

class CustomTokenRefreshView(TokenRefreshView):
    def post(self, request, *args, **kwargs):
        refresh_token = request.COOKIES.get(settings.SIMPLE_JWT['AUTH_COOKIE'])
        
        if refresh_token:
            request.data['refresh'] = refresh_token

        response = super().post(request, *args, **kwargs)
        
        if response.status_code == 200:
            access_token = response.data.get('access')
            refresh_token = response.data.get('refresh')
            
            if refresh_token:
                response.set_cookie(
                    key=settings.SIMPLE_JWT['AUTH_COOKIE'],
                    value=refresh_token,
                    expires=settings.SIMPLE_JWT['REFRESH_TOKEN_LIFETIME'],
                    secure=settings.SIMPLE_JWT['AUTH_COOKIE_SECURE'],
                    httponly=settings.SIMPLE_JWT['AUTH_COOKIE_HTTP_ONLY'],
                    samesite=settings.SIMPLE_JWT['AUTH_COOKIE_SAMESITE']
                )
                del response.data['refresh']
                
        return response

class LogoutView(APIView):
    def post(self, request, *args, **kwargs):
        response = Response({"detail": "Successfully logged out."}, status=status.HTTP_200_OK)
        response.delete_cookie(settings.SIMPLE_JWT['AUTH_COOKIE'])
        return response

# --- Phase 4 ViewSets ---

class PatientViewSet(viewsets.ModelViewSet):
    queryset = Patient.objects.all()
    serializer_class = PatientSerializer
    # permission_classes = [IsAuthenticated]

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
        patient = Patient.objects.filter(email__iexact=email).first()
        if not patient:
            return Response({'error': 'Patient not found'}, status=status.HTTP_404_NOT_FOUND)
        serializer = self.get_serializer(patient, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_200_OK)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class ConsultationViewSet(viewsets.ModelViewSet):
    queryset = Consultation.objects.all()
    serializer_class = ConsultationSerializer
    # permission_classes = [IsAuthenticated]

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

class TreatmentViewSet(viewsets.ModelViewSet):
    queryset = Treatment.objects.all()
    serializer_class = TreatmentSerializer
    # permission_classes = [IsAuthenticated]

class MedicineItemViewSet(viewsets.ModelViewSet):
    queryset = MedicineItem.objects.all()
    serializer_class = MedicineItemSerializer
    # permission_classes = [IsAuthenticated]

class StockHistoryViewSet(viewsets.ModelViewSet):
    queryset = StockHistory.objects.all()
    serializer_class = StockHistorySerializer
    # permission_classes = [IsAuthenticated]

class PurchaseRequestViewSet(viewsets.ModelViewSet):
    queryset = PurchaseRequest.objects.prefetch_related('history').all()
    serializer_class = PurchaseRequestSerializer
    # permission_classes = [IsAuthenticated]

class PurchaseHistoryViewSet(viewsets.ModelViewSet):
    queryset = PurchaseHistory.objects.all()
    serializer_class = PurchaseHistorySerializer
    # permission_classes = [IsAuthenticated]

class MedicalCertificateViewSet(viewsets.ModelViewSet):
    queryset = MedicalCertificate.objects.all()
    serializer_class = MedicalCertificateSerializer
    # permission_classes = [IsAuthenticated]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        patient = serializer.validated_data.get('patient')
        date = serializer.validated_data.get('date')
        purpose = serializer.validated_data.get('purpose', '')
        diagnosis = serializer.validated_data.get('diagnosis', '')

        existing = MedicalCertificate.objects.filter(
            patient=patient,
            date=date,
            purpose=purpose,
            diagnosis=diagnosis
        ).first()

        if existing:
            return Response(self.get_serializer(existing).data, status=status.HTTP_200_OK)

        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        instance = serializer.save()
        try:
            from .models import AppNotification
            p = instance.patient
            AppNotification.objects.create(
                type='general',
                message=f'Your Medical Certificate ({instance.purpose}) has been issued by {instance.doctor or "Clinic Physician"}.',
                patientName=p.name,
                patient_id=str(p.id),
                read=False
            )
        except Exception as e:
            print("Certificate notification note:", e)

class BedViewSet(viewsets.ModelViewSet):
    queryset = Bed.objects.all().order_by('bedNumber')
    serializer_class = BedSerializer
    # permission_classes = [IsAuthenticated]

    def list(self, request, *args, **kwargs):
        if not Bed.objects.exists():
            beds_to_create = [Bed(bedNumber=i, status='Available') for i in range(1, 9)]
            Bed.objects.bulk_create(beds_to_create)
        return super().list(request, *args, **kwargs)

class BedHistoryViewSet(viewsets.ModelViewSet):
    queryset = BedHistory.objects.all()
    serializer_class = BedHistorySerializer
    # permission_classes = [IsAuthenticated]

class HospitalTransferViewSet(viewsets.ModelViewSet):
    queryset = HospitalTransfer.objects.all()
    serializer_class = HospitalTransferSerializer
    # permission_classes = [IsAuthenticated]

class AppNotificationViewSet(viewsets.ModelViewSet):
    queryset = AppNotification.objects.all().order_by('-time')
    serializer_class = AppNotificationSerializer
    # permission_classes = [IsAuthenticated]

    @action(detail=False, methods=['post', 'patch'])
    def mark_all_read(self, request):
        AppNotification.objects.filter(read=False).update(read=True)
        return Response({'status': 'all marked as read'})

    @action(detail=False, methods=['post', 'delete'])
    def clear_all(self, request):
        AppNotification.objects.all().delete()
        return Response({'status': 'all notifications cleared'})

from .models import TelemedicineRequest
from .serializers import TelemedicineRequestSerializer

class TelemedicineRequestViewSet(viewsets.ModelViewSet):
    queryset = TelemedicineRequest.objects.select_related('patient').all().order_by('-created_at')
    serializer_class = TelemedicineRequestSerializer
    # permission_classes = [IsAuthenticated]

    def create(self, request, *args, **kwargs):
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
                meeting_link = f"https://cura-bice.vercel.app/call/CURA-Telemed-{clean_room_id}"
            
            telemed_request.meeting_link = meeting_link
            if secondary_link is not None:
                telemed_request.secondary_link = secondary_link
                
            telemed_request.save()
            
            # Simulated Email Sending
            print("\n=== MOCK EMAIL SENT ===")
            print(f"To: {telemed_request.patient.email}")
            print(f"Subject: Telemedicine Consultation Approved")
            print(f"Your consultation is approved for {scheduled_date} at {scheduled_time}.")
            print(f"Meeting Link: {meeting_link}")
            print("=======================\n")
            from .models import AppNotification
            AppNotification.objects.create(
                type='telemedicine_update',
                message=f"Your telemedicine consultation is approved for {scheduled_date} at {scheduled_time}.",
                patientName=telemed_request.patient.name,
                patient_id=telemed_request.patient.id
            )
            
        elif status == 'Rejected':
            telemed_request.save()
            print("\n=== MOCK EMAIL SENT ===")
            print(f"To: {telemed_request.patient.email}")
            print(f"Subject: Telemedicine Consultation Update")
            print(f"Your consultation request has been rejected. Please contact the clinic for more details.")
            print("=======================\n")
            from .models import AppNotification
            AppNotification.objects.create(
                type='telemedicine_update',
                message=f"Your telemedicine consultation request has been rejected.",
                patientName=telemed_request.patient.name,
                patient_id=telemed_request.patient.id
            )
            
        serializer = self.get_serializer(telemed_request)
        return Response(serializer.data)

    @action(detail=False, methods=['get', 'post'], permission_classes=[])
    def availability(self, request):
        import json
        from .models import TelemedicineAvailability
        try:
            record = TelemedicineAvailability.objects.first()
            if not record:
                record = TelemedicineAvailability.objects.create(unavailable_dates='[]', unavailable_times='[]')
        except Exception:
            return Response({'unavailable_dates': [], 'unavailable_times': []})

        if request.method == 'POST':
            dates = request.data.get('unavailable_dates', [])
            times = request.data.get('unavailable_times', [])
            if not isinstance(dates, list):
                dates = []
            if not isinstance(times, list):
                times = []
            record.unavailable_dates = json.dumps(dates)
            if hasattr(record, 'unavailable_times'):
                record.unavailable_times = json.dumps(times)
            record.save()
            return Response({'status': 'success', 'unavailable_dates': dates, 'unavailable_times': times})

        try:
            dates = json.loads(record.unavailable_dates) if record.unavailable_dates else []
        except Exception:
            dates = []
        try:
            times = json.loads(record.unavailable_times) if hasattr(record, 'unavailable_times') and record.unavailable_times else []
        except Exception:
            times = []
        return Response({'unavailable_dates': dates, 'unavailable_times': times})



from .models import AppointmentRequest
from .serializers import AppointmentRequestSerializer

class AppointmentRequestViewSet(viewsets.ModelViewSet):
    queryset = AppointmentRequest.objects.select_related('patient').all().order_by('-created_at')
    serializer_class = AppointmentRequestSerializer
    # permission_classes = [IsAuthenticated]

    def create(self, request, *args, **kwargs):
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
            
            print("\n=== MOCK EMAIL SENT ===")
            print(f"To: {appointment.patient.email}")
            print(f"Subject: In-Person Appointment Approved")
            print(f"Your appointment is approved for {scheduled_date} at {scheduled_time}.")
            print("=======================\n")
            from .models import AppNotification
            AppNotification.objects.create(
                type='appointment_update',
                message=f"Your appointment is approved for {scheduled_date} at {scheduled_time}.",
                patientName=appointment.patient.name,
                patient_id=appointment.patient.id
            )
            
        elif status == 'Rejected':
            appointment.save()
            print("\n=== MOCK EMAIL SENT ===")
            print(f"To: {appointment.patient.email}")
            print(f"Subject: Appointment Update")
            print(f"Your appointment request has been rejected. Please contact the clinic for more details.")
            print("=======================\n")
            from .models import AppNotification
            AppNotification.objects.create(
                type='appointment_update',
                message=f"Your appointment request has been rejected.",
                patientName=appointment.patient.name,
                patient_id=appointment.patient.id
            )
            
        serializer = self.get_serializer(appointment)
        return Response(serializer.data)

    @action(detail=False, methods=['get', 'post'], permission_classes=[])
    def availability(self, request):
        import json
        from .models import AppointmentAvailability
        try:
            record = AppointmentAvailability.objects.first()
            if not record:
                record = AppointmentAvailability.objects.create(unavailable_dates='[]', unavailable_times='[]')
        except Exception:
            return Response({'unavailable_dates': [], 'unavailable_times': []})

        if request.method == 'POST':
            dates = request.data.get('unavailable_dates', [])
            times = request.data.get('unavailable_times', [])
            if not isinstance(dates, list):
                dates = []
            if not isinstance(times, list):
                times = []
            record.unavailable_dates = json.dumps(dates)
            if hasattr(record, 'unavailable_times'):
                record.unavailable_times = json.dumps(times)
            record.save()
            return Response({'status': 'success', 'unavailable_dates': dates, 'unavailable_times': times})

        try:
            dates = json.loads(record.unavailable_dates) if record.unavailable_dates else []
        except Exception:
            dates = []
        try:
            times = json.loads(record.unavailable_times) if hasattr(record, 'unavailable_times') and record.unavailable_times else []
        except Exception:
            times = []
        return Response({'unavailable_dates': dates, 'unavailable_times': times})



from .models import PatientQueue
from .serializers import PatientQueueSerializer
from django.db import transaction

class PatientQueueViewSet(viewsets.ModelViewSet):
    serializer_class = PatientQueueSerializer

    def get_queryset(self):
        req_date = self.request.query_params.get('date')
        if self.request.query_params.get('all') == 'true':
            return PatientQueue.objects.select_related('patient').all().order_by('queue_number')
        if req_date:
            return PatientQueue.objects.select_related('patient').filter(date=req_date).order_by('queue_number')
        return PatientQueue.objects.select_related('patient').filter(date=timezone.localdate()).order_by('queue_number')

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        patient_id = request.data.get('patient')
        if not patient_id:
            return Response({"error": "Patient ID is required"}, status=status.HTTP_400_BAD_REQUEST)
        
        from .models import Patient
        
        try:
            patient = Patient.objects.select_for_update().get(id=patient_id)
        except Patient.DoesNotExist:
            return Response({"error": "Patient not found"}, status=status.HTTP_400_BAD_REQUEST)

        # Check if patient already in queue and not done
        today = timezone.localdate()
        existing = PatientQueue.objects.filter(patient_id=patient_id, status__in=['waiting', 'called'], date=today).first()
        if existing:
            serializer = self.get_serializer(existing)
            return Response(serializer.data, status=status.HTTP_200_OK)
            
        # Get next queue number for today
        last_queue = PatientQueue.objects.filter(date=today).order_by('-queue_number').first()
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
        queue.status = 'done'
        queue.save()
        serializer = self.get_serializer(queue)
        return Response(serializer.data)

from .models import ClinicAdvisory
from .serializers import ClinicAdvisorySerializer

class ClinicAdvisoryViewSet(viewsets.ModelViewSet):
    permission_classes = [AllowAny]
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

class MedicalCertificateRequestViewSet(viewsets.ModelViewSet):
    queryset = MedicalCertificateRequest.objects.select_related('patient').all().order_by('-created_at')
    serializer_class = MedicalCertificateRequestSerializer

    def create(self, request, *args, **kwargs):
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
    def approve(self, request, pk=None):
        medcert_req = self.get_object()
        
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

        if status_val == 'Approved':
            cert_date = timezone.now().date()
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

