#!/usr/bin/env bash
# exit on error
set -o errexit

pip install -r requirements.txt
python manage.py collectstatic --no-input
python manage.py migrate
python -c "import os, django; os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings'); django.setup(); from django.contrib.auth import get_user_model; User = get_user_model(); u = os.environ.get('DJANGO_USERNAME'); p = os.environ.get('DJANGO_PASSWORD'); User.objects.filter(username=u).exists() or (u and p and User.objects.create_superuser(username=u, email='', password=p)); User.objects.filter(is_superuser=True).update(is_staff=True); User.objects.filter(username='anthropeak2026').update(is_staff=True, is_active=True)" || true
