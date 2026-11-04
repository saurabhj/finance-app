import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from core import analytics
from core.models import Household
from core.templatetags.money import inr

from . import data, engine, services
from .forms import PlanForm


@login_required
def plan(request):
    form = PlanForm(request.POST or None, initial=services.initial_values())
    if request.method == "POST" and form.is_valid():
        services.save_plan(form.cleaned_data)
        messages.success(request, "Plan saved.")
        return redirect("fi")
    from core.models import Account
    loans = Account.objects.filter(kind=Account.Kind.LOAN, is_active=True).select_related("owner")
    return render(request, "core/plan.html", {"form": form, "loans": loans})


@login_required
def fi(request):
    periods = analytics.periods()
    built = data.build_inputs(periods=periods)
    if not built:
        messages.info(request, "Add your monthly expenses to see the FI picture.")
        return redirect("plan")
    inp, contrib_source = built
    hh = Household.get()
    proj = engine.project(inp)
    mc = engine.monte_carlo(inp)
    history = data.wall_chart_history(periods)

    # chart horizon: a few years past crossover, at most 25 years
    cross_year = proj["crossover_date"].year if proj["crossover_date"] else None
    last_year = min((cross_year or inp.as_of.year + 25) + 5, inp.as_of.year + 25)
    future = [r for r in proj["series"] if r["year"] <= last_year]
    bands = [b for b in mc["bands"] if b["year"] <= last_year]
    target_line = [
        r["expenses"] * 12 / inp.swr + r["loan"] for r in future
    ]
    chart = {
        "hist_labels": [h["date"].strftime("%b %Y") for h in history],
        "hist_income": [round(h["safe_income"]) for h in history],
        "hist_expenses": [round(h["expenses"]) for h in history],
        "proj_labels": [str(r["year"]) for r in future],
        "proj_income": [round(r["safe_income"]) for r in future],
        "proj_expenses": [round(r["expenses"]) for r in future],
        "band_labels": [str(b["year"]) for b in bands],
        "p10": [round(b["p10"]) for b in bands],
        "p50": [round(b["p50"]) for b in bands],
        "p90": [round(b["p90"]) for b in bands],
        "target": [round(t) for t in target_line],
    }
    target_prob = None
    if hh.target_fi_date:
        target_prob = next((p["prob"] for p in mc["prob_by_year"] if p["year"] >= hh.target_fi_date.year), None)
    wage = engine.real_hourly_wage(float(hh.monthly_net_income), float(hh.work_costs_monthly),
                                   float(hh.hours_worked_per_month))
    prob_5y = next((p["prob"] for p in mc["prob_by_year"] if p["year"] == inp.as_of.year + 5), None)
    ctx = {
        "inp": inp, "proj": proj, "mc": mc, "hh": hh, "contrib_source": contrib_source,
        "chart_json": json.dumps(chart), "drift": data.drift(inp),
        "wage": wage, "expense_hours": engine.life_energy_hours(inp.monthly_expenses, wage),
        "savings_rates": data.savings_rates(periods, float(hh.monthly_net_income)),
        "target_prob": target_prob, "prob_5y": prob_5y,
        "gap": max(inp.fi_target - inp.corpus, 0),
        "income_sub": f"vs {inr(inp.core_expenses)} core spending / month",
        "cross_sub": f"Monte Carlo median: {mc['median_crossover_year'] or 'beyond 40 yrs'}",
        "has_targets": any(d["target"] is not None for d in data.drift(inp)),
    }
    return render(request, "core/fi.html", ctx)
