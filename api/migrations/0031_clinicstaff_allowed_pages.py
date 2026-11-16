from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0030_patient_rfiduid'),
    ]

    operations = [
        migrations.AddField(
            model_name='clinicstaff',
            name='allowed_pages',
            field=models.JSONField(blank=True, default=None, null=True),
        ),
    ]
