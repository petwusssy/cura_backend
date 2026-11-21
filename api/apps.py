import os
import sys
from django.apps import AppConfig


_MODEL_MIRRORS = {
    'Patient': ('patients', 'daily_report', 'weekly_census', 'cases_attended'),
    'Consultation': ('daily_report', 'weekly_census', 'cases_attended', 'non_consultation'),
    'Treatment': ('daily_report', 'non_consultation', 'inventory'),
    'MedicineItem': ('inventory',),
    'StockHistory': ('inventory',),
    'PurchaseRequest': ('purchases',),
    'PurchaseHistory': ('purchases',),
    'AppointmentRequest': ('appointments',),
    'TelemedicineRequest': ('telemedicine',),
    'MedicalCertificate': ('medcerts',),
}


def _on_model_change(sender, instance=None, **kwargs):
    if os.environ.get('DISABLE_DRIVE_MIRROR') or any(
        cmd in arg.lower() for arg in sys.argv for cmd in ('migrate', 'makemigrations', 'collectstatic', 'test')
    ):
        return
    keys = _MODEL_MIRRORS.get(sender.__name__)
    if not keys:
        return
    from django.db import transaction
    from .gdrive_service import schedule_mirror, trigger_live_consultation_sync
    cons_date = getattr(instance, 'date', None) if sender.__name__ == 'Consultation' else None
    def _dispatch():
        schedule_mirror(*keys)
        if cons_date and kwargs.get('signal') is not None:
            # Rebuild daily consultation ledger on post_delete as well
            from django.db.models.signals import post_delete
            if kwargs.get('signal') == post_delete:
                trigger_live_consultation_sync(instance)
    transaction.on_commit(_dispatch)


class ApiConfig(AppConfig):
    name = 'api'

    def ready(self):
        from django.db.models.signals import post_delete, post_save
        from . import models
        for model_name in _MODEL_MIRRORS:
            model = getattr(models, model_name, None)
            if model is not None:
                uid = f"cura_drive_mirror_{model_name}"
                post_save.connect(_on_model_change, sender=model, dispatch_uid=f"{uid}_save")
                post_delete.connect(_on_model_change, sender=model, dispatch_uid=f"{uid}_del")
