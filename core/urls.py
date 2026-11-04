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

from fi import views as fi_views  # noqa: E402

urlpatterns += [
    path("fi/", fi_views.fi, name="fi"),
    path("plan/", fi_views.plan, name="plan"),
]

from ai import views as ai_views  # noqa: E402

urlpatterns += [
    path("reviews/", ai_views.reviews, name="reviews"),
    path("reviews/run/", ai_views.review_run, name="review_run"),
    path("reviews/<int:pk>/", ai_views.review_detail, name="review_detail"),
    path("recommendations/<int:pk>/", ai_views.recommendation_update, name="recommendation_update"),
    path("ask/", ai_views.ask, name="ask"),
]
