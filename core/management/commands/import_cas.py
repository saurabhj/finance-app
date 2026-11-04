from django.core.management.base import BaseCommand

from core.models import Person
from integrations import cas


class Command(BaseCommand):
    help = "Import a CAMS/KFintech CAS PDF for one person."

    def add_arguments(self, parser):
        parser.add_argument("person")
        parser.add_argument("pdf")
        parser.add_argument("--password", required=True)

    def handle(self, person, pdf, password, **opts):
        account = cas.mf_account(Person.objects.get(name__iexact=person))
        pull, created, skipped = cas.import_cas(cas.read_pdf(pdf, password), account)
        self.stdout.write(f"{account}: pull={pull and pull.total_value}, flows +{created} (skipped {skipped})")
