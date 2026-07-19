pragma foreign_keys = on;

create table if not exists shops (
  id text primary key,
  name text not null,
  timezone text not null default 'Asia/Shanghai',
  created_at text not null
);

create table if not exists users (
  id text primary key,
  shop_id text not null references shops(id),
  username text not null collate nocase,
  display_name text not null,
  role text not null check (role in ('owner','staff')),
  password_hash text not null,
  password_salt text not null,
  active integer not null default 1,
  created_at text not null,
  unique (shop_id, username)
);

create table if not exists sessions (
  token_hash text primary key,
  user_id text not null references users(id) on delete cascade,
  expires_at text not null,
  created_at text not null,
  last_seen_at text not null
);

create table if not exists counters (
  shop_id text not null references shops(id),
  counter_date text not null,
  prefix text not null,
  last_value integer not null default 0,
  primary key (shop_id, counter_date, prefix)
);

create table if not exists devices (
  id text primary key,
  shop_id text not null references shops(id),
  stock_code text not null,
  brand text not null,
  model text not null,
  storage text not null,
  color text not null default '',
  system_version text not null default '',
  battery_health integer check (battery_health between 0 and 100),
  charge_cycles integer check (charge_cycles is null or charge_cycles >= 0),
  condition_grade text not null default '',
  list_price real not null default 0 check (list_price >= 0),
  imei text,
  imei2 text,
  serial_number text,
  status text not null default 'in_stock' check (status in ('in_stock','reserved','sold_pending_pickup','sold','in_repair','borrowed_for_test','peer_transfer','return_processing','scrapped')),
  area text not null default '默认区',
  notes text not null default '',
  source_fields text not null default '{}',
  intake_state text not null default 'complete' check (intake_state in ('pending','complete')),
  created_by text not null references users(id),
  updated_by text not null references users(id),
  created_at text not null,
  updated_at text not null,
  deleted_at text,
  unique (shop_id, stock_code)
);
create unique index if not exists devices_shop_imei_unique on devices(shop_id, imei) where imei is not null and deleted_at is null;
create index if not exists devices_shop_status_idx on devices(shop_id, status) where deleted_at is null;

create table if not exists device_financials (
  device_id text primary key references devices(id),
  shop_id text not null references shops(id),
  purchase_cost real not null check (purchase_cost >= 0),
  minimum_price real check (minimum_price is null or minimum_price >= 0),
  updated_by text not null references users(id),
  updated_at text not null
);

create table if not exists sales (
  id text primary key,
  shop_id text not null references shops(id),
  device_id text not null references devices(id),
  sale_price real not null check (sale_price >= 0),
  payment_method text not null default '',
  customer_note text not null default '',
  sold_by text not null references users(id),
  sold_at text not null,
  model_snapshot text not null default '',
  storage_snapshot text not null default '',
  imei_snapshot text not null default '',
  purchase_cost_snapshot real not null default 0,
  gift_case integer not null default 0,
  gift_screen_protector integer not null default 0,
  gift_charging_head integer not null default 0,
  gift_charger integer not null default 0,
  warranty_days integer not null default 30 check (warranty_days between 0 and 3650),
  warranty_expires_at text,
  updated_at text,
  updated_by text references users(id)
);
create index if not exists sales_device_idx on sales(device_id, sold_at desc);

create table if not exists after_sales_cases (
  id text primary key,
  shop_id text not null references shops(id),
  device_id text not null references devices(id),
  sale_id text not null references sales(id),
  issue text not null,
  status text not null default 'open' check (status in ('open','resolved')),
  resolution text not null default '',
  service_cost real not null default 0 check (service_cost >= 0),
  created_by text not null references users(id),
  closed_by text references users(id),
  created_at text not null,
  updated_at text not null,
  closed_at text
);
create index if not exists after_sales_device_idx on after_sales_cases(device_id, created_at desc);

create table if not exists customer_handoffs (
  id text primary key,
  shop_id text not null references shops(id),
  device_id text not null references devices(id),
  sale_id text not null unique references sales(id),
  handoff_number text not null,
  access_token_hash text not null unique,
  token_hint text not null,
  status text not null default 'active' check (status in ('active','void')),
  snapshot text not null,
  created_by text not null references users(id),
  created_at text not null,
  reissued_at text,
  voided_at text,
  last_viewed_at text,
  view_count integer not null default 0,
  unique (shop_id, handoff_number)
);
create index if not exists customer_handoffs_device_idx on customer_handoffs(device_id, created_at desc);

create table if not exists customer_handoff_events (
  id integer primary key autoincrement,
  handoff_id text not null references customer_handoffs(id),
  event_type text not null check (event_type in ('created','view','download','reissue','void')),
  actor_id text references users(id) on delete set null,
  client_ip text not null default '',
  details text not null default '{}',
  created_at text not null
);
create index if not exists customer_handoff_events_idx on customer_handoff_events(handoff_id, created_at desc, id desc);

create table if not exists audit_events (
  id integer primary key autoincrement,
  shop_id text references shops(id),
  actor_id text references users(id) on delete set null,
  actor_name text not null default '',
  actor_role text not null default '',
  action text not null,
  entity_type text not null default '',
  entity_id text not null default '',
  summary text not null,
  details text not null default '{}',
  success integer not null default 1,
  client_ip text not null default '',
  created_at text not null
);
create index if not exists audit_events_shop_time_idx on audit_events(shop_id, created_at desc, id desc);
create trigger if not exists audit_events_no_update before update on audit_events begin select raise(abort, 'audit events are immutable'); end;
create trigger if not exists audit_events_no_delete before delete on audit_events begin select raise(abort, 'audit events are immutable'); end;

create table if not exists schema_migrations (
  version integer primary key,
  applied_at text not null
);

create table if not exists inventory_events (
  id integer primary key autoincrement,
  shop_id text not null references shops(id),
  device_id text not null references devices(id),
  event_type text not null,
  from_status text,
  to_status text,
  note text not null default '',
  metadata text not null default '{}',
  actor_id text not null references users(id),
  created_at text not null
);
create index if not exists events_device_idx on inventory_events(device_id, created_at desc);

create table if not exists print_jobs (
  id text primary key,
  shop_id text not null references shops(id),
  device_id text references devices(id),
  payload text not null,
  status text not null default 'queued' check (status in ('queued','printing','printed','failed','cancelled')),
  attempts integer not null default 0,
  error_message text,
  requested_by text not null references users(id),
  requested_at text not null,
  finished_at text
);

create table if not exists app_settings (
  shop_id text not null references shops(id),
  setting_key text not null,
  setting_value text not null,
  updated_by text references users(id),
  updated_at text not null,
  primary key (shop_id, setting_key)
);

create table if not exists device_photos (
  id text primary key,
  shop_id text not null references shops(id),
  device_id text not null references devices(id),
  photo_type text not null default 'other',
  file_path text not null,
  description text not null default '',
  created_by text not null references users(id),
  created_at text not null
);

create table if not exists reservations (
  id text primary key,
  shop_id text not null references shops(id),
  device_id text not null references devices(id),
  customer_name text not null,
  customer_phone text not null default '',
  deposit real not null default 0 check (deposit >= 0),
  expires_at text,
  status text not null default 'active' check (status in ('active','completed','cancelled','expired')),
  note text not null default '',
  created_by text not null references users(id),
  created_at text not null,
  updated_at text not null
);

create table if not exists repairs (
  id text primary key,
  shop_id text not null references shops(id),
  device_id text not null references devices(id),
  vendor text not null default '',
  issue text not null,
  cost real not null default 0 check (cost >= 0),
  status text not null default 'sent' check (status in ('sent','repairing','completed','cancelled')),
  sent_at text not null,
  returned_at text,
  created_by text not null references users(id),
  updated_at text not null
);

create table if not exists returns (
  id text primary key,
  shop_id text not null references shops(id),
  device_id text not null references devices(id),
  sale_id text references sales(id),
  refund_amount real not null default 0 check (refund_amount >= 0),
  reason text not null,
  disposition text not null check (disposition in ('restock','repair','scrap')),
  created_by text not null references users(id),
  created_at text not null
);

create table if not exists stocktakes (
  id text primary key,
  shop_id text not null references shops(id),
  area text not null default '',
  status text not null default 'open' check (status in ('open','completed','cancelled')),
  started_by text not null references users(id),
  started_at text not null,
  completed_at text
);

create table if not exists stocktake_items (
  stocktake_id text not null references stocktakes(id) on delete cascade,
  device_id text not null references devices(id),
  scanned_by text not null references users(id),
  scanned_at text not null,
  primary key (stocktake_id, device_id)
);

create table if not exists market_quotes (
  id text primary key,
  shop_id text not null references shops(id),
  source_name text not null,
  quote_type text not null check (quote_type in ('recycle','retail')),
  brand text not null default '',
  model text not null,
  storage text not null,
  condition_grade text not null default '',
  battery_health integer check (battery_health is null or battery_health between 0 and 100),
  repair_status text not null default 'unknown' check (repair_status in ('original','no_repair','minor_repair','major_repair','unknown')),
  price real not null check (price > 0),
  captured_on text not null,
  note text not null default '',
  created_by text not null references users(id),
  created_at text not null
);
create index if not exists market_quotes_lookup_idx on market_quotes(shop_id, model, storage, captured_on desc);

create table if not exists pricing_decisions (
  id text primary key,
  shop_id text not null references shops(id),
  device_id text references devices(id),
  brand text not null default '',
  model text not null,
  storage text not null,
  condition_grade text not null default '',
  battery_health integer check (battery_health is null or battery_health between 0 and 100),
  repair_status text not null default 'unknown' check (repair_status in ('original','no_repair','minor_repair','major_repair','unknown')),
  suggested_purchase_low real,
  suggested_purchase_high real,
  suggested_sale_low real,
  suggested_sale_high real,
  final_purchase_price real,
  final_sale_price real,
  adjustment_reason text not null default '',
  evidence_snapshot text not null default '{}',
  created_by text not null references users(id),
  created_at text not null
);
create index if not exists pricing_decisions_lookup_idx on pricing_decisions(shop_id, model, storage, created_at desc);

create table if not exists market_sheet_imports (
  id text primary key,
  shop_id text not null references shops(id),
  source_name text not null,
  captured_on text not null,
  image_url text not null,
  file_path text not null,
  row_count integer not null default 0,
  quote_count integer not null default 0,
  created_by text not null references users(id),
  created_at text not null
);
create index if not exists market_sheet_imports_idx on market_sheet_imports(shop_id, captured_on desc);

create table if not exists market_feed_runs (
  id text primary key,
  shop_id text not null references shops(id),
  source_name text not null,
  page_id text not null,
  page_url text not null,
  captured_on text not null,
  image_url text not null default '',
  file_path text not null default '',
  status text not null check (status in ('success','rejected','error')),
  expected_row_count integer not null default 0,
  row_count integer not null default 0,
  quote_count integer not null default 0,
  imported_count integer not null default 0,
  skipped_count integer not null default 0,
  message text not null default '',
  created_at text not null
);
create index if not exists market_feed_runs_idx on market_feed_runs(shop_id, page_id, captured_on desc, created_at desc);
