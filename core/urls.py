from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("entry/", views.snapshot_entry, name="snapshot_new"),
    path("entry/<str:date>/", views.snapshot_entry, name="snapshot_edit"),
    path("money-moved/", views.cashflows, name="cashflows"),
    path("money-moved/<int:pk>/delete/", views.cashflow_delete, name="cashflow_delete"),
]

urlpatterns += [
    path("holdings/", views.holdings, name="holdings"),
    path("holdings/tags/", views.instruments_update, name="instruments_update"),
    path("holdings/ledger/", views.ledger_upload, name="ledger_upload"),
    path("holdings/cas/", views.cas_upload, name="cas_upload"),
    path("holdings/nav/", views.nav_refresh, name="nav_refresh"),
    path("zerodha/<int:person_id>/connect/", views.zerodha_connect, name="zerodha_connect"),
    path("zerodha/<int:person_id>/pull/", views.zerodha_pull, name="zerodha_pull"),
    path("zerodha/callback/", views.zerodha_callback, name="zerodha_callback"),
]
