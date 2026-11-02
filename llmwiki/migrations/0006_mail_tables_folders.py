"""Move the heavy columns of Email to their own table, and add folders and mail-rule fields.

The copy is one INSERT … SELECT, so it needs no memory for the mail. The old columns are dropped before the new
columns are added, because adding a NOT NULL column makes Django rewrite the whole table. Stop the portal while this
runs: on a mailbox of a few GB it can take some minutes. Free space stays in the file and is reused by new mail
(`manage.py vacuum` gives it back to the disk).
"""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('llmwiki', '0005_pages_free'),
    ]

    operations = [
        migrations.CreateModel(
            name='EmailOriginal',
            fields=[
                ('email', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, primary_key=True, related_name='original', serialize=False, to='llmwiki.email')),
                ('raw', models.BinaryField(null=True)),
                ('body_text', models.TextField(default='')),
                ('quoted_text', models.TextField(default='')),
            ],
        ),
        migrations.RunSQL(
            "INSERT INTO llmwiki_emailoriginal (email_id, raw, body_text, quoted_text) SELECT id, raw, body_text, quoted_text FROM llmwiki_email",
            reverse_sql="UPDATE llmwiki_email SET raw = (SELECT raw FROM llmwiki_emailoriginal o WHERE o.email_id = llmwiki_email.id), "
                        "body_text = COALESCE((SELECT body_text FROM llmwiki_emailoriginal o WHERE o.email_id = llmwiki_email.id), ''), "
                        "quoted_text = COALESCE((SELECT quoted_text FROM llmwiki_emailoriginal o WHERE o.email_id = llmwiki_email.id), '')",
        ),
        migrations.RemoveField(model_name='email', name='raw'),
        migrations.RemoveField(model_name='email', name='body_text'),
        migrations.RemoveField(model_name='email', name='quoted_text'),
        migrations.CreateModel(
            name='Folder',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=120, unique=True)),
                ('position', models.IntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={'ordering': ['position', 'name']},
        ),
        migrations.AddField(model_name='email', name='rule_ref', field=models.IntegerField(blank=True, db_index=True, null=True)),
        migrations.AddField(model_name='email', name='skip_manual', field=models.BooleanField(default=False)),
        migrations.AddField(model_name='email', name='folder', field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='emails', to='llmwiki.folder')),
        migrations.AddField(model_name='email', name='folder_rule', field=models.IntegerField(blank=True, null=True)),
        migrations.AddField(model_name='email', name='folder_manual', field=models.BooleanField(default=False)),
        migrations.AddIndex(model_name='email', index=models.Index(fields=['folder', 'sent_at'], name='llmwiki_ema_folder__b4a615_idx')),
        migrations.AddField(model_name='skiprule', name='recipient', field=models.CharField(blank=True, default='', help_text='glob on To/Cc, e.g. dl-ct@*', max_length=300)),
        migrations.AddField(model_name='skiprule', name='folder', field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='rules', to='llmwiki.folder')),
        migrations.AddField(model_name='page', name='needs_rewrite', field=models.BooleanField(default=False)),
    ]
