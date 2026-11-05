"""Fills ``title`` / ``body`` of Phase 1/2 rows so they show in the Web App bell (API_CONTRACT_V3 §9.1)."""

import html
import re

from django.db import migrations

_TAG_RE = re.compile(r"<[^>]+>")


def _title_of(text: str) -> str:
    first = html.unescape(_TAG_RE.sub("", text or "")).split("\n", 1)[0].strip()
    # "🐔 NNP — PARRANDA TARQATILDI" → "NNP — PARRANDA TARQATILDI" (drop the leading emoji)
    index = first.find("NNP")
    return (first[index:] if index >= 0 else first)[:200]


def _body_of(text: str) -> str:
    lines = html.unescape(_TAG_RE.sub("", text or "")).split("\n")
    return "\n".join(lines[1:]).strip()


def backfill(apps, schema_editor):
    Notification = apps.get_model("notifications", "Notification")
    batch = []
    for row in Notification.objects.filter(title="").only("id", "text").iterator(chunk_size=500):
        row.title = _title_of(row.text)
        row.body = _body_of(row.text)
        batch.append(row)
        if len(batch) >= 500:
            Notification.objects.bulk_update(batch, ["title", "body"])
            batch = []
    if batch:
        Notification.objects.bulk_update(batch, ["title", "body"])


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0002_v3_inbox_settings"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
