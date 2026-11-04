from django.core.management.base import BaseCommand

from core.models import UploadBatch
from statements import batch as batches


class Command(BaseCommand):
    help = "Process a bulk statement upload (started automatically by the web app)."

    def add_arguments(self, parser):
        parser.add_argument("batch_id", type=int)

    def handle(self, batch_id, **opts):
        b = batches.process(UploadBatch.objects.get(pk=batch_id))
        self.stdout.write(f"Batch {b.id}: {dict(b.counts())}")
