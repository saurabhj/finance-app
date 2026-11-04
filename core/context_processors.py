from pathlib import Path

from django.urls import reverse

_STATIC = Path(__file__).parent / "static" / "core"


def assets(request):
    """Cache-busting version for app.css / app.js (their latest modification time)."""
    try:
        v = int(max((_STATIC / f).stat().st_mtime for f in ("app.css", "app.js")))
    except OSError:
        v = 0
    return {"asset_version": v}


def nav(request):
    """Sidebar counts and the command-palette entries (only when signed in)."""
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    from core.models import Recommendation, Transaction

    unconfirmed = Transaction.objects.filter(confirmed=False).count()
    open_actions = Recommendation.objects.filter(status="open").exclude(verdict="hold").count()
    sections = [
        ("Overview", [
            ("Dashboard", "dashboard", "d", None),
            ("Financial independence", "fi", "f", None),
        ]),
        ("Money", [
            ("Month-end entry", "snapshot_new", "e", None),
            ("Holdings", "holdings", "h", None),
            ("Loans", "loans", "o", None),
            ("Statements", "statements", "s", None),
            ("Review transactions", "statements_review", "r", unconfirmed or None),
            ("Spending", "spending", "p", None),
            ("Money moved", "cashflows", "m", None),
        ]),
        ("AI", [
            ("Reviews", "reviews", "a", open_actions or None),
            ("Ask", "ask", "q", None),
        ]),
        ("Setup", [
            ("Plan", "plan", "l", None),
            ("Settings", "admin:index", None, None),
        ]),
    ]
    path = request.path
    nav_sections = []
    commands = []
    for title, items in sections:
        rows = []
        for label, name, key, badge in items:
            url = reverse(name)
            rows.append({"label": label, "url": url, "key": key, "badge": badge, "active": False})
            commands.append({"label": f"Go to {label}", "url": url, "key": f"g {key}" if key else ""})
        nav_sections.append({"title": title, "items": rows})
    # the active item is the longest URL that prefixes the current path
    all_items = [i for sec in nav_sections for i in sec["items"]]
    matches = [i for i in all_items if path == i["url"] or (i["url"] != "/" and path.startswith(i["url"]))]
    if matches:
        max(matches, key=lambda i: len(i["url"]))["active"] = True
    commands += [
        {"label": "Run weekly AI check", "post": reverse("review_run"), "data": {"cadence": "weekly"}},
        {"label": "Run monthly AI review", "post": reverse("review_run"), "data": {"cadence": "monthly"}},
        {"label": "Refresh mutual fund NAVs", "post": reverse("nav_refresh")},
        {"label": "Re-apply statement rules", "post": reverse("rules_reapply")},
        {"label": "Add a loan", "url": reverse("loan_new")},
        {"label": "Upload a statement", "url": reverse("statements") + "#upload"},
        {"label": "Add a statement rule", "url": reverse("statements") + "#rules"},
        {"label": "Edit notes for Claude", "url": reverse("statements") + "#notes"},
    ]
    return {"nav_sections": nav_sections, "palette_commands": commands}
