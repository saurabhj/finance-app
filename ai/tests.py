import datetime as dt
import json
from decimal import Decimal as D
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings

from core.models import (Account, Balance, HoldingsPull, Instrument, MonthlyExpense, Person, Position,
                         Recommendation, Review, Snapshot)

from . import context, review
from .schemas import REVIEW_SCHEMA


def fake_output(inst_id):
    return {
        "headline": "Move idle cash and exit the regular-plan fund",
        "summary": "Things are on track.",
        "fi_progress": "About 80% of the way.",
        "recommendations": [{"title": "Invest ₹20 L idle cash", "verdict": "action", "instrument_id": 0,
                             "amount": 2000000, "urgency": "high", "rationale": "Cash is above the emergency fund."}],
        "holding_verdicts": [
            {"instrument_id": inst_id, "verdict": "exit", "amount": 500000, "rationale": "Trails Nifty by 3%/yr."},
            {"instrument_id": 99999, "verdict": "hold", "amount": 0, "rationale": "Unknown id is ignored."},
        ],
        "advisor_strategy_notes": [{"advisor": "Weekend Investing", "assessment": "Fine.", "flag": False}],
        "risks": ["Concentration"], "data_gaps": ["FD maturity dates"], "questions_for_you": ["Any LTCG booked?"],
    }


class FakeStream:
    def __init__(self, payload, stop_reason="end_turn", calls=None, kwargs=None):
        self.payload, self.stop_reason = payload, stop_reason
        if calls is not None:
            calls.append(kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return SimpleNamespace(
            stop_reason=self.stop_reason, stop_details=None, model="claude-opus-5-5",
            content=[SimpleNamespace(type="thinking", thinking=""),
                     SimpleNamespace(type="text", text=json.dumps(self.payload))],
            usage=SimpleNamespace(input_tokens=1000, output_tokens=500, cache_read_input_tokens=0,
                                  cache_creation_input_tokens=0),
        )


class FakeClient:
    def __init__(self, payload, stop_reason="end_turn"):
        self.calls = []
        outer = self

        class _Messages:
            def stream(self, **kw):
                return FakeStream(payload, stop_reason, outer.calls, kw)

        self.beta = SimpleNamespace(messages=_Messages())


class ReviewTests(TestCase):
    def setUp(self):
        p = Person.objects.create(name="Saurabh")
        self.mf = Account.objects.create(owner=p, name="ET Money", kind="mf")
        bank = Account.objects.create(owner=p, name="Bank", kind="bank", asset_class="cash")
        s = Snapshot.objects.create(date=dt.date(2026, 11, 1))
        self.inst = Instrument.objects.create(identifier="INF1", name="Big Bluechip Fund - Regular Growth",
                                              kind="mf", management="self")
        adv = Instrument.objects.create(identifier="INF2", name="UTI Nifty 50 Index Fund", kind="mf",
                                        management="advisor", advisor="ET Money", is_passive=True)
        pull = HoldingsPull.objects.create(account=self.mf, source="cas", as_of=dt.date(2026, 10, 31), total_value=1500000)
        Position.objects.create(pull=pull, instrument=self.inst, quantity=100, avg_cost=4000, price=5000, value=500000)
        Position.objects.create(pull=pull, instrument=adv, quantity=100, avg_cost=8000, price=10000, value=1000000)
        Balance.objects.create(snapshot=s, account=self.mf, value=1500000, pull=pull)
        Balance.objects.create(snapshot=s, account=bank, value=3000000)
        MonthlyExpense.objects.create(month=dt.date(2026, 11, 1), amount=150000, includes_emi=False)

    def test_context_splits_self_and_advisor(self):
        ctx = context.build("monthly", fetch_history=False)
        h = ctx["holdings"]
        self.assertEqual([x["instrument_id"] for x in h["self_managed"]], [self.inst.id])
        self.assertEqual(h["self_managed"][0]["plan_type"], "regular")
        self.assertAlmostEqual(h["self_managed"][0]["unrealised_gain_pct"], 0.25)
        self.assertEqual(h["advisor_managed_groups"][0]["advisor"], "ET Money")
        self.assertTrue(ctx["fi"]["available"])
        json.dumps(ctx, default=str)  # serialisable

    @override_settings(REPORT_RECIPIENTS=["a@example.com"])
    def test_run_review_stores_recommendations_and_emails(self):
        client = FakeClient(fake_output(self.inst.id))
        with mock.patch.object(context.mfapi, "benchmark_returns", return_value={}):
            r = review.run_review("monthly", client=client)
        self.assertEqual(r.status, "done")
        recs = list(r.recommendations.order_by("id"))
        self.assertEqual([x.verdict for x in recs], ["action", "exit"])  # unknown-id verdict dropped
        self.assertEqual(recs[1].instrument, self.inst)
        self.assertIn("Self-managed holdings", r.output_markdown)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Move idle cash", mail.outbox[0].subject)
        # request shape: deep model, structured output, adaptive thinking, cached system prompt, fallbacks
        kw = client.calls[0]
        self.assertEqual(kw["model"], "claude-opus-5-5")
        self.assertEqual(kw["output_config"]["format"]["schema"], REVIEW_SCHEMA)
        self.assertEqual(kw["output_config"]["effort"], "high")
        self.assertEqual(kw["thinking"], {"type": "adaptive"})
        self.assertEqual(kw["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(kw["fallbacks"], "default")
        self.assertIn("<finance_snapshot>", kw["messages"][0]["content"])

    def test_weekly_uses_light_model(self):
        client = FakeClient(fake_output(self.inst.id))
        with mock.patch.object(context.mfapi, "benchmark_returns", return_value={}):
            review.run_review("weekly", client=client, send_email=False)
        self.assertEqual(client.calls[0]["model"], "claude-sonnet-5-5")
        self.assertEqual(client.calls[0]["output_config"]["effort"], "medium")

    def test_refusal_marks_review_failed(self):
        with mock.patch.object(context.mfapi, "benchmark_returns", return_value={}):
            with self.assertRaises(review.ReviewError):
                review.run_review("weekly", client=FakeClient({}, stop_reason="refusal"), send_email=False)
        self.assertEqual(Review.objects.get().status, "failed")

    def test_previous_recommendations_feed_next_review(self):
        with mock.patch.object(context.mfapi, "benchmark_returns", return_value={}):
            review.run_review("monthly", client=FakeClient(fake_output(self.inst.id)), send_email=False)
        rec = Recommendation.objects.get(verdict="exit")
        rec.status, rec.status_note = "dismissed", "Waiting for April"
        rec.save()
        prev = context.build("weekly", fetch_history=False)["previous_recommendations"]
        self.assertIn({"status": "dismissed", "your_note": "Waiting for April"},
                      [{k: p[k] for k in ("status", "your_note")} for p in prev])

    def test_pages(self):
        User.objects.create_user("u", password="pw")
        self.client.login(username="u", password="pw")
        with mock.patch.object(context.mfapi, "benchmark_returns", return_value={}):
            r = review.run_review("monthly", client=FakeClient(fake_output(self.inst.id)), send_email=False)
        self.assertContains(self.client.get("/reviews/"), "Invest ₹20 L idle cash")
        self.assertContains(self.client.get(f"/reviews/{r.id}/"), "Trails Nifty")
        rec = r.recommendations.get(verdict="action")
        self.client.post(f"/recommendations/{rec.id}/", {"status": "done", "note": "Moved to FD"})
        rec.refresh_from_db()
        self.assertEqual((rec.status, rec.status_note), ("done", "Moved to FD"))
        self.assertContains(self.client.get("/"), "Move idle cash")


class TrailingReturnTests(TestCase):
    def test_returns(self):
        from integrations.mfapi import trailing_returns
        start = dt.date(2020, 1, 1)
        rows = [(start + dt.timedelta(days=i), D(100) * D("1.0002") ** i) for i in range(0, 6 * 365)]
        r = trailing_returns(rows)
        self.assertAlmostEqual(r["1y"], 1.0002 ** 365 - 1, places=3)
        self.assertIsNotNone(r["5y"])
        self.assertIsNone(trailing_returns(rows[-100:])["1y"])
