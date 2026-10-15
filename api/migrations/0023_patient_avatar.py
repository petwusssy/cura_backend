# Generated for patient avatar field
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0022_loginattempt'),
    ]

    operations = [
        migrations.AddField(
            model_name='patient',
            name='avatar',
            field=models.TextField(blank=True, null=True),
        ),
    ]
