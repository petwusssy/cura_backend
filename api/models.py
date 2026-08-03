import uuid
from django.db import models
from django.contrib.auth.models import User

class Patient(models.Model):
    CATEGORY_CHOICES = [
        ('Student', 'Student'),
        ('Employee', 'Employee'),
        ('Outsider', 'Outsider'),
    ]

    id = models.CharField(max_length=50, primary_key=True)
    name = models.CharField(max_length=255)
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES)
    contact = models.CharField(max_length=50)
    birthday = models.DateField()
    age = models.IntegerField()
    sex = models.CharField(max_length=20, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    emergencyContact = models.CharField(max_length=255, blank=True, null=True)
    emergencyPhone = models.CharField(max_length=50, blank=True, null=True)
    course = models.CharField(max_length=100, blank=True, null=True)
    yearLevel = models.CharField(max_length=50, blank=True, null=True)
    position = models.CharField(max_length=100, blank=True, null=True)
    department = models.CharField(max_length=100, blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    studentCategory = models.CharField(max_length=50, blank=True, null=True)
    guardianName = models.CharField(max_length=255, blank=True, null=True)
    gradeLevel = models.CharField(max_length=50, blank=True, null=True)

    def __str__(self):
        return self.name

class Consultation(models.Model):
    STATUS_CHOICES = [
        ('Consultation', 'Consultation'),
        ('Non-Consultation', 'Non-Consultation'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name='consultations')
    date = models.DateField()
    timeIn = models.TimeField()
    timeOut = models.TimeField(blank=True, null=True)
    complaint = models.TextField()
    categories = models.JSONField(default=list)
    doctorConsulted = models.BooleanField(default=False)
    doctorName = models.CharField(max_length=255, blank=True, null=True)
    whoConsulted = models.CharField(max_length=255, blank=True, null=True)
    vitals = models.JSONField(default=dict, blank=True)
    earlyDismissal = models.BooleanField(default=False)
    earlyDismissalReason = models.TextField(blank=True, null=True)
    fetcherName = models.CharField(max_length=255, blank=True, null=True)
    nurseNotes = models.TextField(blank=True, null=True)
    recommendations = models.TextField(blank=True, null=True)
    followUp = models.CharField(max_length=255, blank=True, null=True)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='Consultation')
    prescriptionImage = models.TextField(blank=True, null=True)
    transferred = models.BooleanField(default=False)
    dismissalDestination = models.CharField(max_length=50, blank=True, null=True)
    fetcherIdImage = models.TextField(blank=True, null=True)
    purposeOfVisit = models.CharField(max_length=255, blank=True, null=True)
    operationalNotes = models.TextField(blank=True, null=True)
    assistingNurse = models.CharField(max_length=255, blank=True, null=True)

    def __str__(self):
        return f"{self.patient.name} - {self.date}"

class Treatment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    consultation = models.ForeignKey(Consultation, on_delete=models.CASCADE, related_name='treatments')
    medicineName = models.CharField(max_length=255)
    quantity = models.IntegerField()
    unit = models.CharField(max_length=50)
    timeGiven = models.TimeField()
    nextDose = models.TimeField(blank=True, null=True)
    remarks = models.TextField(blank=True, null=True)

class MedicineItem(models.Model):
    STATUS_CHOICES = [
        ('Normal', 'Normal'),
        ('Low Stock', 'Low Stock'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    stock = models.IntegerField(default=0)
    unit = models.CharField(max_length=50)
    dateAdded = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='Normal')

    def __str__(self):
        return self.name

class StockHistory(models.Model):
    TYPE_CHOICES = [
        ('add', 'add'),
        ('dispense', 'dispense'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    medicine = models.ForeignKey(MedicineItem, on_delete=models.CASCADE, related_name='stockHistory')
    date = models.DateTimeField(auto_now_add=True)
    qty = models.IntegerField()
    type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    note = models.TextField(blank=True, null=True)

class PurchaseRequest(models.Model):
    STATUS_CHOICES = [
        ('Pending', 'Pending'),
        ('Partial', 'Partial'),
        ('Complete', 'Complete'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    medicine = models.CharField(max_length=255)
    requestedQty = models.IntegerField()
    receivedQty = models.IntegerField(default=0)
    date = models.DateField()
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='Pending')

class PurchaseHistory(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(PurchaseRequest, on_delete=models.CASCADE, related_name='history')
    date = models.DateField()
    qty = models.IntegerField()
    note = models.TextField(blank=True, null=True)

class MedicalCertificate(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE)
    date = models.DateField()
    purpose = models.CharField(max_length=255)
    diagnosis = models.TextField(blank=True, null=True)
    recommendation = models.TextField(blank=True, null=True)
    doctor = models.CharField(max_length=255, blank=True, null=True)
    issuedBy = models.CharField(max_length=255, blank=True, null=True)
    notes = models.TextField(blank=True, null=True)

class Bed(models.Model):
    STATUS_CHOICES = [
        ('Available', 'Available'),
        ('Occupied', 'Occupied'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    bedNumber = models.IntegerField(unique=True)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='Available')
    patientName = models.CharField(max_length=255, blank=True, null=True)
    patientId = models.CharField(max_length=255, blank=True, null=True) # Soft link to allow ad-hoc entries
    timeOccupied = models.DateTimeField(blank=True, null=True)

class BedHistory(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    bed = models.ForeignKey(Bed, on_delete=models.CASCADE, related_name='history')
    patientName = models.CharField(max_length=255, blank=True, null=True)
    patientId = models.CharField(max_length=255, blank=True, null=True)
    date = models.DateField()
    timeIn = models.TimeField()
    timeOut = models.TimeField()
    duration = models.CharField(max_length=50)

class HospitalTransfer(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    consultation = models.ForeignKey(Consultation, on_delete=models.CASCADE)
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE)
    date = models.DateField()
    time = models.TimeField()
    receivingHospital = models.CharField(max_length=255)
    reason = models.TextField()
    transportMode = models.CharField(max_length=255)
    notes = models.TextField(blank=True, null=True)
    transferredBy = models.CharField(max_length=255)

class AppNotification(models.Model):
    TYPE_CHOICES = [
        ('medication', 'medication'),
        ('bed', 'bed'),
        ('general', 'general'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    type = models.CharField(max_length=50, choices=TYPE_CHOICES)
    message = models.TextField()
    time = models.DateTimeField(auto_now_add=True)
    read = models.BooleanField(default=False)
    patientName = models.CharField(max_length=255, blank=True, null=True)
    nextDose = models.TimeField(blank=True, null=True)
    minutesLeft = models.IntegerField(blank=True, null=True)
