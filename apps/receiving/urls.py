from django.urls import path

from apps.receiving import views

app_name = "receiving"

urlpatterns = [
    path("", views.receipt_list, name="list"),
    path("orders/", views.order_search, name="orders"),
    path("orders/<uuid:po_id>/receive/", views.receive, name="receive"),
    path("orders/<uuid:po_id>/post/", views.receipt_post, name="post"),
    path("expected/", views.project_list, name="projects"),
    path("expected/<uuid:project_id>/", views.expected, name="expected"),
    path("<uuid:receipt_id>/", views.receipt_detail, name="detail"),
    path("<uuid:receipt_id>/void/", views.receipt_void, name="void"),
]
