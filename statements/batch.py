"""Bulk statement uploads, processed in a background worker process.

create_batch() saves the files under data/uploads/<batch>/, works out each file's
account, and returns the batch; start_worker() runs `manage.py process_batch` in
the background. Reading (Claude for PDFs) runs up to 3 files at once; database
writes happen one at a time.
"""
import hashlib
import io
import logging
import subprocess
import sys
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from django.conf import settings
from django.db import close_old_connections, connections
from django.utils import timezone

from ai.review import ReviewError
from core.models import Account, StatementUpload, UploadBatch, UploadItem

from . import detect, importer

log = logging.getLogger(__name__)
ALLOWED = (".pdf", ".csv", ".xls", ".xlsx")
UPLOAD_ROOT = Path(settings.BASE_DIR) / "data" / "uploads"
READERS = 3


def _expand(name, data):
    """Yield (name, bytes) for a file, unpacking ZIPs (one level)."""
    if name.lower().endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for info in z.infolist():
                inner = Path(info.filename).name
                if not info.is_dir() and inner.lower().endswith(ALLOWED) and not inner.startswith("._"):
                    yield inner, z.read(info)
    elif name.lower().endswith(ALLOWED):
        yield name, data


def create_batch(files, passwords="", account=None):
    """files: iterable of (name, bytes). account: force all files into one account (else auto-detect)."""
    batch = UploadBatch.objects.create(passwords=passwords.strip())
    folder = UPLOAD_ROOT / str(batch.id)
    folder.mkdir(parents=True, exist_ok=True)
    pw_list = password_list(batch)
    accounts = list(Account.objects.filter(is_active=True, kind__in=["bank", "card"]).exclude(match_hints=""))
    seen = set()
    for name, data in files:
        for inner, blob in _expand(name, data):
            sha = hashlib.sha256(blob).hexdigest()
            if sha in seen:
                continue
            seen.add(sha)
            path = folder / f"{len(seen):03d}_{Path(inner).name}"
            path.write_bytes(blob)
            item = UploadItem(batch=batch, filename=inner[:255], path=str(path), sha256=sha)
            if StatementUpload.objects.filter(sha256=sha).exists():
                item.status, item.message = UploadItem.Status.DUPLICATE, "This exact file was uploaded before."
            elif account:
                item.account, item.detected_by = account, "chosen at upload"
            else:
                _detect(item, blob, pw_list, accounts)
            item.save()
    return batch


def password_list(batch):
    return [""] + [p.strip() for p in batch.passwords.splitlines() if p.strip()]


def _detect(item, blob, pw_list, accounts):
    try:
        pw = detect.pdf_password(blob, pw_list) if item.filename.lower().endswith(".pdf") else ""
        if pw is None:
            item.status, item.message = UploadItem.Status.NEEDS_ACCOUNT, "PDF is password-protected and none of the passwords worked."
            return
        acc, reason = detect.detect(item.filename, blob, pw, accounts)
    except Exception as e:  # noqa: BLE001
        acc, reason = None, f"couldn't read it ({e})"
    if acc:
        item.account, item.detected_by = acc, reason
    else:
        item.status, item.message = UploadItem.Status.NEEDS_ACCOUNT, reason.capitalize()


def start_worker(batch):
    subprocess.Popen(
        [sys.executable, str(Path(settings.BASE_DIR) / "manage.py"), "process_batch", str(batch.id)],
        cwd=settings.BASE_DIR, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )


def process(batch, client=None, workers=None):
    """Import every queued item that has an account. Safe to call again (e.g. after assigning accounts).
    workers: parallel readers (defaults to READERS); 1 runs everything in the calling thread."""
    workers = READERS if workers is None else workers
    threaded = workers > 1
    batch.status, batch.started_at = "running", batch.started_at or timezone.now()
    batch.save(update_fields=["status", "started_at"])
    pw_list = password_list(batch)
    items = list(batch.items.filter(status=UploadItem.Status.QUEUED, account__isnull=False).select_related("account"))
    write_lock = threading.Lock()

    def handle(item):
        if threaded:
            close_old_connections()
        try:
            data = Path(item.path).read_bytes()
            pw = detect.pdf_password(data, pw_list) if item.filename.lower().endswith(".pdf") else ""
            if pw is None:
                raise importer.ImportError_("PDF is password-protected and none of the passwords worked.")
            UploadItem.objects.filter(pk=item.pk).update(status=UploadItem.Status.PROCESSING)
            pre = importer.read(item.account, item.filename, data, pw, client)  # slow part, in parallel
            with write_lock:
                stmt = importer.import_statement(item.account, item.filename, data, pw, client, pre_read=pre)
            msg = f"{stmt.added} transactions" + (f", {stmt.duplicates} already known" if stmt.duplicates else "")
            if stmt.warning:
                msg += f". Check: {stmt.warning}"
            UploadItem.objects.filter(pk=item.pk).update(status=UploadItem.Status.DONE, statement=stmt, message=msg)
            Path(item.path).unlink(missing_ok=True)
        except importer.ImportError_ as e:
            status = UploadItem.Status.DUPLICATE if "already uploaded" in str(e) else UploadItem.Status.FAILED
            UploadItem.objects.filter(pk=item.pk).update(status=status, message=str(e))
        except (ReviewError, ValueError) as e:
            UploadItem.objects.filter(pk=item.pk).update(status=UploadItem.Status.FAILED, message=str(e))
        except Exception as e:  # noqa: BLE001 - one bad file mustn't stop the batch
            log.exception("Upload item %s failed", item.pk)
            UploadItem.objects.filter(pk=item.pk).update(status=UploadItem.Status.FAILED, message=f"Unexpected error: {e}")
        finally:
            if threaded:
                connections.close_all()

    if threaded:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(handle, items))
    else:
        for item in items:
            handle(item)
    finish(batch)
    return batch


def finish(batch):
    batch.refresh_from_db()
    if not batch.items.filter(status__in=[UploadItem.Status.QUEUED, UploadItem.Status.PROCESSING,
                                          UploadItem.Status.NEEDS_ACCOUNT]).exists():
        batch.passwords = ""  # don't keep statement passwords around
        batch.status, batch.finished_at = "done", timezone.now()
    else:
        batch.status = "waiting"  # some files need an account
    batch.save(update_fields=["passwords", "status", "finished_at"])
