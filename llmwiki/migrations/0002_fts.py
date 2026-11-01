"""FTS5 keyword indexes over llmwiki_chunk (rowid = Chunk.id). See llmwiki/rag/fts.py."""
from django.db import migrations

SQL = [
    "CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(header, body, tokenize = \"unicode61 remove_diacritics 2 tokenchars '-_/.'\")",
    "CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts_tri USING fts5(body, tokenize = 'trigram')",
]
REVERSE = ["DROP TABLE IF EXISTS chunk_fts", "DROP TABLE IF EXISTS chunk_fts_tri"]


class Migration(migrations.Migration):
    dependencies = [("llmwiki", "0001_initial")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
