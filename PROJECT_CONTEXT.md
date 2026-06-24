# Project Context

## Repository Purpose

This repository is an independent client deployment for a Telegram subscription bot and admin dashboard.

The app provides:

- A Telegram bot powered by aiogram 3 long polling
- Receipt submission and admin approval/rejection flow
- Dynamic channel access loaded from Supabase `access_channels`
- One-use Telegram invite links
- Manual invite link creation
- Renewal reminder scheduling
- Expired access preview and removal confirmation
- Telegram ID blacklist management
- Payment history storage and dashboard views
- A FastAPI/Jinja admin dashboard
- Railway deployment configuration

## Isolation Rules

All work must stay inside this repository.

Do not reference, depend on, copy from, or assume access to any outside deployment, bot, channel list, token, database, Railway service, or environment configuration.

Do not assume any Telegram channel IDs exist. Client channel IDs must be discovered for this deployment and stored in this repository's configured Supabase `access_channels` table.

If a feature from another deployment is requested here, implement it only in this repository using this repository's code, schema, and environment variables.

## Runtime Shape

Primary files:

- `main.py` contains the Telegram bot, FastAPI dashboard, scheduler, and Supabase integration.
- `schema.sql` defines the Supabase tables used by this deployment.
- `templates/` contains dashboard and login templates.
- `railway.json` starts the app with `python main.py`.
- `requirements.txt` lists Python dependencies.

Required environment variables:

- `BOT_TOKEN`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `ADMIN_CHAT_ID`
- `ADMIN_USER_IDS`
- `ADMIN_PASSWORD`
- `CONTENT_CHANNEL_ID`
- `AUTO_REMOVE_EXPIRED`
- `RENEWAL_NOTICE_DAYS`

Optional environment variables:

- `PORT`
- `SESSION_SECRET`

## Data Model Notes

The app expects these Supabase tables from `schema.sql`:

- `telegram_users`
- `access_channels`
- `user_channel_access`
- `payment_history`
- `manual_invite_links`
- `blacklist`

Channel configuration must come from `access_channels`; channel IDs should not be hardcoded in Python code.

## Development Notes

Install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run locally:

```bash
python main.py
```

Compile check:

```bash
python3 -m py_compile main.py
```
