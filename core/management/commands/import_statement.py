from django.core.management.base import BaseCommand, CommandError

from core.models import Account
from statements import importer


class Command(BaseCommand):
    help = "Import a bank/card statement file into an account (same as the Statements page)."

    def add_arguments(self, parser):
        parser.add_argument("account_id", type=int)
        parser.add_argument("path")
        parser.add_argument("--password", default="")

    def handle(self, account_id, path, password, **opts):
        account = Account.objects.get(pk=account_id)
        with open(path, "rb") as f:
            data = f.read()
        try:
            stmt = importer.import_statement(account, path.rsplit("/", 1)[-1], data, password)
        except (importer.ImportError_, ValueError) as e:
            raise CommandError(str(e))
        self.stdout.write(f"{account}: {stmt.added} added, {stmt.duplicates} duplicates, read by {stmt.parser}, "
                          f"period {stmt.period_start}..{stmt.period_end}")
        if stmt.warning:
            self.stdout.write(f"  WARNING: {stmt.warning}")
