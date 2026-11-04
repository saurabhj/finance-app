from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.models import Account
from statements import batch as batches


class Command(BaseCommand):
    help = "Bulk-import every statement (PDF/CSV/XLS/XLSX/ZIP) in a folder, matching files to accounts automatically."

    def add_arguments(self, parser):
        parser.add_argument("folder")
        parser.add_argument("--passwords-file", help="Text file with PDF passwords, one per line")
        parser.add_argument("--account", type=int, help="Put every file into this account id instead of detecting")

    def handle(self, folder, passwords_file, account, **opts):
        root = Path(folder).expanduser()
        if not root.is_dir():
            raise CommandError(f"{root} is not a folder")
        files = [(p.name, p.read_bytes()) for p in sorted(root.rglob("*"))
                 if p.is_file() and p.suffix.lower() in batches.ALLOWED + (".zip",)]
        passwords = Path(passwords_file).expanduser().read_text() if passwords_file else ""
        acc = Account.objects.get(pk=account) if account else None
        b = batches.create_batch(files, passwords, acc)
        self.stdout.write(f"Batch {b.id}: {b.items.count()} files; reading…")
        batches.process(b)
        for it in b.items.select_related("account"):
            self.stdout.write(f"  [{it.get_status_display():16}] {it.filename[:45]:45} {str(it.account or ''):28} {it.message[:90]}")
        if b.items.filter(status="needs_account").exists():
            self.stdout.write("Some files need an account: assign them on the batch page in the app, or add match hints to the accounts.")
