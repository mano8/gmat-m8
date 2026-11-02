from django.urls import path

from apps.exports import views

app_name = "exports"

urlpatterns = [
    path("", views.index, name="index"),
    path("<slug:key>.csv", views.download, name="download"),
]
