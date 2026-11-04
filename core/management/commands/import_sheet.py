from django.core.management.base import BaseCommand

from core.importer import import_workbook


class Command(BaseCommand):
    help = "Import net-worth history from the Excel sheet (re-runnable; replaces imported dates)."

    def add_arguments(self, parser):
        parser.add_argument("path")

    def handle(self, path, **opts):
        report = import_workbook(path)
        self.stdout.write(f"{'Date':<12}{'Imported':>16}{'Sheet total':>16}  Check")
        for date, imported, sheet in report:
            ok = sheet is None or abs(imported - sheet) < 1
            mark = "ok" if ok else f"DIFF {imported - sheet:,.0f}"
            self.stdout.write(f"{date!s:<12}{imported:>16,.0f}{(sheet or 0):>16,.0f}  {mark}")
