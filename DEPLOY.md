# NNP — serverga joylash (Docker + nginx + avtomatik SSL)

Uch qism — uch alohida compose loyiha. Har birining fayllari o'z papkasida:

| Papka | Compose nomi | Nima bor |
|---|---|---|
| `parranda_backend` | `nnp-backend` | Postgres, Django (gunicorn), rejali ishlar, kunlik backup, **nginx (80/443)**, certbot |
| `parranda_next` | `nnp-frontend` | Next.js Web App |
| `parranda_bot` | `nnp-bot` | aiogram bot |

```
Internet ──► nginx (80/443) ──► /api/, /admin/  ──► backend (Django) ──► db (Postgres)
                     │                                   ▲
                     └──────► qolgan hammasi ──► frontend (Next.js)
                                                         │
                                       bot ──────────────┘ (ichki tarmoq: http://backend:8000/api)
```

- Faqat nginx port ochadi (80, 443). Qolganlari `nnp_net` ichki tarmog'ida.
- Nomlar `imkon` loyihasidan farq qiladi (`nnp-*`, `nnp_net`, alohida volume'lar) — ma'lumotlar aralashmaydi.
- Lekin 80/443 portini ikkalasi ham ishlatadi: **bir vaqtda faqat bittasi ishlaydi.**

---

## 1. imkon loyihasini vaqtincha to'xtatish

```bash
docker ps --format '{{.Names}}'          # qaysi konteynerlar ishlayotganini ko'rish
cd <imkon_bot papkasi>     && docker compose down
cd <imkon_next papkasi>    && docker compose down
cd <imkon backend papkasi> && docker compose down
```

**`-v` YOZMANG** — `down -v` imkon bazasini o'chiradi. `down` o'zi konteynerlarni to'xtatadi, ma'lumot volume'larda qoladi.

## 2. Fayllarni serverga yuborish

Windows'da (venv, node_modules, local baza va `.env` larsiz):

```powershell
cd E:\Haydar_bobo
tar -czf nnp.tar.gz --exclude=venv --exclude=node_modules --exclude=.next --exclude=".next-*" --exclude=db.sqlite3 --exclude=.env --exclude=media --exclude=backups --exclude=__pycache__ --exclude=demo_credentials.txt --exclude=test_accounts.txt parranda_backend parranda_next parranda_bot
scp nnp.tar.gz root@SERVER_IP:/opt/
```

Serverda:

```bash
mkdir -p /opt/nnp && tar -xzf /opt/nnp.tar.gz -C /opt/nnp && cd /opt/nnp
```

## 3. Backend (birinchi)

```bash
cd /opt/nnp/parranda_backend
cp .env.docker.example .env
nano .env        # SSL_EMAIL, POSTGRES_PASSWORD, SECRET_KEY, BOT_TOKEN, BOT_API_TOKEN
chmod +x init-ssl.sh
docker compose build
./init-ssl.sh    # faqat BIRINCHI marta: Let's Encrypt sertifikati
docker compose up -d
docker compose exec backend python manage.py createsuperuser
```

Tasodifiy qiymat yaratish: `python3 -c "import secrets; print(secrets.token_urlsafe(50))"`

## 4. Frontend

```bash
cd /opt/nnp/parranda_next
docker compose build
docker compose up -d
```

## 5. Bot

```bash
cd /opt/nnp/parranda_bot
cp .env.dist .env
nano .env        # BOT_TOKEN, ADMINS, BOT_API_TOKEN (backend .env dagi bilan BIR XIL), WEBAPP_URL=https://imkon-plus.uz
docker compose build
docker compose up -d
```

## 6. Tekshirish

```bash
curl -s https://imkon-plus.uz/api/health        # {"status":"ok","db":"ok",...}
cd /opt/nnp/parranda_backend && docker compose ps
docker compose logs -f --tail=50 backend
cd /opt/nnp/parranda_bot && docker compose logs -f --tail=50
```

Brauzerda: `https://imkon-plus.uz` (Web App), `https://imkon-plus.uz/admin/` (Django admin),
`https://imkon-plus.uz/api/docs` (API hujjati). Telegram'da botga `/start`.

## 7. Yangilash (kod o'zgarganda)

```bash
cd /opt/nnp/parranda_backend && docker compose build && docker compose up -d
cd /opt/nnp/parranda_next    && docker compose build && docker compose up -d
cd /opt/nnp/parranda_bot     && docker compose build && docker compose up -d
```

Migratsiya va statika backend konteyneri ishga tushganda o'zi bajariladi. Bazaga tegilmaydi.

## 8. NNP ni o'chirib, imkon'ni qaytarish

```bash
cd /opt/nnp/parranda_bot     && docker compose down
cd /opt/nnp/parranda_next    && docker compose down
cd /opt/nnp/parranda_backend && docker compose down
# keyin imkon: avval backend, keyin frontend, keyin bot
cd <imkon backend papkasi> && docker compose up -d
cd <imkon_next papkasi>    && docker compose up -d
cd <imkon_bot papkasi>     && docker compose up -d
```

NNP ma'lumotlarini butunlay o'chirish (qaytarib bo'lmaydi!): `cd /opt/nnp/parranda_backend && docker compose down -v`.

## Avtomatik ishlar (qo'lda hech narsa shart emas)

| Ish | Qayerda | Qachon |
|---|---|---|
| SSL yangilash | `certbot` | har 12 soatda tekshiradi |
| nginx sertifikatni qayta o'qish | `nginx` | har 6 soatda |
| Bazaning zaxira nusxasi (`pg_dump`, 14 kun) | `db-backup` → `backups` volume | kuniga bir marta |
| Emlash eslatmalari | `scheduler` (+ bot) | har 15 daqiqada, kuniga bir marta yuboradi |
| Bog'lanmagan fayllar, eski tokenlar | `scheduler` | kuniga bir marta |

Zaxira nusxadan tiklash:

```bash
cd /opt/nnp/parranda_backend
docker compose exec db-backup ls /backups
docker compose exec -T db-backup sh -c 'gunzip -c /backups/<FAYL>.sql.gz | psql'
```
