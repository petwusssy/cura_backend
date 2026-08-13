from rest_framework import serializers
from .models import (
    Patient, Consultation, Treatment, MedicineItem, StockHistory,
    PurchaseRequest, PurchaseHistory, MedicalCertificate,
    Bed, BedHistory, HospitalTransfer, AppNotification
)

class PatientSerializer(serializers.ModelSerializer):
    class Meta:
        model = Patient
        fields = '__all__'

    def validate_name(self, value):
        return value.upper() if value else value

class TreatmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Treatment
        fields = '__all__'
        read_only_fields = ['consultation', 'id']

class ConsultationSerializer(serializers.ModelSerializer):
    treatments = TreatmentSerializer(many=True, required=False)

    class Meta:
        model = Consultation
        fields = '__all__'

    def create(self, validated_data):
        treatments_data = validated_data.pop('treatments', [])
        consultation = Consultation.objects.create(**validated_data)
        for treatment_data in treatments_data:
            Treatment.objects.create(consultation=consultation, **treatment_data)
        return consultation

    def update(self, instance, validated_data):
        treatments_data = validated_data.pop('treatments', None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        
        if treatments_data is not None:
            instance.treatments.all().delete()
            for treatment_data in treatments_data:
                Treatment.objects.create(consultation=instance, **treatment_data)
        return instance

class StockHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = StockHistory
        fields = '__all__'
        read_only_fields = ('medicine', 'id')

class MedicineItemSerializer(serializers.ModelSerializer):
    stockHistory = StockHistorySerializer(many=True, required=False)

    class Meta:
        model = MedicineItem
        fields = '__all__'

    def create(self, validated_data):
        stock_history_data = validated_data.pop('stockHistory', [])
        medicine = MedicineItem.objects.create(**validated_data)
        for stock_data in stock_history_data:
            stock_data.pop('medicine', None)
            stock_data.pop('id', None)
            StockHistory.objects.create(medicine=medicine, **stock_data)
        return medicine

    def update(self, instance, validated_data):
        stock_history_data = validated_data.pop('stockHistory', None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        
        if stock_history_data is not None:
            instance.stockHistory.all().delete()
            for stock_data in stock_history_data:
                stock_data.pop('medicine', None)
                stock_data.pop('id', None)
                StockHistory.objects.create(medicine=instance, **stock_data)
        return instance

class PurchaseHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = PurchaseHistory
        fields = '__all__'
        read_only_fields = ('request', 'id')

class PurchaseRequestSerializer(serializers.ModelSerializer):
    history = PurchaseHistorySerializer(many=True, required=False)

    class Meta:
        model = PurchaseRequest
        fields = '__all__'

    def create(self, validated_data):
        history_data = validated_data.pop('history', [])
        request = PurchaseRequest.objects.create(**validated_data)
        for hist_data in history_data:
            hist_data.pop('request', None)
            hist_data.pop('id', None)
            PurchaseHistory.objects.create(request=request, **hist_data)
        return request

    def update(self, instance, validated_data):
        history_data = validated_data.pop('history', None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        
        if history_data is not None:
            instance.history.all().delete()
            for hist_data in history_data:
                hist_data.pop('request', None)
                hist_data.pop('id', None)
                PurchaseHistory.objects.create(request=instance, **hist_data)
        return instance

class MedicalCertificateSerializer(serializers.ModelSerializer):
    class Meta:
        model = MedicalCertificate
        fields = '__all__'

class BedHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = BedHistory
        fields = '__all__'
        read_only_fields = ('bed',)

class BedSerializer(serializers.ModelSerializer):
    history = BedHistorySerializer(many=True, required=False)

    class Meta:
        model = Bed
        fields = '__all__'

    def update(self, instance, validated_data):
        history_data = validated_data.pop('history', None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        
        if history_data is not None:
            # Recreate history based on provided list
            instance.history.all().delete()
            for hist_data in history_data:
                # Remove bed from hist_data if it's there
                hist_data.pop('bed', None)
                BedHistory.objects.create(bed=instance, **hist_data)
        return instance

class HospitalTransferSerializer(serializers.ModelSerializer):
    class Meta:
        model = HospitalTransfer
        fields = '__all__'

class AppNotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = AppNotification
        fields = '__all__'
