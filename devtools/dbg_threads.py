"""Debug helper: list true threads that the store split, and the resent duplicate. Run via manage.py shell."""
import json
from collections import Counter, defaultdict

from llmwiki.models import Email


def main(path="testdata/truth.json"):
    truth = json.load(open(path, encoding="utf-8"))
    st = {e.message_id: e for e in Email.objects.all()}
    for t in truth["emails"]:
        if t.get("resent_duplicate"):
            e = st.get(t["message_id"])
            print("resent", t["message_id"][:60], e and (e.status, e.dup_of_id))
    print("dups in db", [(e.message_id[:50], e.dup_of_id) for e in Email.objects.filter(status="duplicate")])
    g = defaultdict(list)
    for t in truth["emails"]:
        if t["noise"] or t.get("alias_copy") or t.get("resent_duplicate"):
            continue
        e = st.get(t["message_id"])
        if e:
            g[t["thread"]].append((e.thread_id, e.subject[:60], t.get("broken_refs"), e.in_reply_to[:30]))
    for k, v in g.items():
        c = Counter(x[0] for x in v)
        if len(c) > 1:
            print(k, dict(c), [x for x in v if x[0] != c.most_common(1)[0][0]])


main()
