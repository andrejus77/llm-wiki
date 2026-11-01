"""Write a data package (the whole store as one consistent SQLite copy + manifest) to move it to another machine:
`manage.py export_data dist\\llmwiki-data.zip [--no-adapter]`. The zip holds company mail: keep it inside the company."""
import json

from django.core.management.base import BaseCommand

from llmwiki.transfer import export_package


class Command(BaseCommand):
    help = "Export the processed data (emails, wiki, vectors, settings) as a zip for import_data on another machine"

    def add_arguments(self, p):
        p.add_argument("out", help="zip file to write")
        p.add_argument("--no-adapter", action="store_true", help="leave data/company_adapter.py out")

    def handle(self, *a, **o):
        man = export_package(o["out"], include_adapter=not o["no_adapter"])
        self.stdout.write(json.dumps(man, indent=1, default=str))
        self.stdout.write(f"written: {o['out']}  (contains company mail: copy it only to company machines)")
