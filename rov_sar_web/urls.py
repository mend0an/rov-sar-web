"""Top-level URL routing — delegasi ke detection app."""
from django.urls import path, include

urlpatterns = [
    path("", include("detection.urls")),
]
