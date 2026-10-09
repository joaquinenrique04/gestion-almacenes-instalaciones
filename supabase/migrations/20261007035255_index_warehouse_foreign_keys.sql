-- Indexes for the foreign keys used by inventory and user access queries.
create index if not exists idx_employees_warehouse_id on public.employees(warehouse_id);
create index if not exists idx_locations_warehouse_id on public.locations(warehouse_id);
create index if not exists idx_movement_lines_movement_id on public.movement_lines(movement_id);
create index if not exists idx_movement_lines_source_location on public.movement_lines(source_location_id);
create index if not exists idx_movement_lines_destination_location on public.movement_lines(destination_location_id);
create index if not exists idx_movement_serials_movement_line on public.movement_serials(movement_line_id);
create index if not exists idx_movement_serials_serial_unit on public.movement_serials(serial_unit_id);
create index if not exists idx_movement_serials_source_location on public.movement_serials(source_location_id);
create index if not exists idx_movement_serials_destination_location on public.movement_serials(destination_location_id);
create index if not exists idx_serial_units_item on public.serial_units(item_id);
create index if not exists idx_user_accounts_role on public.user_accounts(role_id);
create index if not exists idx_user_warehouse_access_warehouse on public.user_warehouse_access(warehouse_id);
create index if not exists idx_auth_sessions_user on public.auth_sessions(user_id);

