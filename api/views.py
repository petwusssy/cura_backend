from django.shortcuts import render
from django.http import JsonResponse

def health(request):
    return JsonResponse({
        "status": "ok",
        "message": "CURA Backend Running"
    })
