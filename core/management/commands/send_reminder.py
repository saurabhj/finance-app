from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Email the month-end routine reminder (scheduled on the 1st)."

    def handle(self, **opts):
        if not settings.REPORT_RECIPIENTS:
            self.stdout.write("No REPORT_RECIPIENTS set.")
            return
        base = settings.APP_BASE_URL
        body = (
            "Time for the month-end update (before the EMI goes out):\n\n"
            f"1. Pull Zerodha for both of you: {base}/holdings/\n"
            "2. Upload the Zerodha ledger CSV and this month's CAS PDF (same page)\n"
            f"3. Fill the month-end entry: {base}/entry/\n\n"
            "The monthly AI review runs on the 3rd, using whatever is entered by then."
        )
        send_mail("Month-end finance update due", body, None, settings.REPORT_RECIPIENTS)
        self.stdout.write("Sent.")
