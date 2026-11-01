"""Print pages with their sections (manage.py shell -c "import devtools.dump_pages")."""
import os
import re

from llmwiki.models import Page, SkipRule
from llmwiki.pipeline.templates import label_of

n = int(os.environ.get("DUMP_N", "3"))
for p in Page.objects.order_by("id")[:n]:
    print("=" * 100)
    print(p.id, p.title, "|", p.level, "|", p.status, "| tags:", p.tags)
    for s in p.sections.all():
        if s.text:
            print(f"--- {label_of(p.level, s.key)} (v{s.version}, {len(s.sources)} sources)")
            print(re.sub(r"\n{2,}", "\n", s.text)[:1200])
print("PAGES:", [(p.title[:60], p.level) for p in Page.objects.all()])
print("RULES:", [(r.kind, r.sender, r.template, r.why[:80]) for r in SkipRule.objects.all()])
