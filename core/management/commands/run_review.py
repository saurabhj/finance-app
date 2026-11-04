import json

from django.core.management.base import BaseCommand, CommandError

from ai import context, review
from core.models import Review


class Command(BaseCommand):
    help = "Run the weekly or monthly AI review (scheduled via launchd)."

    def add_arguments(self, parser):
        parser.add_argument("--cadence", choices=["weekly", "monthly"], required=True)
        parser.add_argument("--dry-run", action="store_true", help="Print the payload; don't call Claude.")
        parser.add_argument("--no-email", action="store_true")
        parser.add_argument("--review-id", type=int, help="Fill an existing 'running' review (used by the web UI).")

    def handle(self, cadence, dry_run, no_email, review_id, **opts):
        if dry_run:
            ctx = context.build(cadence)
            self.stdout.write(review.user_message(cadence, ctx))
            self.stdout.write(f"\n-- {len(json.dumps(ctx, default=str)):,} chars, model {review.model_for(cadence)}")
            return
        existing = Review.objects.get(pk=review_id) if review_id else None
        try:
            r = review.run_review(cadence, review=existing, send_email=not no_email)
        except review.ReviewError as e:
            raise CommandError(str(e))
        self.stdout.write(f"Review #{r.id} ({r.model}): {r.output_json['headline']}")
