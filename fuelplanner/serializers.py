from rest_framework import serializers


class RouteRequestSerializer(serializers.Serializer):
    start = serializers.CharField(max_length=200, help_text='e.g. "Chicago, IL" or "41.88,-87.63"')
    finish = serializers.CharField(max_length=200, help_text='e.g. "Denver, CO"')
