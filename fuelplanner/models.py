from django.db import models


class City(models.Model):
    """A US city with coordinates.

    Used to turn "City, ST" text into latitude/longitude without calling
    an external geocoding API (both for fuel stations and for the
    start/finish locations of a request).
    """

    name = models.CharField(max_length=100)
    # Lower-cased name, used for fast case-insensitive lookups.
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
    """A truck stop from the provided fuel price list.

    Coordinates are approximate (city centre), resolved once at load time.
    """

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
