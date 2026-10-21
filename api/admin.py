from django.contrib import admin
from django.contrib.auth.models import User
from django.contrib.auth.admin import UserAdmin as DefaultUserAdmin
from .models import (
    Patient, Consultation, Treatment, MedicineItem, StockHistory,
    PurchaseRequest, PurchaseHistory, MedicalCertificate,
    Bed, BedHistory, HospitalTransfer, AppNotification,
    TelemedicineRequest, AppointmentRequest, PatientQueue,
    ClinicAdvisory, MedicalCertificateRequest, OTPVerification, LoginAttempt
)

class PatientAdmin(admin.ModelAdmin):
    search_fields = ('id', 'name', 'email')
    list_display = ('id', 'name', 'category', 'email', 'contact')

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            obj.delete()

try:
    admin.site.unregister(User)
except admin.sites.NotRegistered:
    pass

@admin.register(User)
class CustomUserAdmin(DefaultUserAdmin):
    def delete_queryset(self, request, queryset):
        for obj in queryset:
            obj.delete()

admin.site.register(Patient, PatientAdmin)
admin.site.register(Consultation)
admin.site.register(Treatment)
admin.site.register(MedicineItem)
admin.site.register(StockHistory)
admin.site.register(PurchaseRequest)
admin.site.register(PurchaseHistory)
admin.site.register(MedicalCertificate)
admin.site.register(Bed)
admin.site.register(BedHistory)
admin.site.register(HospitalTransfer)
admin.site.register(AppNotification)
admin.site.register(TelemedicineRequest)
admin.site.register(AppointmentRequest)
admin.site.register(PatientQueue)
admin.site.register(ClinicAdvisory)
admin.site.register(MedicalCertificateRequest)
admin.site.register(OTPVerification)
admin.site.register(LoginAttempt)
