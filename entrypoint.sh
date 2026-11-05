#!/bin/sh
#
# Konteyner har ishga tushganda: bazani kutish -> migratsiya -> statika.
# Bu yerda bazani o'chiradigan yoki tozalaydigan buyruq YO'Q: `migrate` faqat
# yangi o'zgarishlarni qo'shadi, shuning uchun har deploy'da xavfsiz.
set -e

echo "==> Baza kutilmoqda..."
python - <<'PY'
import os, sys, time
url = os.environ.get("DATABASE_URL", "")
if not url:
    print("    DATABASE_URL yo'q — SQLite ishlatiladi")
    sys.exit(0)
import psycopg
for attempt in range(1, 61):
    try:
        psycopg.connect(url, connect_timeout=3).close()
        print(f"    baza tayyor ({attempt}-urinish)")
        sys.exit(0)
    except Exception as exc:
        if attempt == 1:
            print(f"    hali tayyor emas: {exc}")
        time.sleep(2)
print("    XATO: baza 2 daqiqada javob bermadi")
sys.exit(1)
PY

if [ "${SKIP_MIGRATE:-0}" != "1" ]; then
    echo "==> Migratsiyalar..."
    python manage.py migrate --noinput
    echo "==> Statik fayllar..."
    python manage.py collectstatic --noinput --clear
fi

echo "==> Ishga tushmoqda: $*"
exec "$@"
