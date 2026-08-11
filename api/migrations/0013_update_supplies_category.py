from django.db import migrations

def update_categories(apps, schema_editor):
    MedicineItem = apps.get_model('api', 'MedicineItem')
    supplies = [
        "Adhesive steristrips packs 1/2\"x4\"", "Absorbent cotton in balls/pack", "Arm sling orthopedic small", "Arm sling orthopedic medium", "Arm sling orthopedic large", "Arm sling orthopedic x-large", "Band-Aid 50 strips/box", "Betadine 10% solution 120mL", "Betadine gargle 1% oral antiseptic", "Bactidol gargle 0.1% solution", "Disposable syringe with needle 3mL", "Disposable syringe with needle 5mL", "Disposable syringe with needle 1mL", "Efficascent oil ES 100mL", "Efficascent oil regular 100mL", "Elasctic bandage 2\"", "Elasctic bandage 3\"", "Elasctic bandage 4\"", "Individually packed OS 2x2", "Individually packed OS 4x4", "Micropore plaster 1 inch", "Nebulizing kit ADULT", "Nebulizing kit PEDIA", "Non-Rebreathing Mask Adult", "NSS 1L for irrigation", "Omega pain killer", "Oxygen cannula/mask ADULT", "Oxygen cannula PEDIA", "Salonpas 10pcs/pack x2", "Tongue depressors", "ALCOHOL GREENCROSS", "KN95 MASK 50 PCS/BOX"
    ]
    
    MedicineItem.objects.filter(name__in=supplies).update(category='Supply')

def reverse_update(apps, schema_editor):
    MedicineItem = apps.get_model('api', 'MedicineItem')
    supplies = [
        "Adhesive steristrips packs 1/2\"x4\"", "Absorbent cotton in balls/pack", "Arm sling orthopedic small", "Arm sling orthopedic medium", "Arm sling orthopedic large", "Arm sling orthopedic x-large", "Band-Aid 50 strips/box", "Betadine 10% solution 120mL", "Betadine gargle 1% oral antiseptic", "Bactidol gargle 0.1% solution", "Disposable syringe with needle 3mL", "Disposable syringe with needle 5mL", "Disposable syringe with needle 1mL", "Efficascent oil ES 100mL", "Efficascent oil regular 100mL", "Elasctic bandage 2\"", "Elasctic bandage 3\"", "Elasctic bandage 4\"", "Individually packed OS 2x2", "Individually packed OS 4x4", "Micropore plaster 1 inch", "Nebulizing kit ADULT", "Nebulizing kit PEDIA", "Non-Rebreathing Mask Adult", "NSS 1L for irrigation", "Omega pain killer", "Oxygen cannula/mask ADULT", "Oxygen cannula PEDIA", "Salonpas 10pcs/pack x2", "Tongue depressors", "ALCOHOL GREENCROSS", "KN95 MASK 50 PCS/BOX"
    ]
    MedicineItem.objects.filter(name__in=supplies).update(category='Medicine')


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0012_medicineitem_category'),
    ]

    operations = [
        migrations.RunPython(update_categories, reverse_update)
    ]
