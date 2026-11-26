from urllib.parse import urlencode

from django.urls import reverse
from django.views.generic import TemplateView
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import RouteRequestSerializer
from .services.geocoding import LocationError
from .services.optimizer import NoFeasiblePlanError
from .services.planner import plan_trip
from .services.routing import RoutingError


class RoutePlanView(APIView):
    """GET ?start=&finish= or POST {"start", "finish"}."""

    def get(self, request):
        return self._plan(request.query_params)

    def post(self, request):
        return self._plan(request.data)

    def _plan(self, data):
        serializer = RouteRequestSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        start, finish = serializer.validated_data['start'], serializer.validated_data['finish']

        try:
            result = plan_trip(start, finish)
        except LocationError as exc:
            return _error(str(exc), status.HTTP_400_BAD_REQUEST)
        except NoFeasiblePlanError as exc:
            return _error(str(exc), status.HTTP_422_UNPROCESSABLE_ENTITY)
        except RoutingError as exc:
            return _error(str(exc), status.HTTP_502_BAD_GATEWAY)

        query = urlencode({'start': start, 'finish': finish})
        result['map_url'] = self.request.build_absolute_uri(f"{reverse('route-map')}?{query}")
        return Response(result)


def _error(message, code):
    return Response({'error': message}, status=code)


class MapView(TemplateView):
    """Leaflet map page that calls the API."""

    template_name = 'fuelplanner/map.html'
