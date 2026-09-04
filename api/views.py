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

        name_prefix = email.split('@')[0] if '@' in email else email
        
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
            patient = Patient.objects.get(email=user.email)
        except Patient.DoesNotExist:
            return Response({'error': 'Patient record not found for this user.'}, status=404)

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
                setattr(patient, field, data[field])
                
        # Also update the user's name if provided
        if 'name' in data:
            user.first_name = data['name']
            user.save()

        patient.save()

        # Update user's is_new status so they don't see onboarding again
        return Response({
            'message': 'Profile completed successfully.',
            'user': {'email': user.email, 'name': patient.name, 'is_new': False}
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
    queryset = AppNotification.objects.all()
    serializer_class = AppNotificationSerializer
    # permission_classes = [IsAuthenticated]

from .models import TelemedicineRequest
from .serializers import TelemedicineRequestSerializer
from rest_framework.decorators import action

class TelemedicineRequestViewSet(viewsets.ModelViewSet):
    queryset = TelemedicineRequest.objects.all().order_by('-created_at')
    serializer_class = TelemedicineRequestSerializer
    # permission_classes = [IsAuthenticated]

    @action(detail=True, methods=['patch'])
    def approve(self, request, pk=None):
        telemed_request = self.get_object()
        
        # Admin provides these fields in the request body
        scheduled_date = request.data.get('scheduled_date')
        scheduled_time = request.data.get('scheduled_time')
        meeting_link = request.data.get('meeting_link')
        status = request.data.get('status', 'Approved')
        
        telemed_request.status = status
        
        if status == 'Approved':
            if scheduled_date:
                telemed_request.scheduled_date = scheduled_date
            if scheduled_time:
                telemed_request.scheduled_time = scheduled_time
            if meeting_link:
                telemed_request.meeting_link = meeting_link
                
            telemed_request.save()
            
            # Simulated Email Sending
            print("\n=== MOCK EMAIL SENT ===")
            print(f"To: {telemed_request.patient.email}")
            print(f"Subject: Telemedicine Consultation Approved")
            print(f"Your consultation is approved for {scheduled_date} at {scheduled_time}.")
            print(f"Meeting Link: {meeting_link}")
            print("=======================\n")
            
        elif status == 'Rejected':
            telemed_request.save()
            print("\n=== MOCK EMAIL SENT ===")
            print(f"To: {telemed_request.patient.email}")
            print(f"Subject: Telemedicine Consultation Update")
            print(f"Your consultation request has been rejected. Please contact the clinic for more details.")
            print("=======================\n")
            
        serializer = self.get_serializer(telemed_request)
        return Response(serializer.data)

from .models import AppointmentRequest
from .serializers import AppointmentRequestSerializer

class AppointmentRequestViewSet(viewsets.ModelViewSet):
    queryset = AppointmentRequest.objects.all().order_by('-created_at')
    serializer_class = AppointmentRequestSerializer
    # permission_classes = [IsAuthenticated]

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
            
        elif status == 'Rejected':
            appointment.save()
            print("\n=== MOCK EMAIL SENT ===")
            print(f"To: {appointment.patient.email}")
            print(f"Subject: Appointment Update")
            print(f"Your appointment request has been rejected. Please contact the clinic for more details.")
            print("=======================\n")
            
        serializer = self.get_serializer(appointment)
        return Response(serializer.data)

