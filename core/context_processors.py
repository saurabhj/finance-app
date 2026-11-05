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
    # (label, url name, g-shortcut, badge, icon in includes/icon.html)
    sections = [
        ("Overview", [
            ("Dashboard", "dashboard", "d", None, "dashboard"),
            ("Financial independence", "fi", "f", None, "flag"),
        ]),
        ("Money", [
            ("Month-end entry", "snapshot_new", "e", None, "calendar"),
            ("Accounts", "accounts", "c", None, "bank"),
            ("Holdings", "holdings", "h", None, "pie"),
            ("Loans", "loans", "o", None, "house"),
            ("Statements", "statements", "s", None, "file"),
            ("Review transactions", "statements_review", "r", unconfirmed or None, "check"),
            ("Spending", "spending", "p", None, "wallet"),
            ("Money moved", "cashflows", "m", None, "arrows"),
        ]),
        ("AI", [
            ("Reviews", "reviews", "a", open_actions or None, "sparkle"),
            ("Ask", "ask", "q", None, "chat"),
        ]),
        ("Setup", [
            ("Checklist", "checklist", "k", None, "clipboard"),
            ("Plan", "plan", "l", None, "target"),
            ("Settings", "admin:index", None, None, "gear"),
        ]),
    ]
    new_tab = {"admin:index"}  # Django admin has no app sidebar: keep it out of the way
    path = request.path
    nav_sections = []
    commands = []
    for title, items in sections:
        rows = []
        for label, name, key, badge, icon in items:
            url = reverse(name)
            rows.append({"label": label, "url": url, "key": key, "badge": badge, "icon": icon,
                         "new_tab": name in new_tab, "active": False})
            commands.append({"label": f"Go to {label}", "url": url, "key": f"g {key}" if key else "",
                             "new_tab": name in new_tab})
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
        {"label": "Collapse / expand the menu", "action": "toggle-sidebar", "key": "["},
        {"label": "Add an account", "url": reverse("accounts") + "#add"},
        {"label": "Add a loan", "url": reverse("loan_new")},
        {"label": "Upload a statement", "url": reverse("statements") + "#upload"},
        {"label": "Add a statement rule", "url": reverse("statements") + "#rules"},
        {"label": "Edit notes for Claude", "url": reverse("statements") + "#notes"},
    ]
    return {"nav_sections": nav_sections, "palette_commands": commands}
