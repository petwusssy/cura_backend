from django.contrib import admin
from .models import (
    Patient, Consultation, Treatment, MedicineItem, StockHistory,
    PurchaseRequest, PurchaseHistory, MedicalCertificate,
    Bed, BedHistory, HospitalTransfer, AppNotification
)

admin.site.register(Patient)
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
