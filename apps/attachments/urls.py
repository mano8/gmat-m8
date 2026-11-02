from django.urls import path

from apps.attachments import views

app_name = "attachments"

urlpatterns = [
    path("<uuid:attachment_id>/", views.download, name="download"),
    path("<uuid:attachment_id>/remove/", views.remove, name="remove"),
    path("request/<uuid:parent_id>/", views.upload, {"kind": "request"}, name="upload_request"),
    path("receipt/<uuid:parent_id>/", views.upload, {"kind": "receipt"}, name="upload_receipt"),
    path("handover/<uuid:parent_id>/", views.upload, {"kind": "handover"}, name="upload_handover"),
]
