from django.contrib import admin

from . import models

for m in [models.Person, models.Snapshot, models.MonthlyExpense, models.Household,
          models.AllocationTarget, models.Review, models.Recommendation]:
    admin.site.register(m)


@admin.register(models.Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ["name", "owner", "kind", "asset_class", "expected_return", "is_active"]
    list_filter = ["owner", "kind"]


@admin.register(models.Instrument)
class InstrumentAdmin(admin.ModelAdmin):
    list_display = ["identifier", "name", "asset_class", "management", "advisor", "needs_review"]
    list_filter = ["management", "asset_class", "needs_review"]
    list_editable = ["management", "advisor"]


@admin.register(models.CashFlow)
class CashFlowAdmin(admin.ModelAdmin):
    list_display = ["date", "account", "amount", "kind", "source", "note"]
    list_filter = ["account", "kind", "source"]


@admin.register(models.Balance)
class BalanceAdmin(admin.ModelAdmin):
    list_display = ["snapshot", "account", "value"]
    list_filter = ["account"]
