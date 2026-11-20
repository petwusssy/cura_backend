from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0031_clinicstaff_allowed_pages'),
    ]

    operations = [
        migrations.AddField(
            model_name='medicineitem',
            name='expirationDate',
            field=models.CharField(blank=True, max_length=50, null=True),
        ),
    ]
