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
    path("plan/goals/", fi_views.plan_goals, name="plan_goals"),
]

from ai import views as ai_views  # noqa: E402

urlpatterns += [
    path("reviews/", ai_views.reviews, name="reviews"),
    path("reviews/run/", ai_views.review_run, name="review_run"),
    path("reviews/<int:pk>/", ai_views.review_detail, name="review_detail"),
    path("recommendations/<int:pk>/", ai_views.recommendation_update, name="recommendation_update"),
    path("ask/", ai_views.ask, name="ask"),
]

from statements import views as st_views  # noqa: E402

from . import account_views, checklist_views  # noqa: E402

urlpatterns += [
    path("checklist/", checklist_views.checklist, name="checklist"),
    path("checklist/from/<int:pk>/", checklist_views.statements_from, name="statements_from"),
    path("accounts/", account_views.accounts, name="accounts"),
    path("accounts/add/", account_views.account_add, name="account_add"),
    path("accounts/<int:pk>/", account_views.account_edit, name="account_edit"),
]

urlpatterns += [
    path("statements/", st_views.home, name="statements"),
    path("statements/upload/", st_views.upload, name="statement_upload"),
    path("statements/batches/<int:pk>/", st_views.batch_detail, name="statement_batch"),
    path("statements/batches/<int:pk>/assign/", st_views.batch_assign, name="statement_batch_assign"),
    path("statements/<int:pk>/delete/", st_views.delete_upload, name="statement_delete"),
    path("statements/rules/<int:pk>/delete/", st_views.delete_rule, name="rule_delete"),
    path("statements/rules/add/", st_views.add_rule, name="rule_add"),
    path("statements/rules/reapply/", st_views.reapply, name="rules_reapply"),
    path("statements/notes/", st_views.save_notes, name="statement_notes"),
    path("statements/review/", st_views.review, name="statements_review"),
    path("statements/review/save/", st_views.review_save, name="statements_review_save"),
    path("spending/", st_views.spending_view, name="spending"),
    path("spending/adopt/", st_views.spending_adopt, name="spending_adopt"),
    path("spending/<int:year>/<int:month>/", st_views.spending_month, name="spending_month"),
]

from loans import views as loan_views  # noqa: E402

urlpatterns += [
    path("loans/", loan_views.loan_list, name="loans"),
    path("loans/new/", loan_views.loan_edit, name="loan_new"),
    path("loans/<int:pk>/", loan_views.loan_detail, name="loan_detail"),
    path("loans/<int:pk>/edit/", loan_views.loan_edit, name="loan_edit"),
    path("loans/<int:pk>/rates/", loan_views.add_rate, name="loan_add_rate"),
    path("loans/<int:pk>/prepayments/", loan_views.add_prepayment, name="loan_add_prepayment"),
    path("loans/<int:pk>/checkpoints/", loan_views.add_checkpoint, name="loan_add_checkpoint"),
    path("loans/<int:pk>/<str:kind>/<int:item>/delete/", loan_views.delete_item, name="loan_delete_item"),
    path("loans/<int:pk>/from-transaction/<int:txn>/", loan_views.prepayment_from_transaction, name="loan_prepay_txn"),
    path("loans/<int:pk>/refresh-snapshots/", loan_views.refresh_snapshots, name="loan_refresh_snapshots"),
]
