# Telegram Subscription Bot Client Template

This folder is a clean reusable template for a new independent Telegram subscription bot. It is intentionally isolated and contains no existing client channel names, IDs, prediction games, Tirada Mundial code, or campaign text.

## What Is Included

- Telegram bot with aiogram 3 long polling
- Private payment receipt submission
- Admin approval/rejection flow
- Dynamic multi-channel access from Supabase `access_channels`
- One-use invite links that expire after 1 hour
- Manual invite links with `/manual_open_link`
- Dashboard with password login
- Supabase integration
- Railway deployment config
- Renewal reminder scheduler
- Expired user preview and removal confirmation
- Telegram ID blacklist
- Payment history for approved payments
- Per-channel access tracking in `user_channel_access`

## 1. Create A New Telegram Bot In BotFather

1. Open Telegram and message `@BotFather`.
2. Run `/newbot`.
3. Choose a bot name and username.
4. Copy the bot token.
5. Save it as `BOT_TOKEN` in Railway later.

## 2. Create A New Supabase Project

1. Open Supabase.
2. Create a new project.
3. Copy:
   - Project URL
   - Service role key
4. Save them as:
   - `SUPABASE_URL`
   - `SUPABASE_SERVICE_ROLE_KEY`

Do not expose the service role key publicly.

## 3. Run `schema.sql`

1. Open the Supabase SQL Editor.
2. Paste the full contents of `schema.sql`.
3. Run the query.

This creates only these tables:

- `telegram_users`
- `payment_history`
- `access_channels`
- `user_channel_access`
- `manual_invite_links`
- `blacklist`

## 4. Create A Railway Project

1. Create a new Railway project.
2. Connect the GitHub repo or upload this template as its own project.
3. Railway will run:

```bash
python main.py
```

## 5. Add Environment Variables

Add these variables in Railway:

```env
BOT_TOKEN=
SUPABASE_URL=
SUPABASE_SERVICE_ROLE_KEY=
ADMIN_CHAT_ID=
ADMIN_USER_IDS=
ADMIN_PASSWORD=
CONTENT_CHANNEL_ID=
AUTO_REMOVE_EXPIRED=false
RENEWAL_NOTICE_DAYS=7,3,1
```

`ADMIN_USER_IDS` can contain multiple Telegram IDs separated by commas:

```env
ADMIN_USER_IDS=123456789,987654321
```

## 6. Add Bot As Admin To Client Channels

For each client channel:

1. Add the bot to the channel.
2. Promote the bot to admin.
3. Allow the bot to create invite links.
4. Allow the bot to ban users if you want expired removal to work.

## 7. Insert Client Channels Into `access_channels`

Use `/chat_id` inside each channel or group to get the Telegram chat ID.

Example insert:

```sql
insert into public.access_channels (
  code,
  title,
  telegram_chat_id,
  has_expiry,
  is_active,
  sort_order
) values (
  'grupo',
  'Grupo',
  '-1001234567890',
  true,
  true,
  10
);
```

Add one row per channel. Do not hardcode channel IDs in `main.py`; the bot loads active channels from Supabase.

`has_expiry=true` means the channel participates in renewal and expiration logic.

`has_expiry=false` means the channel can receive invite links but does not expire.

## 8. Deploy

1. Deploy the Railway service.
2. Confirm the logs show:

```text
Starting web dashboard on port ...
Starting Telegram bot polling
Renewal reminder job registered
```

3. Open the Railway public URL.
4. Log in with `ADMIN_PASSWORD`.

## 9. Test Core Commands

Test these commands before using the bot with clients:

```text
/chat_id
/manual_open_link grupo
/users
/renewal_preview
```

Also test:

```text
/pending_payments
/expired
/remove_expired_preview
/blacklist <telegram_id>
/unblacklist <telegram_id>
/check_blacklist <telegram_id>
```

## Payment Flow

1. A non-admin user opens the bot privately.
2. The user sends a receipt image or document.
3. The bot stores the pending payment in `telegram_users`.
4. The admin receives the receipt with channel selection buttons.
5. The admin selects one or more channels.
6. The admin taps `Approve selected ✅`.
7. The bot creates one invite link per selected channel.
8. The bot sends the links to the user by DM.
9. The bot stores approved payment history in `payment_history`.

If a user sends another receipt while already pending review, the bot updates the existing pending receipt instead of creating duplicate admin alerts.

## Manual Invite Links

Use:

```text
/manual_open_link grupo
```

The bot creates a one-use invite link for that channel and replies only to the admin.

## Blacklist

Use:

```text
/blacklist <telegram_id>
/unblacklist <telegram_id>
/check_blacklist <telegram_id>
```

Blacklisted non-admin users are ignored by user-generated bot interactions.

## Dashboard

The dashboard includes:

- User list
- Payment status badges
- Membership dates
- Copy ID button
- Copy invite link button
- Renew +30 days
- Mark inactive
- Payment history page

## Local Development

Install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run:

```bash
python main.py
```

Compile check:

```bash
python3 -m py_compile main.py
```
