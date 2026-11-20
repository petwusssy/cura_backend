from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0032_medicineitem_expirationdate'),
    ]

    operations = [
        migrations.AddField(
            model_name='medicineitem',
            name='batches',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='purchasehistory',
            name='batchNumber',
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name='purchasehistory',
            name='expirationDate',
            field=models.CharField(blank=True, max_length=50, null=True),
        ),
        migrations.AddField(
            model_name='stockhistory',
            name='batchNumber',
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name='stockhistory',
            name='expirationDate',
            field=models.CharField(blank=True, max_length=50, null=True),
        ),
    ]
