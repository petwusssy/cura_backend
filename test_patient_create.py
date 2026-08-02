import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.test import Client
from rest_framework.test import APIClient
import uuid

client = APIClient()

data = {
    'id': str(uuid.uuid4())[:10],
    'name': 'Grace Aquino',
    'category': 'Student',
    'contact': '1234567',
    'birthday': '2026-02-08',
    'age': 0,
    'sex': 'Male',
    'email': 'ocampobaltazar48@gmail.com',
    'emergencyContact': 'thie',
    'emergencyPhone': '09975672235',
    'course': 'bsit',
    'yearLevel': '3rd Year',
    'studentCategory': 'College',
    'position': '',
    'department': '',
    'address': '',
    'guardianName': '',
    'gradeLevel': ''
}

response = client.post('/api/patients/', data, format='json')
print(f"Status Code: {response.status_code}")
print(f"Response Data: {response.data}")
