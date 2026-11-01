"""Which new-page proposals were refused (merged into an existing page) and with what cosine; and storylines per page."""
import json
from collections import Counter, defaultdict

from llmwiki.models import Job, PageEmail

rows = []
for j in Job.objects.filter(stage__in=["s2", "s12", "s123"]):
    for m in (j.result or {}).get("merged_new", []) or []:
        rows.append((m.get("cos"), m.get("title"), m.get("into")))
for cos, t, into in sorted(rows, key=lambda x: -(x[0] or 0)):
    print(f"{cos}  '{t[:55]}'  →  '{into[:55]}'")
truth = json.load(open("testdata/truth.json", encoding="utf-8"))
st = {t["message_id"]: t["storyline"] for t in truth["emails"] if not t.get("noise")}
per_page = defaultdict(Counter)
for pe in PageEmail.objects.filter(role="primary").select_related("email", "page"):
    s = st.get(pe.email.message_id)
    if s:
        per_page[pe.page.title][s] += 1
print()
for p, c in per_page.items():
    print(f"{p[:60]:<62} {dict(c)}")
