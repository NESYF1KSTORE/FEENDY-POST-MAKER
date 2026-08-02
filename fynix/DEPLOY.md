# Развёртывание FYNIX AI Pipeline

Стек: **Caddy** (TLS) → **FastAPI** (API + портал) + **worker** → **PostgreSQL 16** + **Redis 7**.
Минимальный сервер: 2 vCPU / 4 ГБ RAM / 40 ГБ диска, Ubuntu 22.04 или 24.04.

---

## Вариант 0. Через GitHub Actions, без терминала

Самый короткий путь, если не хочется заходить на сервер руками.

1. **Settings → Secrets and variables → Actions → New repository secret**, добавьте:

   | Секрет | Значение |
   |---|---|
   | `DEPLOY_HOST` | IP-адрес сервера |
   | `DEPLOY_USER` | `root` |
   | `DEPLOY_SSH_KEY` | приватный SSH-ключ — **предпочтительно** |
   | `DEPLOY_PASSWORD` | пароль root — если ключа нет |
   | `DEPLOY_DOMAIN` | домен для TLS (необязательно) |

   Достаточно одного из `DEPLOY_SSH_KEY` / `DEPLOY_PASSWORD`.

2. **Actions → FYNIX Deploy → Run workflow**.

Workflow синхронизирует исходники, запускает установщик, проверяет
`/health/ready` и прогоняет офлайн-конвейер целиком, чтобы убедиться, что
платформа на сервере действительно работает. Ссылки на портал появятся в
Summary запуска.

**О пароле в секретах.** Секрет репозитория может прочитать не только владелец:
любой, кто вправе добавить в репозиторий workflow, может его вывести. Пароль
root — это полный доступ к серверу. Поэтому:

```bash
# один раз, со своей машины — и дальше пароль в секретах не нужен
ssh-keygen -t ed25519 -f ~/.ssh/fynix_deploy -N ''
ssh-copy-id -i ~/.ssh/fynix_deploy.pub root@<IP-адрес>
cat ~/.ssh/fynix_deploy      # это значение в секрет DEPLOY_SSH_KEY
```

После этого удалите `DEPLOY_PASSWORD` и смените пароль root.

---

## Вариант 1. Одной командой на сервере

```bash
ssh root@<IP-адрес>

curl -fsSL https://raw.githubusercontent.com/nesyf1kstore/feendy-post-maker/claude/deploy-project-server-yapfn4/fynix/deploy/install.sh \
  | bash -s -- --domain fynix.example.com
```

Без домена (первый запуск по IP, без TLS):

```bash
curl -fsSL .../install.sh | bash
```

Скрипт делает:

1. ставит Docker и docker compose;
2. настраивает `ufw` — открыты только 22, 80, 443;
3. клонирует репозиторий в `/opt/fynix`;
4. генерирует `.env` со свежими секретами (`openssl rand`), права `600`;
5. поднимает стек, применяет миграции, ждёт `/health/ready`;
6. создаёт тенант `fynix` и первого администратора.

Скрипт **идемпотентен**: повторный запуск обновляет установку и сохраняет
существующий `.env` — секреты и данные не теряются.

По завершении:

```
Portal:  https://fynix.example.com/portal
API doc: https://fynix.example.com/docs

grep BOOTSTRAP_ADMIN /opt/fynix/fynix/.env      # логин и пароль администратора
```

Установщик **не печатает пароль администратора** в вывод: транскрипт деплоя
переживает сессию — он остаётся в логах CI, в истории терминала и на скриншотах.
Пароль лежит только в `.env` с правами `600`.

**Смените пароль администратора после первого входа.**

---

## Вариант 2. Push по SSH со своей машины

Когда сервер не должен ходить в GitHub сам:

```bash
cd fynix
./deploy/push.sh --host <IP-адрес> --domain fynix.example.com
```

Скрипт синхронизирует исходники по `rsync` и запускает установщик удалённо.
Аутентификация — по вашему SSH-ключу:

```bash
ssh-copy-id root@<IP-адрес>
```

Если сервер принимает только пароль, поставьте `sshpass` и передайте его через
переменную окружения (не аргументом — иначе он попадёт в историю команд):

```bash
export SSHPASS='...'
./deploy/push.sh --host <IP-адрес>
```

Ключ лучше пароля: пароль в переменной окружения виден в `/proc` другим
процессам на вашей машине.

---

## Вариант 3. Вручную

```bash
git clone -b claude/deploy-project-server-yapfn4 \
  https://github.com/nesyf1kstore/feendy-post-maker.git /opt/fynix
cd /opt/fynix/fynix

cp .env.example .env
# заполните POSTGRES_PASSWORD, JWT_SECRET, FYNIX_DOMAIN, FYNIX_BASE_URL
chmod 600 .env

docker compose up -d --build
docker compose exec api python -m app.cli bootstrap
```

Миграции применяет отдельный сервис `migrate`, от которого зависят `api` и
`worker` — они не стартуют до успешного `alembic upgrade head`.

---

## Переменные окружения

| Переменная | Обязательна | Назначение |
|---|---|---|
| `FYNIX_ENV` | да | `dev` / `test` / `staging` / `prod` |
| `FYNIX_DOMAIN` | да | домен для Caddy; `:80` — работать по HTTP без TLS |
| `FYNIX_BASE_URL` | да | публичный URL; в `prod` обязан начинаться с `https://` |
| `POSTGRES_PASSWORD` | да | пароль БД |
| `JWT_SECRET` | да | подпись токенов, ≥ 32 символов |
| `AI_DEFAULT_PROVIDER` | нет | `mock` (по умолчанию), `anthropic`, `deepseek` |
| `ANTHROPIC_API_KEY` / `DEEPSEEK_API_KEY` | нет | ключи провайдеров |
| `RUNNER_DRIVER` | нет | `local` (по умолчанию) или `docker` |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | нет | уведомления |
| `DEFAULT_PROJECT_BUDGET_RUB` | нет | бюджет проекта по умолчанию, 50 000 |

В `prod` приложение **отказывается стартовать**, если `JWT_SECRET` остался
плейсхолдером, если в `DATABASE_URL` дефолтный пароль или если `FYNIX_BASE_URL`
не `https://`. Это сделано намеренно: тихий запуск с дефолтными секретами хуже
громкого отказа.

---

## Включение реальных моделей

По умолчанию стоит `AI_DEFAULT_PROVIDER=mock` — конвейер работает целиком, но без
обращений к внешним моделям и без расходов. Так безопаснее принимать первый деплой.

```bash
cd /opt/fynix/fynix
nano .env          # AI_DEFAULT_PROVIDER=anthropic, ANTHROPIC_API_KEY=sk-ant-...
docker compose up -d --force-recreate api worker
```

Классы данных ограничивают, что уходит наружу (§8.1). По умолчанию тенант
выпускает наружу не выше `internal`; `restricted` (персональные данные, секреты,
дампы production) не уходит внешнему провайдеру ни при каких настройках.

---

## Изоляция runner'ов

`RUNNER_DRIVER=local` — работа агента идёт во временном каталоге внутри
контейнера воркера. Этого достаточно, пока агенты не исполняют недоверенный код.

`RUNNER_DRIVER=docker` даёт каждому запуску отдельный контейнер: без сети,
read-only root, non-root пользователь, `cap-drop ALL`, `no-new-privileges`,
лимиты CPU/памяти/pids. Для этого воркеру нужен доступ к Docker-сокету:

```yaml
  worker:
    volumes:
      - fynixdata:/var/lib/fynix
      - /var/run/docker.sock:/var/run/docker.sock   # добавить осознанно
```

**Взвесьте риск:** доступ к сокету равносилен root на хосте. Для production по
ТЗ (§4.3) правильнее вынести runner'ы на отдельные узлы или microVM, а не давать
сокет воркеру Control Plane.

---

## Эксплуатация

```bash
cd /opt/fynix/fynix

docker compose ps
docker compose logs -f api worker
docker compose restart api worker

docker compose exec api python -m app.cli verify-audit --slug fynix   # цепочка audit
docker compose exec api python -m app.cli reconcile                   # orphan cleanup
docker compose exec api python -m app.cli demo                        # офлайн-прогон
```

### Мониторинг

- `GET /health` — liveness, не трогает БД.
- `GET /health/ready` — readiness, отдаёт 503 при недоступной БД.
- `GET /metrics` — Prometheus. Caddy отдаёт по нему 403 наружу; скрейпить нужно
  изнутри сети compose или через отдельный внутренний порт.
- `GET /v1/dashboard` — SLI по проектам, workflow, AI, стоимости и инцидентам.

Что стоит завести алерты на (§13): `fynix_outbox_pending` растёт, jobs в статусе
`dead`, `budget.threshold.reached` с порогом 100, любой rollback, провал
`audit:verify`.

---

## Обновление

```bash
cd /opt/fynix
git pull
cd fynix
docker compose up -d --build
```

Миграции применяются автоматически сервисом `migrate`. Схема меняется по правилу
expand-and-contract (§12.1): версии N и N−1 совместимы во время выката.

---

## Резервное копирование и восстановление

Копировать нужно три вещи: базу, том с артефактами и `.env`.

```bash
cd /opt/fynix/fynix

# бэкап
docker compose exec -T postgres pg_dump -U fynix fynix | gzip > /root/fynix-$(date +%F).sql.gz
docker run --rm -v fynix_fynixdata:/data -v /root:/backup alpine \
  tar czf /backup/fynix-data-$(date +%F).tar.gz -C /data .
cp .env /root/fynix-env-$(date +%F).bak

# восстановление
gunzip -c /root/fynix-2026-08-02.sql.gz | docker compose exec -T postgres psql -U fynix fynix
docker run --rm -v fynix_fynixdata:/data -v /root:/backup alpine \
  tar xzf /backup/fynix-data-2026-08-02.tar.gz -C /data
```

Ежедневный бэкап через cron:

```bash
echo '0 3 * * * cd /opt/fynix/fynix && docker compose exec -T postgres pg_dump -U fynix fynix | gzip > /root/backups/fynix-$(date +\%F).sql.gz' | crontab -
```

**AC-12 требует регулярной проверки восстановления, а не только бэкапа.**
Восстановите дамп на отдельный сервер и убедитесь, что метаданные целы и
`verify-audit` проходит. Автоматизации этой проверки в текущей версии нет —
поставьте её на расписание отдельной задачей.

---

## Первые шаги после установки

1. Смените пароль администратора; включите MFA для привилегированных ролей.
2. Заведите пользователей с реальными ролями:
   `POST /v1/admin/users`, `POST /v1/admin/roles:grant`. Не работайте под
   platform_admin постоянно — матрица согласований требует разных людей.
3. Создайте проект и подайте бриф: `POST /v1/projects`, `POST /v1/projects/{id}/briefs`.
4. Утвердите blueprint в портале — до этого production-код не генерируется.
5. Настройте уведомления: `TELEGRAM_BOT_TOKEN` или `POST /v1/webhooks`.

---

## Диагностика

| Симптом | Причина | Что делать |
|---|---|---|
| `refusing to start: JWT_SECRET must be set` | плейсхолдер в `prod` | `openssl rand -hex 32` в `.env`, пересоздать `api` и `worker` |
| Caddy не выдаёт сертификат | A-запись домена не указывает на сервер | проверить DNS; временно `FYNIX_DOMAIN=:80` |
| `/health/ready` отдаёт 503 | БД не поднялась | `docker compose logs postgres` |
| Задачи висят в `ready` | воркер не работает | `docker compose logs worker`, `docker compose restart worker` |
| Джобы в статусе `dead` | постоянная ошибка | `docker compose logs worker \| grep job.permanent_failure` |
| Запуски отклоняются с `budget_hard_stop` | бюджет исчерпан | `POST /v1/projects/{id}/budget:override` (нужен MFA) |
| Деплой отклонён с `approval_pending` | нет двойного подтверждения | собрать подписи в портале |
| `audit:verify` вернул `intact: false` | журнал изменён вне приложения | инцидент SEV-1: сохранить состояние, разбираться |
