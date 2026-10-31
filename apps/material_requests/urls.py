from django.urls import path

from apps.material_requests import views

app_name = "requests"

urlpatterns = [
    path("", views.request_list, name="list"),
    path("inbox/", views.inbox, name="inbox"),
    path("new/", views.request_new, name="new"),
    path("start/", views.request_start, name="start"),
    path("<uuid:request_id>/", views.request_detail, name="detail"),
    path("<uuid:request_id>/materials/", views.request_materials, name="materials"),
    path("<uuid:request_id>/review/", views.request_review, name="review"),
    path("<uuid:request_id>/lines/add/", views.line_add, name="add_line"),
    path("<uuid:request_id>/lines/add-free/", views.line_add_free, name="add_free_line"),
    path("<uuid:request_id>/submit/", views.request_submit, name="submit"),
    path("<uuid:request_id>/withdraw/", views.request_withdraw, name="withdraw"),
    path("<uuid:request_id>/cancel/", views.request_cancel, name="cancel"),
    path("<uuid:request_id>/examine/", views.request_examine, name="examine"),
    path("<uuid:request_id>/approve-all/", views.request_approve_all, name="approve_all"),
    path("<uuid:request_id>/return/", views.request_return, name="return_to_draft"),
    path("<uuid:request_id>/schedule/", views.request_schedule, name="schedule"),
    path("<uuid:request_id>/close/", views.request_close, name="close"),
    path("<uuid:request_id>/reopen/", views.request_reopen, name="reopen"),
    path("<uuid:request_id>/cancel-any/", views.request_cancel_any, name="cancel_any"),
    path("lines/<uuid:line_id>/decide/", views.line_decide, name="decide_line"),
    path("lines/<uuid:line_id>/reduce/", views.line_reduce, name="reduce_line"),
    path("lines/<uuid:line_id>/sourcing/", views.line_sourcing, name="set_sourcing"),
    path("lines/<uuid:line_id>/link/", views.line_link, name="link"),
    path("lines/<uuid:line_id>/resolve/", views.line_resolve, name="resolve_line"),
    path("lines/<uuid:line_id>/close-short/", views.line_close_short, name="close_line"),
    path("lines/<uuid:line_id>/reopen-line/", views.line_reopen, name="reopen_line"),
    path("lines/<uuid:line_id>/update/", views.line_update, name="update_line"),
    path("lines/<uuid:line_id>/remove/", views.line_remove, name="remove_line"),
]
