import subprocess
import sys

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.safestring import mark_safe
from django.views.decorators.http import require_POST

from core.models import Recommendation, Review

from . import review as ai_review


@login_required
def reviews(request):
    return render(request, "core/reviews.html", {
        "reviews": Review.objects.all()[:50],
        "open_recs": Recommendation.objects.filter(status="open").exclude(verdict="hold")
                     .select_related("review", "instrument").order_by("-review__created_at")[:30],
        "running": Review.objects.filter(status="running").exists(),
        "has_key": bool(__import__("os").environ.get("ANTHROPIC_API_KEY")),
    })


@login_required
def review_detail(request, pk):
    r = get_object_or_404(Review, pk=pk)
    return render(request, "core/review_detail.html", {
        "review": r,
        "html": mark_safe(ai_review.render_html(r.output_markdown)) if r.output_markdown else "",
        "recs": r.recommendations.select_related("instrument").order_by("id"),
    })


@login_required
@require_POST
def review_run(request):
    cadence = request.POST.get("cadence")
    if cadence not in ("weekly", "monthly"):
        return redirect("reviews")
    if Review.objects.filter(status="running").exists():
        messages.info(request, "A review is already running.")
        return redirect("reviews")
    r = ai_review.start_review(cadence)
    # Run in a separate process: a monthly review with deep thinking can take a few minutes.
    subprocess.Popen(
        [sys.executable, str(settings.BASE_DIR / "manage.py"), "run_review", "--cadence", cadence,
         "--review-id", str(r.id)],
        cwd=settings.BASE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    messages.success(request, f"{cadence.title()} review started. It takes a minute or two (longer for monthly); refresh this page.")
    return redirect("reviews")


@login_required
@require_POST
def recommendation_update(request, pk):
    rec = get_object_or_404(Recommendation, pk=pk)
    status = request.POST.get("status")
    if status in Recommendation.Status.values:
        rec.status = status
        rec.status_note = request.POST.get("note", "")[:200]
        rec.save(update_fields=["status", "status_note"])
    return redirect(request.POST.get("next") or "reviews")


@login_required
def ask(request):
    answer = None
    question = request.POST.get("question", "").strip()
    if request.method == "POST" and question:
        try:
            answer = ai_review.ask(question)
        except ai_review.ReviewError as e:
            messages.error(request, str(e))
    recent = Review.objects.filter(cadence="adhoc", status="done")[:10]
    return render(request, "core/ask.html", {
        "question": question, "answer": answer,
        "answer_html": mark_safe(ai_review.render_html(answer.output_markdown)) if answer else "",
        "recent": recent,
    })
