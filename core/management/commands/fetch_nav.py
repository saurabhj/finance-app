from django.core.management.base import BaseCommand

from integrations.amfi import update_navs


class Command(BaseCommand):
    help = "Fetch today's mutual fund NAVs from AMFI (run daily via launchd)."

    def handle(self, **opts):
        self.stdout.write(f"Updated {update_navs()} funds.")
