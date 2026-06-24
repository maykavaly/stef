create table if not exists public.telegram_users (
  telegram_id bigint primary key,
  username text,
  first_name text,
  last_name text,
  status text default 'inactive',
  payment_status text default 'unpaid',
  pending_payment_file_id text,
  pending_payment_file_type text,
  pending_payment_at timestamptz,
  approved_by_admin_id bigint,
  approved_at timestamptz,
  rejected_at timestamptz,
  needs_new_receipt_at timestamptz,
  membership_start_date date,
  expiry_date date,
  joined_at timestamptz,
  registered_at timestamptz default now(),
  last_payment_at timestamptz,
  invite_link text,
  invite_link_created_at timestamptz,
  invite_link_name text,
  invite_link_revoked boolean default false,
  invite_link_used boolean default false,
  joined_channel_at timestamptz,
  left_channel_at timestamptz,
  last_seen_at timestamptz,
  renewal_notice_7d_sent_at timestamptz,
  renewal_notice_3d_sent_at timestamptz,
  renewal_notice_1d_sent_at timestamptz,
  removed_at timestamptz,
  removal_reason text,
  source text,
  notes text,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

create table if not exists public.access_channels (
  id bigserial primary key,
  code text not null unique,
  title text not null,
  telegram_chat_id text not null,
  has_expiry boolean default true,
  is_active boolean default true,
  sort_order integer default 100,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

create table if not exists public.user_channel_access (
  id bigserial primary key,
  telegram_id bigint not null,
  channel_code text not null,
  channel_title text,
  telegram_chat_id text,
  invite_link text,
  invite_link_name text,
  invite_link_created_at timestamptz,
  invite_link_revoked boolean default false,
  invite_link_used boolean default false,
  status text default 'active',
  granted_at timestamptz default now(),
  joined_channel_at timestamptz,
  left_channel_at timestamptz,
  expires_at date,
  removed_at timestamptz,
  removal_reason text,
  created_at timestamptz default now(),
  updated_at timestamptz default now(),
  unique (telegram_id, channel_code)
);

create table if not exists public.payment_history (
  id bigserial primary key,
  telegram_id bigint not null,
  username text,
  first_name text,
  admin_id bigint,
  action text default 'approved',
  payment_status text default 'paid',
  receipt_file_id text,
  receipt_file_type text,
  invite_link text,
  membership_start_date date,
  expiry_date date,
  verified boolean default true,
  notes text,
  created_at timestamptz default now()
);

create table if not exists public.manual_invite_links (
  id bigserial primary key,
  channel_code text not null,
  telegram_chat_id text not null,
  invite_link text not null,
  invite_link_name text,
  created_by_admin_id bigint,
  created_at timestamptz default now(),
  expires_at timestamptz,
  used_by_telegram_id bigint,
  used_at timestamptz,
  revoked boolean default false,
  revoked_at timestamptz,
  notes text
);

create table if not exists public.blacklist (
  telegram_id bigint primary key,
  blocked_at timestamptz default now(),
  reason text
);

create index if not exists idx_telegram_users_status on public.telegram_users(status);
create index if not exists idx_telegram_users_payment_status on public.telegram_users(payment_status);
create index if not exists idx_telegram_users_expiry_date on public.telegram_users(expiry_date);
create index if not exists idx_access_channels_active on public.access_channels(is_active, sort_order);
create index if not exists idx_user_channel_access_telegram_id on public.user_channel_access(telegram_id);
create index if not exists idx_user_channel_access_channel_code on public.user_channel_access(channel_code);
create index if not exists idx_user_channel_access_expires_at on public.user_channel_access(expires_at);
create index if not exists idx_payment_history_telegram_id on public.payment_history(telegram_id);
create index if not exists idx_payment_history_created_at on public.payment_history(created_at desc);
create index if not exists idx_manual_invite_links_invite_link on public.manual_invite_links(invite_link);
create index if not exists idx_manual_invite_links_channel_code on public.manual_invite_links(channel_code);
