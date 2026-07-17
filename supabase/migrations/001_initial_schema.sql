-- 掌柜台正式云端数据库。由 Supabase migration 执行。
create extension if not exists pgcrypto;

create type public.shop_role as enum ('owner', 'staff', 'print_agent');
create type public.device_status as enum (
  'in_stock', 'reserved', 'sold_pending_pickup', 'sold', 'in_repair',
  'borrowed_for_test', 'peer_transfer', 'return_processing', 'scrapped'
);
create type public.inventory_event_type as enum (
  'intake', 'update', 'reserve', 'unreserve', 'sale', 'repair_out',
  'repair_return', 'borrow', 'return', 'transfer', 'stocktake', 'scrap'
);
create type public.print_job_status as enum ('queued', 'printing', 'printed', 'failed', 'cancelled');

create table public.shops (
  id uuid primary key default gen_random_uuid(),
  name text not null check (char_length(name) between 1 and 80),
  timezone text not null default 'Asia/Shanghai',
  created_at timestamptz not null default now()
);

create table public.profiles (
  user_id uuid primary key references auth.users(id) on delete cascade,
  display_name text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.shop_members (
  shop_id uuid not null references public.shops(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  role public.shop_role not null,
  active boolean not null default true,
  created_at timestamptz not null default now(),
  primary key (shop_id, user_id)
);

create table public.shop_settings (
  shop_id uuid primary key references public.shops(id) on delete cascade,
  stock_code_prefix text not null default 'A',
  label_width_mm numeric(5,2) not null default 30,
  label_height_mm numeric(5,2) not null default 40,
  currency text not null default 'CNY',
  ai_fallback_enabled boolean not null default false,
  updated_at timestamptz not null default now()
);

create table public.stock_counters (
  shop_id uuid not null references public.shops(id) on delete cascade,
  counter_date date not null,
  prefix text not null,
  last_value integer not null default 0,
  primary key (shop_id, counter_date, prefix)
);

create table public.devices (
  id uuid primary key default gen_random_uuid(),
  shop_id uuid not null references public.shops(id) on delete restrict,
  stock_code text,
  brand text not null,
  model text not null,
  storage text not null,
  color text not null default '',
  system_version text not null default '',
  battery_health smallint check (battery_health between 0 and 100),
  charge_cycles integer check (charge_cycles is null or charge_cycles >= 0),
  condition_grade text not null default '',
  list_price numeric(12,2) not null default 0 check (list_price >= 0),
  imei text,
  imei2 text,
  serial_number text,
  product_type text,
  region text,
  status public.device_status not null default 'in_stock',
  source_snapshot_path text,
  source_fields jsonb not null default '{}'::jsonb,
  notes text not null default '',
  area text not null default '默认区',
  created_by uuid not null references auth.users(id),
  updated_by uuid not null references auth.users(id),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  deleted_at timestamptz,
  unique (shop_id, stock_code)
);

create unique index devices_shop_imei_unique
  on public.devices(shop_id, imei) where imei is not null and deleted_at is null;
create index devices_shop_status_idx on public.devices(shop_id, status) where deleted_at is null;
create index devices_shop_created_idx on public.devices(shop_id, created_at desc) where deleted_at is null;

create table public.device_financials (
  device_id uuid primary key references public.devices(id) on delete restrict,
  shop_id uuid not null references public.shops(id) on delete restrict,
  purchase_cost numeric(12,2) not null check (purchase_cost >= 0),
  minimum_price numeric(12,2) check (minimum_price is null or minimum_price >= 0),
  updated_by uuid not null references auth.users(id),
  updated_at timestamptz not null default now()
);

create table public.sales (
  id uuid primary key default gen_random_uuid(),
  shop_id uuid not null references public.shops(id) on delete restrict,
  device_id uuid not null unique references public.devices(id) on delete restrict,
  sale_price numeric(12,2) not null check (sale_price >= 0),
  payment_method text not null default '',
  customer_note text not null default '',
  sold_by uuid not null references auth.users(id),
  sold_at timestamptz not null default now(),
  cancelled_at timestamptz,
  cancelled_by uuid references auth.users(id)
);

create table public.inventory_events (
  id bigint generated always as identity primary key,
  shop_id uuid not null references public.shops(id) on delete restrict,
  device_id uuid not null references public.devices(id) on delete restrict,
  event_type public.inventory_event_type not null,
  from_status public.device_status,
  to_status public.device_status,
  note text not null default '',
  metadata jsonb not null default '{}'::jsonb,
  actor_id uuid not null references auth.users(id),
  created_at timestamptz not null default now()
);
create index inventory_events_device_idx on public.inventory_events(device_id, created_at desc);

create table public.print_jobs (
  id uuid primary key default gen_random_uuid(),
  shop_id uuid not null references public.shops(id) on delete restrict,
  device_id uuid references public.devices(id) on delete restrict,
  template_key text not null default 'phone-30x40-v1',
  payload jsonb not null,
  status public.print_job_status not null default 'queued',
  attempts smallint not null default 0,
  error_message text,
  requested_by uuid not null references auth.users(id),
  requested_at timestamptz not null default now(),
  claimed_at timestamptz,
  finished_at timestamptz
);
create index print_jobs_queue_idx on public.print_jobs(shop_id, requested_at) where status = 'queued';

create or replace function public.is_shop_member(target_shop uuid)
returns boolean language sql stable security definer set search_path = public
as $$
  select exists (
    select 1 from public.shop_members
    where shop_id = target_shop and user_id = auth.uid() and active
  );
$$;

create or replace function public.has_shop_role(target_shop uuid, allowed public.shop_role[])
returns boolean language sql stable security definer set search_path = public
as $$
  select exists (
    select 1 from public.shop_members
    where shop_id = target_shop and user_id = auth.uid() and active and role = any(allowed)
  );
$$;

create or replace function public.create_shop(shop_name text)
returns uuid language plpgsql security definer set search_path = public
as $$
declare new_shop_id uuid;
begin
  if auth.uid() is null then raise exception 'not authenticated'; end if;
  insert into public.shops(name) values (trim(shop_name)) returning id into new_shop_id;
  insert into public.shop_members(shop_id, user_id, role) values (new_shop_id, auth.uid(), 'owner');
  insert into public.shop_settings(shop_id) values (new_shop_id);
  insert into public.profiles(user_id, display_name) values (auth.uid(), '') on conflict do nothing;
  return new_shop_id;
end;
$$;

create or replace function public.assign_stock_code()
returns trigger language plpgsql security definer set search_path = public
as $$
declare next_value integer; code_prefix text;
begin
  if new.stock_code is not null and new.stock_code <> '' then return new; end if;
  code_prefix := case when lower(new.brand) = 'apple' then 'A' when new.brand like '华为%' then 'H' else 'M' end;
  insert into public.stock_counters(shop_id, counter_date, prefix, last_value)
    values (new.shop_id, (now() at time zone 'Asia/Shanghai')::date, code_prefix, 1)
  on conflict (shop_id, counter_date, prefix)
    do update set last_value = public.stock_counters.last_value + 1
  returning last_value into next_value;
  new.stock_code := code_prefix || to_char(now() at time zone 'Asia/Shanghai', 'YYMMDD') || '-' || lpad(next_value::text, 3, '0');
  return new;
end;
$$;

create trigger devices_assign_stock_code before insert on public.devices
for each row execute function public.assign_stock_code();

create or replace function public.intake_device(
  device_data jsonb,
  purchase_cost numeric,
  list_price numeric,
  minimum_price numeric default null,
  queue_label boolean default true
)
returns public.devices language plpgsql security definer set search_path = public
as $$
declare new_device public.devices;
begin
  if not public.has_shop_role((device_data->>'shop_id')::uuid, array['owner','staff']::public.shop_role[]) then
    raise exception 'forbidden';
  end if;
  insert into public.devices(
    shop_id, brand, model, storage, color, system_version, battery_health,
    charge_cycles, condition_grade, list_price, imei, imei2, serial_number, product_type,
    region, source_snapshot_path, source_fields, notes, area, created_by, updated_by
  ) values (
    (device_data->>'shop_id')::uuid,
    device_data->>'brand', device_data->>'model', device_data->>'storage',
    coalesce(device_data->>'color',''), coalesce(device_data->>'system_version',''),
    nullif(device_data->>'battery_health','')::smallint,
    nullif(device_data->>'charge_cycles','')::integer,
    coalesce(device_data->>'condition_grade',''), list_price, nullif(device_data->>'imei',''),
    nullif(device_data->>'imei2',''), nullif(device_data->>'serial_number',''),
    nullif(device_data->>'product_type',''), nullif(device_data->>'region',''),
    nullif(device_data->>'source_snapshot_path',''), coalesce(device_data->'source_fields','{}'::jsonb),
    coalesce(device_data->>'notes',''), coalesce(device_data->>'area','默认区'), auth.uid(), auth.uid()
  ) returning * into new_device;

  insert into public.device_financials(device_id, shop_id, purchase_cost, minimum_price, updated_by)
    values (new_device.id, new_device.shop_id, purchase_cost, minimum_price, auth.uid());
  insert into public.inventory_events(shop_id, device_id, event_type, to_status, actor_id)
    values (new_device.shop_id, new_device.id, 'intake', 'in_stock', auth.uid());

  if queue_label then
    insert into public.print_jobs(shop_id, device_id, payload, requested_by)
      values (new_device.shop_id, new_device.id, jsonb_build_object(
        'model', new_device.model, 'system', new_device.system_version,
        'storage', new_device.storage, 'battery', new_device.battery_health,
        'serial', coalesce(new_device.imei, new_device.serial_number),
        'price', new_device.list_price, 'stock_code', new_device.stock_code
      ), auth.uid());
  end if;
  return new_device;
end;
$$;

create or replace function public.sell_device(
  target_device uuid,
  final_price numeric,
  pay_method text default '',
  customer_text text default ''
)
returns public.sales language plpgsql security definer set search_path = public
as $$
declare current_device public.devices; new_sale public.sales;
begin
  select * into current_device from public.devices where id = target_device and deleted_at is null for update;
  if current_device.id is null then raise exception 'device not found'; end if;
  if not public.has_shop_role(current_device.shop_id, array['owner','staff']::public.shop_role[]) then raise exception 'forbidden'; end if;
  if current_device.status not in ('in_stock','reserved','sold_pending_pickup') then raise exception 'device cannot be sold from current status'; end if;

  insert into public.sales(shop_id, device_id, sale_price, payment_method, customer_note, sold_by)
    values (current_device.shop_id, current_device.id, final_price, pay_method, customer_text, auth.uid())
    returning * into new_sale;
  update public.devices set status = 'sold', updated_by = auth.uid(), updated_at = now() where id = current_device.id;
  insert into public.inventory_events(shop_id, device_id, event_type, from_status, to_status, note, actor_id)
    values (current_device.shop_id, current_device.id, 'sale', current_device.status, 'sold', customer_text, auth.uid());
  return new_sale;
end;
$$;

alter table public.shops enable row level security;
alter table public.profiles enable row level security;
alter table public.shop_members enable row level security;
alter table public.shop_settings enable row level security;
alter table public.stock_counters enable row level security;
alter table public.devices enable row level security;
alter table public.device_financials enable row level security;
alter table public.sales enable row level security;
alter table public.inventory_events enable row level security;
alter table public.print_jobs enable row level security;

create policy shops_read on public.shops for select using (public.is_shop_member(id));
create policy shops_owner_update on public.shops for update using (public.has_shop_role(id, array['owner']::public.shop_role[]));
create policy profiles_self_read on public.profiles for select using (user_id = auth.uid());
create policy profiles_self_update on public.profiles for update using (user_id = auth.uid()) with check (user_id = auth.uid());
create policy members_read on public.shop_members for select using (public.is_shop_member(shop_id));
create policy members_owner_write on public.shop_members for all using (public.has_shop_role(shop_id, array['owner']::public.shop_role[])) with check (public.has_shop_role(shop_id, array['owner']::public.shop_role[]));
create policy settings_read on public.shop_settings for select using (public.is_shop_member(shop_id));
create policy settings_owner_write on public.shop_settings for all using (public.has_shop_role(shop_id, array['owner']::public.shop_role[])) with check (public.has_shop_role(shop_id, array['owner']::public.shop_role[]));

create policy devices_read on public.devices for select using (public.is_shop_member(shop_id));
create policy devices_insert on public.devices for insert with check (public.has_shop_role(shop_id, array['owner','staff']::public.shop_role[]) and created_by = auth.uid() and updated_by = auth.uid());
create policy devices_update on public.devices for update using (public.has_shop_role(shop_id, array['owner','staff']::public.shop_role[])) with check (public.has_shop_role(shop_id, array['owner','staff']::public.shop_role[]) and updated_by = auth.uid());

create policy financials_owner_read on public.device_financials for select using (public.has_shop_role(shop_id, array['owner']::public.shop_role[]));
create policy financials_owner_write on public.device_financials for all using (public.has_shop_role(shop_id, array['owner']::public.shop_role[])) with check (public.has_shop_role(shop_id, array['owner']::public.shop_role[]));

create policy sales_read on public.sales for select using (public.has_shop_role(shop_id, array['owner','staff']::public.shop_role[]));
create policy events_read on public.inventory_events for select using (public.is_shop_member(shop_id));
create policy print_jobs_member_read on public.print_jobs for select using (public.is_shop_member(shop_id));
create policy print_jobs_request on public.print_jobs for insert with check (public.has_shop_role(shop_id, array['owner','staff']::public.shop_role[]) and requested_by = auth.uid());
create policy print_jobs_agent_update on public.print_jobs for update using (public.has_shop_role(shop_id, array['owner','print_agent']::public.shop_role[])) with check (public.has_shop_role(shop_id, array['owner','print_agent']::public.shop_role[]));

insert into storage.buckets(id, name, public) values ('device-snapshots', 'device-snapshots', false)
on conflict (id) do nothing;
create policy snapshot_read on storage.objects for select using (
  bucket_id = 'device-snapshots' and public.is_shop_member((storage.foldername(name))[1]::uuid)
);
create policy snapshot_upload on storage.objects for insert with check (
  bucket_id = 'device-snapshots' and public.has_shop_role((storage.foldername(name))[1]::uuid, array['owner','staff']::public.shop_role[])
);
