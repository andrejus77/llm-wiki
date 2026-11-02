"""Give free space in the database back to the disk: `manage.py vacuum` (stop the portal first; it needs about as much
free disk as the database is large, and it can take some minutes). Only useful after removing a lot of data."""
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Compact the SQLite database file"

    def handle(self, *a, **o):
        from pathlib import Path
        path = Path(connection.settings_dict["NAME"])
        before = path.stat().st_size
        with connection.cursor() as c:
            c.execute("VACUUM")
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.stdout.write(f"{before / 1e6:.0f} MB → {path.stat().st_size / 1e6:.0f} MB")
