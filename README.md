# NNP backend (Django 5.2 + DRF)

“Navobod naslli parranda qisqarmasi” uchun API. Ikkala shartnoma majburiy: `../docs/API_CONTRACT.md` (1-bosqich)
va `../docs/API_CONTRACT_V2.md` (2-bosqich: statistika, audit, health, OpenAPI, partiya PATCH, bot hisoboti).
Barcha yo‘llar `/api/...` ostida va **oxirida `/` yo‘q** (`APPEND_SLASH = False`). Django admin: `/admin/`.

## Tuzilma

| Papka | Vazifasi |
|---|---|
| `config/` | sozlamalar (`.env` orqali, `environs`), `database.py` (`DATABASE_URL` tahlili), root URL |
| `common/` | rollar, ruxsat klasslari, paginatsiya, o‘zbekcha xato xabarlari, formatlash, `health.py` (health + OpenAPI ko‘rinishlari), `schema.py`, test fixture’lari |
| `accounts/` | `User` (rol `ADMIN/WAREHOUSE/OWNER`, `phone`, `telegram_id`, `must_change_password`), login, JWT, Telegram `initData`, foydalanuvchilarni boshqarish |
| `farm/` | `Household`, `PoultryBatch`, `PoultryMovement`, `DailyReport`; sanash qoidalari `selectors.py`, biznes mantiq `services.py`; `seed_demo`, `check_integrity` buyruqlari |
| `warehouse/` | `Product`, `StockTransaction`; qoldiq faqat `services.py` orqali o‘zgaradi |
| `notifications/` | `Notification`, `notify()` (faqat `transaction.on_commit` orqali), hodisa matnlari `events.py`, bot endpointlari (`X-Bot-Token`) |
| `dashboard/` | `/api/dashboard/admin` va `/api/dashboard/owner` |
| `stats/` | statistika: `periods.py` (davr parametrlari), `metrics.py` (sof formulalar), `flock.py` (parranda sonini tiklash), `queries.py` (agregat SQL), `services.py` (har endpoint uchun bitta funksiya), `benchmark.py` + `stats_benchmark` buyrug‘i |
| `audit/` | `AuditLog` (o‘zgarmas jurnal), `record()` (biznes tranzaksiyasi ichida), manba/IP middleware, admin mixin, `GET /api/audit-logs` |

Asosiy qoidalar:
- Parranda soni hech qachon qo‘lda saqlanmaydi — har doim harakatlardan (`PoultryMovement`) agregatsiya qilinadi.
- Son/qoldiqni o‘zgartiruvchi har bir amal `transaction.atomic()` ichida, partiya yoki mahsulot qatori `select_for_update()` bilan qulflangan holda tekshiriladi.
- Har bir biznes o‘zgarishi audit qatorini **o‘sha tranzaksiya ichida** yozadi (rollback → audit qatori ham yo‘q).
- OWNER faqat o‘z xonadonini ko‘radi: boshqa xonadon obyektlari `404`, ro‘yxatlar filtrlanadi.
- Superuser har doim `ADMIN` hisoblanadi.
- Operatsiyalar (harakatlar, ombor operatsiyalari, kunlik hisobotlar, audit) o‘zgarmas: API va adminda faqat o‘qiladi.

## O‘rnatish (Windows)

```powershell
cd E:\Haydar_bobo\parranda_backend
python -m venv venv                      # agar venv bo‘lmasa
venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env                   # so‘ng qiymatlarni to‘ldiring
venv\Scripts\python.exe manage.py migrate
venv\Scripts\python.exe manage.py createsuperuser   # ixtiyoriy
venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000
```

Kalit yaratish: `venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(50))"`

## Muhit o‘zgaruvchilari (`.env`)

| Nomi | Majburiy | Izoh |
|---|---|---|
| `SECRET_KEY` | ha | Django maxfiy kaliti |
| `DEBUG` | yo‘q | `True/False`, standart `False`. **`True` faqat tashqariga ochilmagan kompyuterda**: ngrok tunnel (`:3000`) yoqilgan bo‘lsa, Next.js `/api/*` ni Django’ga uzatadi va `DEBUG=True` URL ro‘yxati, traceback sahifalari hamda `/api/docs` ni internetga ochib qo‘yadi. `DEBUG=False` da admin CSS va Swagger UI uchun `runserver --insecure` ishlating |
| `ALLOWED_HOSTS` | yo‘q | vergul bilan, standart `localhost,127.0.0.1` |
| `BOT_TOKEN` | yo‘q | @BotFather tokeni, faqat Web App `initData` tekshiruvi uchun. Bo‘sh bo‘lsa `POST /api/auth/telegram` doim `401` |
| `BOT_API_TOKEN` | bot uchun ha | `parranda_bot` bilan umumiy maxfiy kalit (`X-Bot-Token`). Bo‘sh bo‘lsa barcha `/api/bot/...` endpointlari `403` |
| `CORS_ALLOWED_ORIGINS` | yo‘q | vergul bilan (odatda kerak emas: Next.js `/api` ni proxy qiladi) |
| `DB_PATH` | yo‘q | SQLite fayli (standart `db.sqlite3`). `scripts/integration_check.py` va `stats_benchmark` ni bazaning bir martalik nusxasida ishlatish uchun |
| `DATABASE_URL` | yo‘q | PostgreSQL’ga o‘tish: `postgres://user:parol@host:5432/nomi?sslmode=require` (`psycopg[binary]` o‘rnatilishi kerak). `sqlite:///yo‘l` ham qabul qilinadi. Berilmasa SQLite ishlaydi |
| `CONN_MAX_AGE` | yo‘q | `DATABASE_URL` bilan: ulanishni qayta ishlatish soniyalari (standart 60, health check yoqilgan) |
| `LOG_LEVEL` | yo‘q | `nnp.*` loggerlari darajasi (standart `INFO`). 5xx xatolar `django.request` orqali traceback bilan konsolga yoziladi |
| `NUM_PROXIES` | yo‘q | `X-Forwarded-For` ga IP qo‘shadigan ishonchli proxylar soni (standart `1` — ngrok). Login throttle va audit `ip` mijoz IP sini shu sarlavhaning oxirgi `N`-yozuvidan oladi; `0` — faqat `REMOTE_ADDR`. Next.js rewrite proxy bu sarlavhani o‘zgarishsiz uzatadi va o‘zidan hech narsa qo‘shmaydi, shuning uchun `:3000` ni faqat IP qo‘shadigan proxy (ngrok, nginx `proxy_add_x_forwarded_for`) orqali tashqariga oching |

`.env` git’ga qo‘shilmaydi (`.gitignore`). Shablon — `.env.example`.

## 2-bosqich endpointlari

| Endpoint | Kim | SQL so‘rovlar (o‘lchangan / limit) |
|---|---|---|
| `GET /api/stats/overview?date_from=&date_to=&mortality_threshold=` | A, W | 5 / 8 |
| `GET /api/households/{id}/stats` | A, W, O (o‘ziniki, boshqasi `404`) | 5 / 7 |
| `GET /api/my/stats` | O | 5 / 7 |
| `GET /api/batches/{id}/stats` | A, W | 2 / 6 |
| `PATCH /api/batches/{id}` (`health_status`, `supplier`) | A, W | — |
| `GET /api/products/{id}/stats` | A, W | 4 / 6 |
| `GET /api/warehouse/stats` | A, W | 3 / 6 |
| `GET /api/bot/users/{telegram_id}/summary` | bot (`X-Bot-Token`) | 4 (egasi), 6 (xodim) / 10 |
| `GET /api/audit-logs` (+ `/{id}`) | A | 2 / 2 |
| `GET /api/health` | hamma (auth yo‘q, throttle yo‘q) | 1 |
| `GET /api/schema` (`?format=yaml`), `GET /api/docs` | `DEBUG=True`: hamma; aks holda faqat ADMIN (JWT yoki `/admin/` sessiyasi) | — |

So‘rovlar soni JWT foydalanuvchisini o‘qishni hisobga olmaydi; ma’lumot 10 barobar oshganda ham o‘zgarmaydi
(`stats/tests/test_query_budget.py` 1-bosqichning barcha ro‘yxat/detal endpointlari bilan birga tekshiradi).
Swagger UI faqat Django portida ishlaydi: `http://127.0.0.1:8000/api/docs` (Next.js `/static/` ni proxy qilmaydi).

## Statistika algoritmlari (`stats/`)

Barcha formulalar `API_CONTRACT_V2.md` §1.1–1.2 bo‘yicha; kod joylashuvi:

1. **Kun** — Asia/Tashkent kalendar kuni; `today` so‘rov boshida bir marta olinadi. Ombor operatsiyasining kuni =
   `created_at` ning mahalliy sanasi; davr filtri `[date_from 00:00, date_to+1 00:00)` (indeksdan foydalanadi).
2. **Davr** (`periods.py`) — standart 30 kun, ko‘pi bilan 366; oldingi davr shu uzunlikda, darhol oldin. Xatolar §8.1 tartibida.
3. **Parranda soni** (`flock.py`) — `B_h(d) = Σ s(m)`, `date ≤ d` (DISTRIBUTE `+`, RETURN/DEATH/OUT `−`).
   Ikki agregat so‘rov: `S` dan oldingi qoldiq (Q-a) va `S…E` kunlik o‘zgarishlar (Q-b), `(xonadon, tur, sana)` bo‘yicha.
   Har xonadon/tur uchun bo‘lakli-o‘zgarmas funksiya (`Step`): qiymat `bisect` bilan, parranda-kunlar bo‘laklar
   bo‘yicha `qiymat × uzunlik` — kunlar soniga emas, o‘zgarishlar soniga bog‘liq. Kompaniya seriyasi farq massivi + prefiks yig‘indi.
   `DailyReport.bird_count` statistikada **ishlatilmaydi** (orqaga sanalangan harakatlar uni eskirtiradi).
4. **Hisobotlar** (Q-c) — `(xonadon, kun indeksi, tuxum, yem×100)` butun sonlar; kun indeksi SQL’da hisoblanadi
   (SQLite `julianday`, PostgreSQL `date − date`). Bitta o‘tishda: joriy/oldingi davr yig‘indilari, `R_L`
   (qatlam parrandali kunlar), `R+`, kutilgan-va-topshirilgan kunlar, kunlik seriya.
5. **Formulalar** (`metrics.py`, DB’siz unit testlangan): tuxumdorlik `100·Σeggs/ΣL`, yem/tuxum (aralash xonadonda yem
   `L/B` ulushida), yem/parranda/kun, o‘lim `100·deaths/birds_avg`, hisobot intizomi, kunlar zaxirasi, tugash sanasi.
   Yig‘indilar aniq (yem va ombor miqdori yuzdan birlarda butun son), nisbatlar `Decimal` (28 xona), yaxlitlash faqat
   chiqishda `ROUND_HALF_UP`; `0` ga bo‘lish → `null`; delta yaxlitlanmagan qiymatlardan (`%` yoki `pp`).
6. **Ombor** — boshlang‘ich qoldiq = davrdan oldingi oxirgi operatsiyaning `balance_after`; kunlik `(mahsulot, tur, kun)`
   guruhlash bitta so‘rov; birliklar hech qachon qo‘shilmaydi (FEED/kg alohida).

Tezlik (`manage.py stats_benchmark`, 100 xonadon × 730 kun = 70 097 hisobot, 884 harakat, 20 mahsulot × 10 operatsiya/kun
× 365 kun = 73 000 operatsiya, SQLite fayl, 5 ta iliq o‘lchov medianasi): overview 30 kun ≈ 20 ms, 366 kun ≈ 155 ms;
xonadon 5–9 ms; partiya 2–5 ms; mahsulot 7–23 ms; ombor 30 kun ≈ 10 ms, 366 kun ≈ 125 ms; bot xulosasi 4–6 ms.
Maqsad: har biri < 300 ms (buyruq oshsa chiqish kodi 1).

## Buyruqlar

```powershell
venv\Scripts\python.exe manage.py seed_demo            # faqat bo‘sh bazada ishlaydi
venv\Scripts\python.exe manage.py seed_demo --reset    # BARCHA biznes ma’lumotlarini o‘chirib qayta yaratadi
venv\Scripts\python.exe manage.py check_integrity      # sonlar/qoldiqlarni qayta hisoblaydi; muammo bo‘lsa exit 1
set DB_PATH=C:\temp\nnp_bench.sqlite3                   # FAQAT bir martalik fayl!
venv\Scripts\python.exe manage.py stats_benchmark      # sintetik katta baza + tezlik jadvali
venv\Scripts\python.exe manage.py spectacular --validate --fail-on-warn --file schema.yaml
```

**`seed_demo`** — 1 admin (`demo_admin`, superuser), 1 omborchi (`demo_omborchi`), 3 tumanda koordinatali 12 xonadon
(`demo_egasi1..12`, bittasi 12 kun oldin faolsizlantirilgan), 5 partiya (2 LAYER, 2 BROILER, 1 BREEDER), tarqatish,
qaytarish, sotish va nobud bo‘lishlar, ≈60 kunlik kunlik hisobotlar (har xil tuxumdorlik, ba’zi kunlar o‘tkazib yuborilgan,
bitta xonadonda yuqori o‘lim), 6 mahsulot va 60 kunlik kirim/chiqim tarixi. Hammasi biznes servislari orqali yaratiladi,
so‘ng `created_at` vaqtlari tarixga suriladi. Parollar tasodifiy va **faqat** `demo_credentials.txt` fayliga yoziladi
(git’ga qo‘shilmaydi); buyruq faqat fayl yo‘lini chiqaradi. Buyruqlar audit qatori yozmaydi.

**`check_integrity`** — partiya `center_stock`/`deaths_total`/`distributed_total` ni mustaqil qayta hisoblaydi,
`boshlang‘ich = markaz + xonadonlar + nobud + chiqim` invariantini, manfiy sonlarni, kelajakdagi yoki partiya kelishidan
oldingi harakatlarni, kunlik hisobot ↔ DEATH harakati mosligini va har mahsulot uchun `balance_after` zanjirini
(id tartibida qayta o‘ynash) hamda `Product.balance` ni tekshiradi. Faqat o‘qiydi. Ogohlantirish (`!`, chiqish kodiga ta’sir
qilmaydi): biror o‘tgan kun oxirida xonadon×partiya yoki markazdagi son manfiy bo‘lgan bo‘lsa (orqa sana bilan
kiritilgan harakat) — statistikada o‘sha kunlar noto‘g‘ri ko‘rinadi.

**`stats_benchmark`** — `DB_PATH` berilmagan yoki `db.sqlite3` ga teng bo‘lsa ishlamaydi; bo‘sh bo‘lmagan faylda faqat
`--reuse` bilan. `bulk_create` bilan katta baza yaratadi, servislar va view’lar vaqtini (`median`, so‘rovlar soni) chiqaradi.

## 3-bosqich (V3) poydevori

`docs/API_CONTRACT_V3.md` bo‘yicha umumiy qism tayyor (modullar jamoalari uchun qoidalar — `docs/SCAFFOLD_NOTES.md`):

- Rollar: `LOGISTICS` (Logistika xodimi), `VET` (Veterinar); Super Admin = Django superuser (`is_super_admin`).
  `IsAnyRole` ataylab A/W/O bo‘lib qoladi; yangi rollar uchun `common/permissions.py` dagi `IsRoleIn`, `ReadOnlyFor`,
  `IsLogistics`, `IsVet`, `IsSuperAdmin`, `IsAuthenticatedRole`.
- Yangi ilovalar: `core` (kompaniya sozlamalari `/api/settings`, fayllar `/api/files`, qidiruv `/api/search`, zaxira
  buyruqlari), `orders`, `logistics`, `vet`, `reports` (bo‘sh skelet: `urls.py`, `services.py`, `events.py`, `tests/`).
- Sessiyalar: `POST /api/auth/logout` (`{refresh, all}`), `POST /api/users/{id}/end-sessions`; logoutdan keyin refresh →
  `401` o‘zbekcha. `rest_framework_simplejwt.token_blacklist` o‘rnatildi — `flushexpiredtokens` ni haftada bir ishga tushiring.
- Bildirishnomalar V3: har bir faol qabul qiluvchiga qator (Telegramsiz → `SKIPPED`), `/api/my/notifications*` (qo‘ng‘iroqcha),
  `/api/notifications*` (monitor, qayta yuborish), `/api/notification-settings*` (Super Admin), `/api/bot/users/{tid}/notifications`,
  `/api/bot/reminders/run` + `notifications/reminders.py` (kunlik eslatmalar registri).
- Fayllar `MEDIA_ROOT` (`.env`: `MEDIA_ROOT`, `FILE_STORAGE=local|s3`) ga tasodifiy nom bilan saqlanadi; ommaviy URL yo‘q.

```powershell
venv\Scripts\python.exe manage.py backup_db [--output-dir DIR] [--keep 14] [--with-media]   # onlayn izchil nusxa
venv\Scripts\python.exe manage.py restore_db --file backups\nnp-YYYYMMDD-HHMMSS.sqlite3 --yes  # avval joriy bazani saqlaydi
venv\Scripts\python.exe manage.py cleanup_files        # 24 soatdan eski biriktirilmagan fayllar (har kuni)
venv\Scripts\python.exe manage.py send_reminders [--date YYYY-MM-DD] [--force]   # botning 15 daqiqalik tiki bilan bir xil
venv\Scripts\python.exe manage.py flushexpiredtokens   # muddati o‘tgan refresh tokenlar (haftada bir)
```

**Zaxira nusxa va tiklash.** Windows Task Scheduler’da har kuni `backup_db --keep 14 --with-media`, nusxalarni boshqa
diskka/kompyuterga ko‘chiring. `restore_db` faqat SQLite uchun, `--yes` siz ishlamaydi, tiklashdan oldin
`nnp-before-restore-*.sqlite3` nusxa oladi; so‘ng serverni qayta ishga tushiring. PostgreSQL: `pg_dump -Fc` / `pg_restore --clean`.

### Buyurtmalar (`orders`, V3 §6)

- Modellar: `Order` (`BY-000005`), `OrderItem` (`requested_qty > 0`, `approved_qty ≥ 0`, `(order, product)` unikal),
  `OrderStatusLog`; migratsiya `orders/0001_initial`. Buyurtmalar o‘chirilmaydi (DELETE yo‘q, Django admin faqat o‘qish).
- Endpointlar: `GET/POST /api/orders`, `GET /api/orders/{id}` (`history` bilan), `POST …/approve`, `…/reject`, `…/cancel`,
  `…/create-task`, `GET /api/catalog/products` (egalar uchun katalog: qoldiq ko‘rsatilmaydi, faqat `in_stock`).
- Har bir o‘tish `orders/services.py` da: `transaction.atomic()` → buyurtma qatori `select_for_update()` → **joriy** holat
  tekshiruvi (`409`) → qatorlar → `OrderStatusLog` + audit `orders / status_change` → bildirishnoma (`on_commit`).
- Tasdiqlash `create_task=true` bilan logistika topshirig‘ini **shu tranzaksiyada** yaratadi (zaxira §7.4); ombor yetmasa
  butun tasdiqlash bekor bo‘ladi (hech narsa saqlanmaydi).
- Logistika bilan bog‘lanish faqat `orders/bridge.py` orqali: `logistics.services.create_task_from_order(actor, order, items, data)`
  va `cancel_task(actor, task_id, reason)`; teskari yo‘nalishda logistika `orders.services.lock_order()` va
  `sync_order_with_task(task, actor, note)` ni chaqiradi (`IN_TRANSIT → IN_DELIVERY`, `DELIVERED`, `CONFIRMED`,
  `CANCELLED → APPROVED`). Logistika jadvallari bo‘lmasa topshiriqsiz tasdiqlash ishlaydi, `create_task` → `409`.
- O‘qish tomoni `orders/selectors.py`: `allowed_actions`, dashboard kartalari (`orders`, `pending_orders`, egasi uchun
  `orders.open/awaiting_confirmation`), bot uchun `bot_new_orders()` / `owner_open_orders()`, qidiruv guruhi `order`.
  Jadvallar hali migratsiya qilinmagan bazada (jonli server, Fix bosqichigacha) dashboard nol qiymat, qidiruv bo‘sh guruh qaytaradi.

### Logistika (`logistics`, V3 §7)

- Modellar: `Vehicle` (raqam katta harf, `Lower(plate_number)` unikal, `capacity_kg > 0`), `Driver` (`user` — faqat faol
  LOGISTICS, OneToOne), `LogisticsTask` (`LG-000012`, `order`/`order_item` — `orders` ga FK), `LogisticsTaskItem`,
  `LogisticsTaskStatusLog` (`source` API/BOT, koordinatalar); migratsiya `logistics/0001_initial` (orders.0001 ga bog‘liq).
  DB cheklovlari: holat qiymati, `ASSIGNED…CONFIRMED ⇒ vehicle+driver`, `IN_TRANSIT+ ⇒ started_at`, bitta buyurtmaga bitta faol topshiriq.
- Endpointlar: `/api/logistics/vehicles|drivers` (yozish A, L; o‘qish W), `/api/logistics/tasks` (+ `assign`, `accept`,
  `start`, `deliver`, `confirm`, `cancel`, PATCH `planned_date/note`), `/api/logistics/summary`; bot: `/api/bot/users/{tid}/tasks`,
  `/api/bot/tasks/{id}/accept|start|deliver|confirm`, `/api/bot/files` (`bot_action` throttle — Telegram ID bo‘yicha).
- Holat mashinasi `logistics/services.py::ACTIONS`: rol + joriy holat (`409 "Topshiriq holati: …"`), LOGISTICS uchun
  `accept/start/deliver` faqat mas’ul yoki haydovchiga bog‘langan xodim (`403 "Bu topshiriq boshqa xodimga biriktirilgan."`);
  mas’ul bo‘lmasa qabul qilgan xodim mas’ul bo‘ladi. OWNER boshqa xonadon topshirig‘ida `404`.
- Ombor (`logistics/stock.py`, `warehouse.services.post_locked` ustida): yaratishda zaxira (`reserved += qty`, hammasi yoki
  hech narsa), `start` da har mahsulot uchun bitta OUT (`reference = logistics_task`, `stock_out` faqat omborchilarga),
  `IN_TRANSIT` dan bekor qilishda bitta qaytarish IN (yangi partiya). Qulf tartibi: Order → Task → Product (id o‘sish) → lotlar.
  Ikki parallel `start` → bitta OUT (test: `logistics/tests/test_concurrency.py`).
- Bildirishnomalar `logistics/events.py`: `task_created`, `driver_assigned` (kalit `{id}.{assign_seq}`), `delivery_started`,
  `delivery_delivered`, `delivery_confirmed` (“Muammo bilan qabul qilindi”), `task_cancelled` (egasiga faqat biriktirilgandan keyin).
- Bot va boshqa modullar uchun: `logistics.bot.register_extension(role, fn)` — VET/ADMIN `/tasks` payloadini veterinariya
  moduli to‘ldiradi; LOGISTICS bot xulosasi `stats.services.bot_summary` → `logistics.selectors.bot_today`.
- `check_integrity`: `Product.reserved = Σ faol topshiriq zaxirasi`, boshlangan topshiriqda har mahsulot uchun OUT bor,
  buyurtma/topshiriq holatlari mos (`logistics/integrity.py`).
- Izohlar: tasdiqlash sababi admin uchun `issue_note` da saqlanadi; qayta biriktirishda `responsible` berilmasa —
  haydovchining xodimi → LOGISTICS bajaruvchi → avvalgi mas’ul; mahsulotlar o‘zgarmaydi (bekor qilib yangisini yarating).

### Veterinariya (`vet`, V3 §8)

- Modellar: `VetVisit` (`VT-…`), `Vaccination` (`EM-…`), `DiseaseRecord` (`KS-…`), `Treatment` (`DV-…`), `VetRequest` (`VM-…`);
  migratsiya `vet/0001_initial` (farm.0002, warehouse.0002 ga bog‘liq). DB cheklovlari: `DONE ⇒ done_at + holat + xulosa`,
  `DONE ⇒ done_date`, `cases > 0`, `RESOLVED ⇒ resolved_date ≥ diagnosed_date`, `end_date ≥ start_date`, miqdorlar ≥ 0,
  yopilgan murojaatda `closed_at`; ombor chiqimi OneToOne (ikki marta chiqim bo‘lmaydi).
- Endpointlar: `/api/vet/visits` (+ `complete`, `cancel`, PATCH), `/api/vet/vaccinations` (bitta yoki `household_ids` — ommaviy;
  + `complete`, `cancel`, PATCH; `?batch=` WAREHOUSE uchun ham ochiq), `/api/vet/diseases` (`?visit=`, `?active=1` qo‘shimcha
  filtrlar), `/api/vet/treatments` (`?active=1`), `/api/vet/requests` (+ `take`, `close`, `plan-visit`), `/api/vet/calendar`,
  `/api/vet/summary`, `/api/my/vet`. `vet=me|<id>|none` filtri. OWNER boshqa xonadon obyektida `404`, LOGISTICS `403`.
- Xizmatlar `vet/services.py`: `transaction.atomic` → `select_for_update` → joriy holat (`409`) → yozuv → audit `vet / create|update|status_change`
  → bildirishnoma (`vet/events.py`, commitdan keyin). Vaksina/preparat sarfi: mahsulot + `quantity_used > 0` bo‘lsa
  `warehouse.services.post_locked` orqali bitta OUT (`document_no` = `EM-…`/`DV-…`, `reference = vaccination|treatment`,
  `stock_out` faqat omborchilarga); miqdor `available` dan oshsa `400 {"quantity_used": [...]}`.
- VET Phase 1 ma’lumotlarini o‘qiydi (xonadonlar, partiyalar, harakatlar, kunlik hisobotlar, mahsulotlar) va faqat `DEATH`
  harakatini kiritadi (`household` majburiy) — `farm/views.py`.
- Eslatmalar (`vet/reminders.py`, `send_reminders` / bot tiki): `vaccination_due` (`{id}.due`, kuni `{id}.today`),
  `vaccination_overdue` (1/7/14/30-kun, `{id}.od{N}`), `vet_visit_today` (`{id}.{YYYYMMDD}`) — kuniga bir marta, qayta ishga
  tushirish yangi qator yaratmaydi.
- Boshqa modullar uchun: `vet.selectors.admin_dashboard_block()` / `owner_dashboard_block()` (dashboard `vet` kartasi),
  `problem_counts()` (muammoli xonadonlar: muddati o‘tgan emlash, ochiq kasallik), `bot_today()` (VET bot xulosasi),
  `logistics.bot` kengaytmalari (VET `/tasks`, OWNER `upcoming_vet`, ADMIN `vet_overdue`), qidiruv guruhi `vet_visit`.
  Jadval hali yo‘q bazada (Fix bosqichigacha) bular nol / bo‘sh qaytaradi.
- `check_integrity`: sarflangan vaksina/preparatning OUT i bor va mos (mahsulot, miqdor, xonadon), DONE/RESOLVED maydonlari (`vet/integrity.py`).

### Ombor: partiyalar va inventarizatsiya (`warehouse`, V3 §5)

- Modellar: `StockLot` (har bir IN = bitta partiya: `lot_no`, `expiry_date`, `received_qty`, `remaining_qty`),
  `StockLotAllocation` (OUT/ADJUST_OUT qaysi partiyadan qancha olgani), `InventoryCount` (`INV-…`, DRAFT/APPROVED/CANCELLED),
  `InventoryCountLine`; yangi turlar `ADJUST_IN`/`ADJUST_OUT` (faqat inventarizatsiya tasdiqlanganda). Migratsiya
  `warehouse/0003_…`. DB cheklovlari: `remaining ≥ 0`, `remaining ≤ received`, `received > 0`, allocation `quantity > 0`,
  `(transaction, lot)` va `(count, product)` unikal, `counted_qty ≥ 0`.
- Chiqim tartibi (FEFO): avval partiyasiz qoldiq (`balance − Σ remaining`, V3 dan oldingi kirimlar va ADJUST_IN), keyin
  partiyalar `expiry_date` o‘sish tartibida (muddatsizlari oxirida), `id`. Qo‘lda OUT da `lot` berilsa avval o‘sha partiya.
- Xizmatlar (`warehouse/services.py`, `warehouse/inventory.py`): `record_stock_transaction` (qo‘lda IN/OUT; vaksina/davolash
  OUT i uchun `owner_notified=False`, `reference`), `post_locked` (qulflangan mahsulotga IN/OUT — logistika va veterinariya),
  `apply_adjustment`, `create_count` / `update_lines` / `approve` / `cancel` — barchasi `transaction.atomic` + `select_for_update`
  (inventarizatsiya → mahsulotlar id bo‘yicha → partiyalar), holat qayta tekshiriladi (`409`), audit `inventory / create|update|approve|status_change`
  va har tuzatish uchun `stock / create`, bildirishnoma `inventory_approved` (commitdan keyin).
- Endpointlar: `GET /api/stock-lots` va `GET /api/products/{id}/lots` (`?expiring_within=0..365&expired=1&has_remaining=1`, A/W/V),
  `/api/inventory-counts` (`?status=`), `POST /api/inventory-counts`, `PATCH /{id}/lines`, `POST /{id}/approve` (faqat A),
  `POST /{id}/cancel`. Mahsulot obyekti: `reserved`, `available`, `nearest_expiry`; operatsiya obyekti: `lots`, `reference`.
  VET mahsulot va partiyalarni faqat o‘qiydi.
- Eslatmalar (`warehouse/reminders.py`): `lot_expiring` (`{lot_id}.soon`, `0 ≤ qolgan kun ≤ expiry_reminder_days`) va
  `lot_expired` (`{lot_id}.expired`) — `send_reminders` / bot tiki bilan kuniga bir marta, takroriy ishga tushirish yangi qator yaratmaydi.
- `check_integrity`: ADJUST_OUT chiqim sifatida hisoblanadi; partiya qoldig‘i = qabul − sarf, `Σ remaining ≤ balance`,
  sarf faqat chiqim operatsiyalariga bog‘langan (natija oxirida “Mahsulot partiyalari: N.” qatori).
- Statistika (V3 §5.6): `in_*`/`out_*` faqat IN/OUT; `adjust_in_qty`, `adjust_out_qty`, kunlik `adjust_qty`,
  `closing_balance = opening + in − out + adjust_in − adjust_out`.
- Fix bosqichigacha (migratsiya qilinmagan baza) partiya o‘qishlari `warehouse/readiness.py` orqali o‘tkazib yuboriladi.
  Boshqa modullar ma’lumotini o‘qish uchun `readiness.optional_model(app, model, *fields)`.

### Hisobotlar va dashboard (`reports`, V3 §10–§11)

- `GET /api/reports` (rolga ruxsat etilgan hisobotlar), `GET /api/reports/{key}?format=json|xlsx|pdf&date_from=&date_to=&region=&district=&household=&poultry_type=`,
  `GET /api/households/regions` (A/W/V; filtr paneli uchun). 10 ta hisobot (`reports/registry.py`), ma’lumot quruvchilar
  `reports/builders.py` — raqamlar V2 ta’riflaridan (`stats.flock`, `stats.metrics`), har hisobot ≤ 12 so‘rov.
- Excel `reports/xlsx.py` (openpyxl, write-only): 1–4 qator sarlavha, 6-qator ustun nomlari (`#12372A`), 7-qatordan ma’lumot,
  “Jami” qatori, muzlatilgan sarlavha, avtofiltr, raqam/sana formatlari, “Xulosa” varag‘i. PDF `reports/pdf.py` (reportlab,
  A4 albom, 12 mm): sarlavha qatori har sahifada, zebra, o‘ng tomonda raqamlar, pastda `Sahifa N / M`. Shrift — `REPORT_FONT_PATH` /
  `REPORT_FONT_BOLD_PATH` (masalan DejaVuSans), bo‘lmasa reportlab ichidagi Vera; `pdf_text()` shriftda yo‘q belgilarni almashtiradi
  (`ʻ` → `‘`, `№` → `No`, boshqasi `?`). Kirill harflari kerak bo‘lsa `REPORT_FONT_PATH` ga DejaVuSans bering.
- Fayl nomi: `kunlik-ishlab-chiqarish_2026-11-01_2026-11-04.xlsx` + `filename*` (`Kunlik ishlab chiqarish 01.11.2026-04.11.2026.xlsx`),
  `Cache-Control: no-store`. Har eksport audit `reports / export` (`after: {key, format, filters, rows}`); JSON ko‘rish audit qilinmaydi.
- Qator chegaralari: json 500 (`truncated`), xlsx 20 000, pdf 3 000 → `400`.
- Dashboard (`dashboard/problems.py`): `problem_households` (NO_REPORT, HIGH_MORTALITY, OVERDUE_VACCINATION, OPEN_DISEASE —
  bitta so‘rov, korrelyatsiyalangan subquery’lar) va `expiring_lots`; butun `/api/dashboard/admin` ≤ 22 so‘rov (test bilan).

## Testlar

```powershell
venv\Scripts\python.exe manage.py check
venv\Scripts\python.exe manage.py makemigrations --check --dry-run
venv\Scripts\python.exe manage.py test
```

~590 ta test (`manage.py test --parallel 4`). 1-bosqichdagilar (login/JWT/bloklash/throttle, OWNER ajratish, harakat/hisobot/ombor qoidalari,
bildirishnomalar, bot endpointlari, dashboardlar) va 2-bosqich:
- `stats/tests/test_metrics.py` — sof formulalar: §1.3 namunaviy misol (81.3, 135.7, 0.110, 0.8, 66.7, 24.0 kun,
  28.11.2026), half-up yaxlitlash, `0` ga bo‘lish, delta qoidalari, `Step`, davr parametrlari va xatolari;
- `stats/tests/test_api.py` — misol API orqali, oldingi davr, seriya (N ta, eski → yangi, nol bilan), orqaga sanalangan
  harakat, `bird_count` e’tiborsizligi, vaqt mintaqasi chegarasi (19:30Z → ertangi kun), partiya invarianti,
  `closing_balance = oxirgi balance_after`, reyting/yuqori o‘lim, rollar matritsasi, bot xulosasi;
- `stats/tests/test_query_budget.py` — barcha ro‘yxat/detal/statistika endpointlari: 1× va 10× ma’lumotda bir xil so‘rovlar soni;
- `stats/tests/test_performance.py` — 100 xonadon × 365 kun (`bulk_create`), limitlar va vaqt (`NNP_PRINT_TIMINGS=1` jadvalni chiqaradi);
- `audit/tests.py` — har amal uchun bitta qator (modul/amal/manba/aktor/IP), xato/rollback/no-op/throttle’da qator yo‘q,
  o‘zgarmaslik, maxfiy kalitlar yo‘qligi (rekursiv), filtrlar, Django admin;
- `common/tests.py` — health `200`/`503`, schema/docs ruxsatlari (`DEBUG` on/off), `spectacular --validate --fail-on-warn`, `DATABASE_URL`;
- `farm/test_batch_patch.py`, `farm/test_integrity.py`, `farm/test_seed_demo.py`.
- Mustaqil tekshiruv: `stats/tests/test_oracle.py`, `test_oracle_batch.py` — statistikadan butunlay alohida, har kun va har
  xonadon bo‘yicha `Fraction` bilan hisoblaydigan “oracle” bilan tasodifiy (seed’li) ma’lumotda overview, xonadon,
  partiya, mahsulot, ombor va bot xulosasining **har bir** maydoni solishtiriladi; `test_timezone_verify.py` (23:30/00:15
  mahalliy vaqt), `test_edge_verify*.py` (manfiy o‘tgan son, sana chegaralari), `test_schema_shapes.py` (OpenAPI ↔
  haqiqiy javob), `audit/test_secret_values.py` (parol/token *qiymatlari* audit’da yo‘q).

## Shartnomaga oid izohlar

1-bosqich:
- Hodisa jadvalidagi umumiy qoida “owner → `/my`” barcha hodisalarga qo‘llanadi (`poultry_returned` va
  `poultry_death` uchun ham), chunki `/households/{id}` sahifasi OWNER uchun yopiq.
- Kunlik hisobot bildirishnomasidagi “Operatsiya ID” `KH-000005` ko‘rinishida (shartnomada prefiks berilmagan).
- `POST /api/households`: `mfy`, `address` bo‘sh bo‘lishi mumkin; `latitude`/`longitude` birga beriladi.
- `dona`/`qop` birlikdagi mahsulotlar uchun ombor miqdori butun son bo‘lishi shart.
- Harakat sanasi partiya kelgan sanadan oldin bo‘la olmaydi.
- DRF’da o‘zbek tarjimasi yo‘q, shuning uchun `common/exceptions.py` standart xabarlarni o‘zbekchaga o‘giradi.
- Login throttle Django keshida (standart LocMem) saqlanadi — bir nechta jarayonli ishlab chiqarishda umumiy kesh (Redis) ulang.
- Login cheklovi ikki qatlamli: shartnomadagi `login` (10/daqiqa, mijoz IP si bo‘yicha; `/api/bot/link` da Telegram id bo‘yicha)
  va qo‘shimcha ravishda login nomi bo‘yicha `10/daqiqa` + `50/soat` (`/api/auth/login` va `/api/bot/link` uchun umumiy).
- JWT bekor qilish: har bir token `tv` (foydalanuvchining `token_version`) claim’ini olib yuradi. Parol tiklanganda
  (`reset-password`, Django admin parol formasi ham), parol almashtirilganda (`change-password`, joriy sessiya ham) va
  Telegram uzilganda (admin, bot `/logout` yoki Telegram id boshqa hisobga ko‘chganda) versiya oshadi — avval berilgan
  barcha access/refresh tokenlar `401 "Sessiya muddati tugagan. Qaytadan kiring."` oladi.
- Superuser’lar superuser bo‘lmagan ADMIN uchun `/api/users` da ko‘rinmaydi (`404`).
- `change-password` Django `AUTH_PASSWORD_VALIDATORS` ni ham tekshiradi; “kamida 8 belgi” qoidasi saqlangan.
- `GET /api/users?role=` vergul bilan bir nechta qiymat qabul qiladi (`role=ADMIN,WAREHOUSE`).
- `dashboard/admin`: `reports_today` faqat **faol** xonadonlar hisobotlarini sanaydi; `eggs_today`/`feed_used_today` barcha hisobotlarni.
- SQLite `SELECT ... FOR UPDATE` ni e’tiborsiz qoldiradi, shuning uchun bazaga `transaction_mode=IMMEDIATE` berilgan.
- `PATCH /api/households/{id}`: natijada `latitude`/`longitude` ikkalasi ham bor yoki ikkalasi ham yo‘q bo‘lishi shart.
- Barcha id’lar (URL, query, body) 64-bit oralig‘ida tekshiriladi: juda katta id → `400`/`404`, hech qachon `500` emas.
- `POST /api/bot/notifications/{id}/result`: `error` uzun bo‘lsa 2000 belgigacha qisqartiriladi, `null` ham qabul qilinadi.

2-bosqich (`API_CONTRACT_V2.md`):
- Bildirishnoma `button_path` (§9): `batch_created`, `poultry_out`, xonadonsiz `DEATH` → `/batches/{batch_id}`;
  `stock_in`, `stock_out` (xodimlar), `low_stock` → `/warehouse/{product_id}`; egasi doim `/my`.
- `GET /api/batches/{id}/stats`: faqat `date_to` berilsa `date_from = min(date_to, max(arrived_at, date_to − 365))`.
- Bot xulosasi (§5.1) `last_7d.reports` = 7 kundagi barcha hisobotlar (`reports` KPI ta’rifi), `expected` = kutilgan kunlar.
- SQLite’da mahalliy kun `date(created_at, '+18000 seconds')` bilan hisoblanadi (Django `TruncDate(tz)` har qator uchun
  Python funksiyasini chaqiradi va 366 kunlik omborda ~3 barobar sekin). Bu faqat davr bo‘yi UTC siljishi o‘zgarmas
  bo‘lsa ishlatiladi (Asia/Tashkent: doim +05:00), aks holda va PostgreSQL’da `TruncDate(tzinfo=…)`.
- Hisobot qatorlari (70k gacha) ORM yaratgan SQL bilan xom kursor orqali o‘qiladi — barcha ustunlar butun son, Django
  konvertorlari kerak emas (~35 % tezroq). SQL va parametrlar ORM’niki.
- Audit: User snapshot’iga `is_staff`, `is_superuser` ham qo‘shilgan (Django admin’da imtiyoz o‘zgarishi ko‘rinsin).
  Deny-list’da `password` bo‘lgan har qanday kalit o‘chiriladi, faqat `must_change_password` bundan mustasno (shartnoma uni saqlashni talab qiladi).
- Audit: Django admin parol formasi `users`/`password_reset` (manba `ADMIN`) yozadi va sessiyalarni bekor qiladi;
  admin’da `telegram_id` o‘zgartirilsa `telegram`/`link` yoki `unlink` yoziladi.
- Audit: mavjud login bilan noto‘g‘ri parol → `actor` = o‘sha foydalanuvchi (shartnoma: `null` faqat noma’lum login uchun);
  noma’lum login → `actor_name` = yuborilgan login (≤150). `/api/bot/link` dagi xato urinishlar audit qilinmaydi (shartnomada yo‘q).
- Audit: `GET /api/audit-logs/{id}` ham bor (shunda `PUT/PATCH/DELETE` `405` qaytaradi). `seed_demo --reset` audit qatorlarini
  o‘chirmaydi (o‘zgarmas); o‘chirilgan foydalanuvchining qatorlarida `actor = null`, `actor_name` saqlanadi.
- Audit konteksti middleware’da: `/api/bot/...` → `BOT` (IP `null`), `/admin/...` → `ADMIN`, qolgani → `API`.
- Yem/tuxum (§1.2): `feed × L/B` ulushi 1 dan oshmaydi. Orqa sana bilan kiritilgan DEATH/OUT (1-bosqich qoidasi bo‘yicha
  *joriy* son bilan tekshiriladi) boshqa tur sonini o‘tgan kunda manfiy qilishi mumkin; shunda `B ≤ L` (hatto `B = 0`)
  bo‘ladi — bunday kunda butun yem qatlam parrandalariga yoziladi (0 ga bo‘lish → 500 bo‘lmaydi). Bunday tarix
  `check_integrity` da ogohlantirish sifatida chiqadi.
- `GET /api/audit-logs`: `date_from`/`date_to` §8.1 bo‘yicha tekshiriladi (366 kun chegarasisiz): `date_to > bugun` →
  “Sana kelajakda…”, `date_from < 2000-01-01` → “Sana 01.01.2000 dan oldin…”, `date_from > date_to` → tartib xatosi.
- Statistika: faqat `date_to` berilib, standart boshlanish `date.min` dan oldinga tushsa (`date_to=0001-01-05`),
  boshlanish `date.min` ga qisiladi va `400` “Sana 01.01.2000 dan oldin…” qaytadi (avval `500`).
  HTTP so‘rovidan tashqarida (buyruqlar, migratsiyalar) `record()` hech narsa yozmaydi.
- Health: DB xatosi `nnp.health` loggeriga traceback bilan yoziladi, javobda esa faqat `status/db/time`.
- `stats_benchmark` 300 ms chegarasini standart **va** 366 kunlik davrlar uchun tekshiradi.
- PostgreSQL: `DATABASE_URL` bilan sozlanadi, barcha statistika ifodalari (kun indeksi, `TruncDate`, yuzdan birlar) portativ;
  PostgreSQL serverida testlar bu muhitda ishga tushirilmagan.
- V3 ilovalari nomi shartnoma §0.2 bo‘yicha: sozlamalar va fayllar alohida `files`/`company` ilovasida emas, `core` da;
  veterinariya — `vet`. `Attachment` umumiy `object_type`/`object_id` juftligi bilan bog‘lanadi (GenericForeignKey emas).
- Bildirishnoma `notify()` endi kalit so‘zli argumentlar oladi (`key`, `text`, `staff_path`, `owner_path`, `contextual` …);
  qabul qiluvchilar registr + Super Admin sozlamalaridan aniqlanadi. Eski testlar §18.4 bo‘yicha moslashtirildi.

3-bosqich, buyurtmalar (`orders`):
- Noma’lum mahsulot id si uchun xabar `“#123” mahsulot faol emas.` (shartnomadagi “X” o‘rniga id).
- `allowed_actions`: `confirm_receipt` buyurtma `DELIVERED` va topshiriq mavjud bo‘lganda egasiga (o‘z xonadoni) va
  ADMIN ga beriladi; qabulning o‘zi `POST /api/logistics/tasks/{id}/confirm` orqali. `create_task` logistika moduli ishlamasa
  ko‘rsatilmaydi.
- Egasi dashboardida `orders.open` = yakunlanmagan barcha buyurtmalar (`NEW`, `APPROVED`, `IN_DELIVERY`, `DELIVERED`),
  `awaiting_confirmation` = `DELIVERED`.
- ADMIN `APPROVED` buyurtmani bekor qilsa, hali yo‘lga chiqmagan topshiriq avval bekor qilinadi (zaxira bo‘shaydi), keyin
  buyurtma `CANCELLED`; topshiriq `IN_TRANSIT` yoki keyingi holatda bo‘lsa `409`.

3-bosqich (ombor, hisobotlar):
- `ADJUST_IN` partiya yaratmaydi (partiyasiz qoldiqqa qo‘shiladi); `ADJUST_OUT` oddiy chiqim kabi FEFO bo‘yicha sarflaydi.
- Inventarizatsiyada `lines_with_difference`: qoralamada sanalgan ≠ joriy qoldiq, tasdiqlanganda `difference ≠ 0`.
  Inventarizatsiya obyektida qo‘shimcha `allowed_actions` (`count`, `approve`, `cancel`) va qatorda `reserved`.
- Operatsiya `reference.path`: `vaccination` → `/vet?tab=vaccinations`, `treatment` → `/vet?tab=treatments`; OWNER uchun
  logistika topshirig‘i → `/my/orders/{order_id}` (buyurtmasiz bo‘lsa `/my/deliveries`), boshqalari `null`.
- Hisobotlarda `poultry_type` filtri: xonadonlar to‘plami (shu turdagi partiya harakati bor xonadonlar), parranda soni va
  nobud/o‘lim shu tur bo‘yicha; tuxum va yem hisobotda tur bo‘yicha ajratilmaydi, shuning uchun xonadon bo‘yicha qoladi.
- `reports` throttle (30/daqiqa) faqat fayl eksportiga (xlsx/pdf) qo‘llanadi; JSON ko‘rish cheklanmaydi.
- `?format=` DRF’ning renderer parametri emas — hisobot formati (maxsus content negotiation).
- §1.2 jadvalida W uchun “stock, feed, movements” deyilgan, §11.2 jadvalida esa W kunlik/oylik ishlab chiqarishni ham ko‘radi —
  batafsilroq §11.2 qo‘llangan.
- `vet_status`: “rejada” = PLANNED va sanasi bugundan keyin; “muddati o‘tgan” = PLANNED va sanasi bugundan oldin (davrga bog‘liq emas);
  “o‘tkazilgan” = davr ichida DONE. `logistics_efficiency` “O‘z vaqtida”: yetkazilgan sana ≤ reja sanasi; yetkazilmagan va
  reja sanasi o‘tgan → `Yo‘q`; qolganlari `—`.

## Fix bosqichi (V3 ko‘rib chiqish topilmalari va TZ qamrovi)

To‘liq ro‘yxat — `docs/API_CONTRACT_V3.md` §20.1, TZ bo‘yicha jadval — `docs/TZ_COVERAGE.md`. Yangi migratsiyalar:
`accounts.0004` (`sessions_revoked_at`), `audit.0003` (`household_ref`, `product_ref`, `status` + mavjud qatorlarni
to‘ldirish), `audit.0004`, `core.0003` (`STOCK_DOCUMENT`), `farm.0003` (`Household.is_demo`, `demo_*`/`integ_*`
egalari belgilanadi), `notifications.0004` (`NotificationPreference`).

- **Xavfsizlik**: vaqtinchalik parol bilan faqat `/api/auth/me|change-password|logout` va `GET /api/settings`
  (`accounts/authentication.py`, bot amallari ham `403`); sessiyalar yakunlangandan keyin eski `initData` rad etiladi;
  VET boshqa veterinar yozuvini o‘ziga ola olmaydi; fayl tanasi `Content-Length` bo‘yicha oldindan rad etiladi,
  biriktirilmagan fayllar kvotasi (20 ta / 100 MB), egasidan faqat rasm, yuklab olish `no-store` (V3 §4.4 dan og‘ish);
  egasiga ombor `balance_after`/partiya/xodim nomi ko‘rsatilmaydi; Django admin’da sozlamalar, transport, haydovchi
  faqat o‘qish uchun.
- **To‘g‘rilik**: eslatmalar guard’i faqat oldinga yuradi, `send_reminders --date` kelajak sanani rad etadi; emlash
  eslatma kalitlarida reja sanasi; yo‘lda bekor qilinganda qaytgan tovar partiya raqami/muddatini saqlaydi va xonadon
  iste’molidan chiqariladi (`warehouse.models.returned_task_out`); logistika “o‘z vaqtida” bitta qoida
  (`logistics.selectors.is_on_time/delivery_kpis`); “Muammo” faqat muammo bilan qabulda; nofaol kontekst
  foydalanuvchilari fallback’dan oldin olib tashlanadi; tasdiqlashda xonadon/mahsulot faolligi tekshiriladi.
- **TZ**: go‘sht (dashboard, overview `meat`/`series[].meat_kg`, bot xulosasi), `birds_total`, `production_12m`,
  `warehouse_summary`; overview filtrlari `region`, `district`, `poultry_type`, `exclude_demo`; `GET /api/my/batches/{id}`;
  `first_received_date`; `telegram_id_masked`; audit xonadon/mahsulot/holat filtrlari; shaxsiy bildirishnoma
  sozlamalari `/api/my/notification-preferences`; `STOCK_DOCUMENT` fayllari (`file_ids` kirim/chiqimda, `files`
  har operatsiyada); `stock_balance` hisobotida markazdagi parranda; `seed_demo` V3 modullarini ham to‘ldiradi
  (L/V xodimlar, transport, buyurtmalar, topshiriqlar, vet, partiyalar) va xonadonlarni `is_demo` qiladi.
- **Rejali vazifalar**: `..\scriptsegister_scheduled_tasks.ps1` — Task Scheduler’da `backup_db --keep 14 --with-media`
  (har kuni), `cleanup_files`, `flushexpiredtokens` (haftalik), `send_reminders` (har 15 daqiqa). Skript faqat ro‘yxatga
  oladi; o‘chirish `-Remove`.
- Real `db.sqlite3` Fix bosqichida `manage.py migrate` qilindi; `check_integrity` toza.

## Docker (server)

Serverga joylash (Postgres, gunicorn, nginx, avtomatik SSL, kunlik backup): [DEPLOY.md](DEPLOY.md).
Server `.env` namunasi: `.env.docker.example`. Local Windows `.env` (SQLite) bilan aralashtirmang.
