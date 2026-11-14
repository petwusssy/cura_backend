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
    name = models.CharField(max_length=255, db_index=True)
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES, db_index=True)
    contact = models.CharField(max_length=50)
    birthday = models.DateField()
    age = models.IntegerField()
    sex = models.CharField(max_length=20, blank=True, null=True)
    email = models.EmailField(blank=True, null=True, db_index=True)
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
    avatar = models.TextField(blank=True, null=True)

    def save(self, *args, **kwargs):
        if self.name:
            self.name = self.name.strip().upper()
        if self.guardianName:
            self.guardianName = self.guardianName.strip().upper()
        if self.emergencyContact:
            self.emergencyContact = self.emergencyContact.strip().upper()
        if self.avatar and str(self.avatar).strip().startswith('data:image/'):
            from .gdrive_service import offload_image
            label = f"{self.id}_{self.name}".strip('_') if self.id or self.name else 'patient'
            self.avatar = offload_image(self.avatar, category='avatar', label=label)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

class Consultation(models.Model):
    STATUS_CHOICES = [
        ('Consultation', 'Consultation'),
        ('Non-Consultation', 'Non-Consultation'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name='consultations')
    date = models.DateField(db_index=True)
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
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='Consultation', db_index=True)
    prescriptionImage = models.TextField(blank=True, null=True)
    transferred = models.BooleanField(default=False)
    dismissalDestination = models.CharField(max_length=50, blank=True, null=True)
    fetcherIdImage = models.TextField(blank=True, null=True)
    purposeOfVisit = models.CharField(max_length=255, blank=True, null=True)
    operationalNotes = models.TextField(blank=True, null=True)
    assistingNurse = models.CharField(max_length=255, blank=True, null=True)

    def save(self, *args, **kwargs):
        from .gdrive_service import offload_image
        patient = getattr(self, 'patient', None) if self.patient_id else None
        patient_label = f"{self.patient_id}_{getattr(patient, 'name', '')}".strip('_') if self.patient_id else ''
        if self.prescriptionImage and str(self.prescriptionImage).strip().startswith('data:image/'):
            self.prescriptionImage = offload_image(self.prescriptionImage, category='prescription', label=patient_label or 'rx')
        if self.fetcherIdImage and str(self.fetcherIdImage).strip().startswith('data:image/'):
            self.fetcherIdImage = offload_image(self.fetcherIdImage, category='fetcher_id', label=patient_label or 'id')
        super().save(*args, **kwargs)

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
        ('Out of Stock', 'Out of Stock'),
        ('Healthy', 'Healthy'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, db_index=True)
    stock = models.IntegerField(default=0)
    unit = models.CharField(max_length=50)
    dateAdded = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='Normal', db_index=True)
    batchNumber = models.CharField(max_length=100, blank=True, null=True)
    beginningQty = models.IntegerField(blank=True, null=True)
    dispensed = models.IntegerField(default=0)
    category = models.CharField(max_length=50, default='Medicine')
    threshold = models.IntegerField(default=15)

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
    description = models.CharField(max_length=255, blank=True, null=True)
    unit = models.CharField(max_length=100, blank=True, null=True, default='Tablet')
    prfNo = models.CharField(max_length=100, blank=True, null=True)
    unitPrice = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)

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
    reason = models.CharField(max_length=255, blank=True, null=True)
    allottedTime = models.IntegerField(blank=True, null=True)

    def save(self, *args, **kwargs):
        if self.patientName:
            self.patientName = self.patientName.strip().upper()
        super().save(*args, **kwargs)

class BedHistory(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    bed = models.ForeignKey(Bed, on_delete=models.CASCADE, related_name='history')
    patientName = models.CharField(max_length=255, blank=True, null=True)
    patientId = models.CharField(max_length=255, blank=True, null=True)
    date = models.DateField()
    timeIn = models.TimeField()
    timeOut = models.TimeField()
    duration = models.CharField(max_length=50)
    reason = models.CharField(max_length=255, blank=True, null=True)

    def save(self, *args, **kwargs):
        if self.patientName:
            self.patientName = self.patientName.strip().upper()
        super().save(*args, **kwargs)

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
        ('telemedicine_request', 'telemedicine_request'),
        ('appointment_request', 'appointment_request'),
        ('telemedicine_update', 'telemedicine_update'),
        ('appointment_update', 'appointment_update'),
        ('certificate', 'certificate'),
        ('medcert_request', 'medcert_request'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    type = models.CharField(max_length=50, choices=TYPE_CHOICES, db_index=True)
    message = models.TextField()
    time = models.DateTimeField(auto_now_add=True, db_index=True)
    read = models.BooleanField(default=False, db_index=True)
    patientName = models.CharField(max_length=255, blank=True, null=True)
    patient_id = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    nextDose = models.TimeField(blank=True, null=True)
    minutesLeft = models.IntegerField(blank=True, null=True)

    def save(self, *args, **kwargs):
        if self.patientName:
            self.patientName = self.patientName.strip().upper()
        super().save(*args, **kwargs)

class OTPVerification(models.Model):
    email = models.EmailField(db_index=True)
    otp = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    is_verified = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.email} - {self.otp}"

class TelemedicineRequest(models.Model):
    STATUS_CHOICES = [
        ('Pending', 'Pending'),
        ('Approved', 'Approved'),
        ('Rejected', 'Rejected'),
        ('Completed', 'Completed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name='telemedicine_requests')
    preferred_date = models.DateField()
    preferred_time = models.CharField(max_length=50)
    reason = models.TextField()
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='Pending')
    
    # Fields filled by Admin upon approval
    scheduled_date = models.DateField(blank=True, null=True)
    scheduled_time = models.CharField(max_length=50, blank=True, null=True)
    meeting_link = models.URLField(max_length=500, blank=True, null=True)
    secondary_link = models.URLField(max_length=500, blank=True, null=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Telemed - {self.patient.name} ({self.status})"

class TelemedicineAvailability(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    unavailable_dates = models.TextField(default='[]', blank=True)
    unavailable_times = models.TextField(default='[]', blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"TelemedicineAvailability ({self.updated_at})"


class AppointmentRequest(models.Model):
    STATUS_CHOICES = [
        ('Pending', 'Pending'),
        ('Approved', 'Approved'),
        ('Rejected', 'Rejected'),
        ('Completed', 'Completed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name='appointment_requests')
    visit_type = models.CharField(max_length=100) # e.g. medical, dental, clearance
    preferred_date = models.DateField()
    preferred_time = models.CharField(max_length=50)
    reason = models.TextField()
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='Pending')
    
    # Fields filled by Admin upon approval
    scheduled_date = models.DateField(blank=True, null=True)
    scheduled_time = models.CharField(max_length=50, blank=True, null=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Appointment - {self.patient.name} ({self.status})"

class AppointmentAvailability(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    unavailable_dates = models.TextField(default='[]', blank=True)
    unavailable_times = models.TextField(default='[]', blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"AppointmentAvailability ({self.updated_at})"



class PatientQueue(models.Model):
    STATUS_CHOICES = [
        ('waiting', 'waiting'),
        ('called', 'called'),
        ('done', 'done'),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name='queues')
    queue_number = models.IntegerField()
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='waiting', db_index=True)
    date = models.DateField(auto_now_add=True, db_index=True)
    time_added = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"Queue #{self.queue_number} - {self.patient.name} ({self.status})"

class ClinicAdvisory(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    status = models.CharField(max_length=50, default='Closed')
    message = models.TextField(default='Welcome to the University Clinic! Standard operating hours are 8:00 AM to 5:00 PM.')
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Advisory ({self.status}) - {self.message[:30]}"

class MedicalCertificateRequest(models.Model):
    STATUS_CHOICES = [
        ('Pending', 'Pending'),
        ('Approved', 'Approved'),
        ('Rejected', 'Rejected'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name='medcert_requests')
    purpose = models.CharField(max_length=255)
    complaint = models.TextField()
    start_date = models.DateField(blank=True, null=True)
    end_date = models.DateField(blank=True, null=True)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='Pending')
    diagnosis = models.TextField(blank=True, null=True)
    recommendations = models.TextField(blank=True, null=True)
    doctor = models.CharField(max_length=255, blank=True, null=True)
    remarks = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"MedCert Request - {self.patient.name} ({self.status})"

class LoginAttempt(models.Model):
    identifier = models.CharField(max_length=255, unique=True, db_index=True)
    failed_attempts = models.IntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    last_attempt_at = models.DateTimeField(auto_now=True)

    def is_locked(self):
        if self.locked_until:
            from django.utils import timezone
            return timezone.now() < self.locked_until
        return False

    def remaining_seconds(self):
        if self.locked_until:
            from django.utils import timezone
            now = timezone.now()
            if now < self.locked_until:
                return max(0, int((self.locked_until - now).total_seconds()))
        return 0

    def __str__(self):
        return f"LoginAttempt({self.identifier}, fails={self.failed_attempts})"

from django.db.models.signals import pre_save, post_delete
from django.dispatch import receiver

@receiver(pre_save, sender=User)
def ensure_superuser_is_staff(sender, instance, **kwargs):
    if instance.is_superuser and not instance.is_staff:
        instance.is_staff = True

@receiver(post_delete, sender=Patient)
def cleanup_on_patient_delete(sender, instance, **kwargs):
    user_pks = set()
    if instance.email:
        user_pks.update(User.objects.filter(is_staff=False, is_superuser=False, email__iexact=instance.email).values_list('pk', flat=True))
        user_pks.update(User.objects.filter(is_staff=False, is_superuser=False, username__iexact=instance.email).values_list('pk', flat=True))
    if instance.id:
        user_pks.update(User.objects.filter(is_staff=False, is_superuser=False, username__iexact=str(instance.id)).values_list('pk', flat=True))
    if user_pks:
        User.objects.filter(pk__in=user_pks).delete()

    if instance.id:
        AppNotification.objects.filter(patient_id=str(instance.id)).delete()
        Bed.objects.filter(patientId=str(instance.id)).update(
            status='Available', patientName=None, patientId=None, timeOccupied=None, reason=None, allottedTime=None
        )
        BedHistory.objects.filter(patientId=str(instance.id)).delete()
        LoginAttempt.objects.filter(identifier__iexact=str(instance.id)).delete()

    if instance.name:
        AppNotification.objects.filter(patientName__iexact=instance.name).delete()

    if instance.email:
        OTPVerification.objects.filter(email__iexact=instance.email).delete()
        LoginAttempt.objects.filter(identifier__iexact=instance.email).delete()

@receiver(post_delete, sender=User)
def cleanup_on_user_delete(sender, instance, **kwargs):
    if not instance.is_staff and not instance.is_superuser:
        patient_pks = set()
        if instance.email:
            patient_pks.update(Patient.objects.filter(email__iexact=instance.email).values_list('pk', flat=True))
        if instance.username:
            patient_pks.update(Patient.objects.filter(id__iexact=instance.username).values_list('pk', flat=True))
            patient_pks.update(Patient.objects.filter(email__iexact=instance.username).values_list('pk', flat=True))

        for pid in patient_pks:
            p = Patient.objects.filter(pk=pid).first()
            if p:
                p.delete()

        if instance.email:
            OTPVerification.objects.filter(email__iexact=instance.email).delete()
            LoginAttempt.objects.filter(identifier__iexact=instance.email).delete()
        if instance.username:
            LoginAttempt.objects.filter(identifier__iexact=instance.username).delete()


class ClinicStaff(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=150)
    role = models.CharField(max_length=80, default='Staff Nurse')
    pin_hash = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.role})"


class AuditLog(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    staff = models.ForeignKey(ClinicStaff, on_delete=models.SET_NULL, null=True, blank=True, related_name='audit_logs')
    staff_name_snapshot = models.CharField(max_length=150)
    staff_role_snapshot = models.CharField(max_length=80, blank=True, default='Staff Nurse')
    action = models.CharField(max_length=100, db_index=True)
    target_type = models.CharField(max_length=80, blank=True, default='')
    target_id = models.CharField(max_length=120, blank=True, default='')
    details = models.TextField(blank=True, default='')
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-timestamp']

    def save(self, *args, **kwargs):
        is_new = self._state.adding
        super().save(*args, **kwargs)
        if is_new and self.action != 'SYSTEM_GDRIVE_CONFIG':
            from .gdrive_service import trigger_live_audit_sync
            trigger_live_audit_sync(self)

    def __str__(self):
        return f"[{self.timestamp}] {self.staff_name_snapshot}: {self.action}"




