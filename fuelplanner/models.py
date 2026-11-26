from django.db import models


class City(models.Model):
    """US city with coordinates, used for offline geocoding."""

    name = models.CharField(max_length=100)
    name_key = models.CharField(max_length=100)
    state = models.CharField(max_length=2)
    latitude = models.FloatField()
    longitude = models.FloatField()

    class Meta:
        verbose_name_plural = 'cities'
        indexes = [models.Index(fields=['name_key', 'state'])]

    def __str__(self):
        return f'{self.name}, {self.state}'


class FuelStation(models.Model):
    """Truck stop from the price list (city-level coordinates)."""

    opis_id = models.IntegerField(unique=True)
    name = models.CharField(max_length=200)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2)
    rack_id = models.IntegerField(null=True, blank=True)
    price = models.DecimalField(max_digits=8, decimal_places=4)
    latitude = models.FloatField()
    longitude = models.FloatField()

    class Meta:
        indexes = [models.Index(fields=['latitude', 'longitude'])]

    def __str__(self):
        return f'{self.name} ({self.city}, {self.state}) ${self.price}'
