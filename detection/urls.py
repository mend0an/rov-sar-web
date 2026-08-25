"""HTTP URL routing untuk detection app."""
from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("video", views.video_stream, name="video_stream"),

    # REST API
    path("api/state",            views.api_state),
    path("api/control",          views.api_control),
    path("api/waypoints",        views.api_waypoints_list),
    path("api/waypoint",         views.api_waypoint_mark),
    path("api/waypoints/clear",  views.api_waypoints_clear),
    path("api/screenshot",       views.api_screenshot),
    path("api/export",           views.api_export_gpx),

    # ROV — telemetri & kontrol
    path("api/rov/unlock",       views.api_rov_unlock),
    path("api/rov/command",      views.api_rov_command),
    path("api/rov/move",         views.api_rov_move),
    path("api/rov/estop",        views.api_rov_estop),
    path("api/rov/caps",         views.api_rov_caps),
    path("api/rov/sim",          views.api_rov_sim),
    path("api/rov/mapping",      views.api_rov_mapping),
    path("api/rov/prefs",        views.api_rov_prefs),
    path("api/sources",          views.api_sources),
    path("api/source",           views.api_set_source),
]
