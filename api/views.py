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
    Bed, BedHistory, HospitalTransfer, AppNotification, OTPVerification
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
            # Using the web client ID provided by the user
            CLIENT_ID = "289437991360-4ge9cgmpvjmr68pfprsvfimau1i4batr.apps.googleusercontent.com"
            idinfo = id_token.verify_oauth2_token(token, requests.Request(), CLIENT_ID)

            # ID token is valid. Get the user's email from the decoded token.
            email = idinfo.get('email')
            first_name = idinfo.get('given_name', '')
            last_name = idinfo.get('family_name', '')

            # Check if user exists, if not create one
            user, created = User.objects.get_or_create(username=email, defaults={
                'email': email,
                'first_name': first_name,
                'last_name': last_name
            })

            # Check if Patient profile exists, if not create one
            patient, patient_created = Patient.objects.get_or_create(email=email, defaults={
                'firstName': first_name,
                'lastName': last_name,
                'contactNumber': '',
                'classification': 'Outsider'
            })

            # Generate JWT tokens for the user
            refresh = RefreshToken.for_user(user)
            refresh['username'] = user.username
            refresh['roles'] = list(user.groups.values_list('name', flat=True))
            if user.is_superuser:
                refresh['roles'].append('Admin')

            return Response({
                'refresh': str(refresh),
                'access': str(refresh.access_token),
                'user': {
                    'email': user.email,
                    'first_name': user.first_name,
                    'last_name': user.last_name,
                    'is_new': created
                }
            }, status=status.HTTP_200_OK)

        except ValueError as e:
            # Invalid token
            return Response({'error': 'Invalid token', 'details': str(e)}, status=status.HTTP_401_UNAUTHORIZED)


class CheckEmailView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        email = request.data.get('email')
        if not email:
            return Response({'error': 'Email is required'}, status=400)
        exists = Patient.objects.filter(email=email).exists()
        claimed = User.objects.filter(username=email).exists()
        return Response({'exists': exists, 'claimed': claimed})

class RequestOTPView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        email = request.data.get('email')
        if not email:
            return Response({'error': 'Email is required'}, status=400)
        
        patient_exists = Patient.objects.filter(email=email).exists()
        if not patient_exists:
            return Response({'error': 'Patient not found'}, status=404)
        
        otp = str(random.randint(100000, 999999))
        OTPVerification.objects.update_or_create(
            email=email,
            defaults={
                'otp': otp,
                'expires_at': timezone.now() + timedelta(minutes=10),
                'is_verified': False
            }
        )
        
        print(f"\n=== MOCK EMAIL SENT ===")
        print(f"To: {email}")
        print(f"OTP: {otp}")
        print(f"=======================\n")
        
        return Response({'message': 'OTP sent successfully'})

class VerifyOTPView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        email = request.data.get('email')
        otp = request.data.get('otp')
        
        try:
            verification = OTPVerification.objects.get(email=email, otp=otp)
            if timezone.now() > verification.expires_at:
                return Response({'error': 'OTP expired'}, status=400)
            
            verification.is_verified = True
            verification.save()
            return Response({'message': 'OTP verified'})
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
            
        if User.objects.filter(username=email).exists():
            return Response({'error': 'User already exists'}, status=400)
            
        # Create User
        user = User.objects.create_user(username=email, email=email, password=password)
        
        # Auto-infer category based on email
        if email.endswith('.student@ua.edu.ph'):
            category = 'Student'
        elif email.endswith('@ua.edu.ph'):
            category = 'Employee'
        else:
            category = 'Outsider'

        name_prefix = (email.split('@')[0] if '@' in email else email).upper()
        
        Patient.objects.create(
            id=uuid.uuid4().hex[:8], # Short UUID for ID
            name=name_prefix,
            category=category,
            contact='Not Provided',
            birthday='2000-01-01', # Placeholder
            age=0,
            email=email
        )
        
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
        email = request.data.get('email')
        password = request.data.get('password')
        
        try:
            verification = OTPVerification.objects.get(email=email, is_verified=True)
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

            patient = Patient.objects.filter(email=email).first()
            patient_name = patient.name if patient else email.split('@')[0]
            return Response({
                'refresh': str(refresh),
                'access': str(refresh.access_token),
                'user': {'email': user.email, 'name': patient_name, 'is_new': False}
            })
            
        except OTPVerification.DoesNotExist:
            return Response({'error': 'Email not verified or session expired'}, status=400)

def health(request):
    return JsonResponse({
        "status": "ok",
        "message": "CURA Backend Running"
    })

class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
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
        username_or_email = attrs.get(self.username_field)
        if username_or_email:
            username_or_email = str(username_or_email).strip()
            from django.db.models import Q
            matched_user = User.objects.filter(
                Q(username__iexact=username_or_email) | Q(email__iexact=username_or_email)
            ).first()
            if matched_user:
                attrs[self.username_field] = matched_user.username

        data = super().validate(attrs)
        roles = list(self.user.groups.values_list('name', flat=True))
        if self.user.is_superuser:
            roles.append('Admin')
        data['roles'] = roles
        data['username'] = self.user.username
        return data

class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer

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

            # Include user data so mobile client can display name/email
            username = response.data.get('username', '')
            user_obj = User.objects.filter(username=username).first()
            if user_obj:
                patient = Patient.objects.filter(email__iexact=user_obj.email or username).first()
                patient_name = patient.name if patient else (user_obj.first_name or username.split('@')[0]).upper()
                response.data['user'] = {
                    'email': user_obj.email or username,
                    'name': patient_name,
                    'is_new': False
                }

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

    def list(self, request, *args, **kwargs):
        # Auto-clean duplicate records in database
        seen = set()
        duplicates_to_delete = []
        for c in Consultation.objects.all().order_by('id'):
            key = (str(c.patient_id), str(c.date), str(c.timeIn), str(c.complaint or '').strip(), str(c.status))
            if key in seen:
                duplicates_to_delete.append(c.id)
            else:
                seen.add(key)

        if duplicates_to_delete:
            Consultation.objects.filter(id__in=duplicates_to_delete).delete()

        return super().list(request, *args, **kwargs)

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
    queryset = PurchaseRequest.objects.all()
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

    def list(self, request, *args, **kwargs):
        seen = set()
        duplicates_to_delete = []
        for cert in MedicalCertificate.objects.all().order_by('id'):
            key = (str(cert.patient_id), str(cert.date), str(cert.purpose or '').strip())
            if key in seen:
                duplicates_to_delete.append(cert.id)
            else:
                seen.add(key)
        if duplicates_to_delete:
            MedicalCertificate.objects.filter(id__in=duplicates_to_delete).delete()

        return super().list(request, *args, **kwargs)

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
    queryset = TelemedicineRequest.objects.all().order_by('-created_at')
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

from .models import AppointmentRequest
from .serializers import AppointmentRequestSerializer

class AppointmentRequestViewSet(viewsets.ModelViewSet):
    queryset = AppointmentRequest.objects.all().order_by('-created_at')
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

from .models import PatientQueue
from .serializers import PatientQueueSerializer
from django.db import transaction

class PatientQueueViewSet(viewsets.ModelViewSet):
    serializer_class = PatientQueueSerializer

    def get_queryset(self):
        req_date = self.request.query_params.get('date')
        if self.request.query_params.get('all') == 'true':
            return PatientQueue.objects.all().order_by('queue_number')
        if req_date:
            return PatientQueue.objects.filter(date=req_date).order_by('queue_number')
        return PatientQueue.objects.filter(date=timezone.localdate()).order_by('queue_number')

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

