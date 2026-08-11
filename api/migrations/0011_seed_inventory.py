from django.db import migrations
import json
import os

def seed_inventory(apps, schema_editor):
    MedicineItem = apps.get_model('api', 'MedicineItem')
    items = [
        "Allerta 10mg tab", "Allerkid 60mL bottle", "Alnix 10mg tab", "Aspilets-EC 80mg tab", "Benadryl 25mg", "Benadryl 50mg", "Benadryl 60mL bottle", "Bioflu tab", "Biogesic 500mg tab", "Budecort respules 250mcg/mL", "Buscopan 10mg tab", "Buscopan Plus", "Calmoseptine ointment", "Catapres 75mcg", "Celecoxib 200mg capsule", "Dolcet 37.5mg/325mg tab", "Dolfenal 500mg tab", "Duavent nebules", "Erceflora Niblet", "Erythromycin ointment tubes", "Flotera chewable", "Gaviscon sachet", "Gaviscon tablet", "Hidrasec 30mg granules", "Hydrite sachet", "Hypromellose 3mg/mL drops", "Imodium 2mg cap", "Isordil SL 5mg tab", "Kramil-S chewable pink", "Kramil-S ADVANCE", "Motilium 10mg", "Nafarin A", "Norvasc 10mg tab", "Omeprazole 20mg cap", "Omeprazole 40mg cap", "Panto Plus cap", "Paracetamol syrup", "Plavix 75mg tab", "Ranitidine 150mg tab", "Serc 16mg cap", "Sinupret tab", "Strepsils lozenges (8xpack)", "Ventolin nebules",
        "Adhesive steristrips packs 1/2\"x4\"", "Absorbent cotton in balls/pack", "Arm sling orthopedic small", "Arm sling orthopedic medium", "Arm sling orthopedic large", "Arm sling orthopedic x-large", "Band-Aid 50 strips/box", "Betadine 10% solution 120mL", "Betadine gargle 1% oral antiseptic", "Bactidol gargle 0.1% solution", "Disposable syringe with needle 3mL", "Disposable syringe with needle 5mL", "Disposable syringe with needle 1mL", "Efficascent oil ES 100mL", "Efficascent oil regular 100mL", "Elasctic bandage 2\"", "Elasctic bandage 3\"", "Elasctic bandage 4\"", "Individually packed OS 2x2", "Individually packed OS 4x4", "Micropore plaster 1 inch", "Nebulizing kit ADULT", "Nebulizing kit PEDIA", "Non-Rebreathing Mask Adult", "NSS 1L for irrigation", "Omega pain killer", "Oxygen cannula/mask ADULT", "Oxygen cannula PEDIA", "Salonpas 10pcs/pack x2", "Tongue depressors", "ALCOHOL GREENCROSS", "KN95 MASK 50 PCS/BOX"
    ]
    
    for name in items:
        if not MedicineItem.objects.filter(name=name).exists():
            unit = 'Piece'
            if 'mL' in name or 'bottle' in name.lower():
                unit = 'Bottle'
            elif 'sachet' in name.lower() or 'pack' in name.lower():
                unit = 'Pack'
            elif 'tab' in name.lower() or 'cap' in name.lower():
                unit = 'Tablet/Capsule'
            elif 'tube' in name.lower() or 'ointment' in name.lower():
                unit = 'Tube'
                
            MedicineItem.objects.create(
                name=name,
                stock=0,
                unit=unit,
                status='Out of Stock',
                threshold=15
            )

def reverse_seed(apps, schema_editor):
    pass # No need to delete them as they might be used

class Migration(migrations.Migration):

    dependencies = [
        ('api', '0010_medicineitem_batchnumber_medicineitem_beginningqty_and_more'),
    ]

    operations = [
        migrations.RunPython(seed_inventory, reverse_seed),
    ]
