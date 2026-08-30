from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views
from .views import CustomTokenObtainPairView, CustomTokenRefreshView, LogoutView

router = DefaultRouter()
router.register(r'patients', views.PatientViewSet)
router.register(r'consultations', views.ConsultationViewSet)
router.register(r'treatments', views.TreatmentViewSet)
router.register(r'medicines', views.MedicineItemViewSet)
router.register(r'stock-history', views.StockHistoryViewSet)
router.register(r'purchase-requests', views.PurchaseRequestViewSet)
router.register(r'purchase-history', views.PurchaseHistoryViewSet)
router.register(r'certificates', views.MedicalCertificateViewSet)
router.register(r'beds', views.BedViewSet)
router.register(r'bed-history', views.BedHistoryViewSet)
router.register(r'hospital-transfers', views.HospitalTransferViewSet)
router.register(r'notifications', views.AppNotificationViewSet)

urlpatterns = [
    path('health/', views.health, name='health'),
    path('auth/login/', CustomTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('auth/refresh/', CustomTokenRefreshView.as_view(), name='token_refresh'),
    path('auth/logout/', LogoutView.as_view(), name='auth_logout'),
    path('auth/google/', views.GoogleLoginView.as_view(), name='google_login'),
    path('', include(router.urls)),
]
