-- Semilla ficticia idempotente para el primer almacén de pruebas.
insert into public.warehouses(name, active, created_at)
values ('Primera prueba', 1, now()::text)
on conflict do nothing;

insert into public.locations(name, kind, warehouse_id, employee_id, created_at)
select 'General', 'central', w.id, null, now()::text
from public.warehouses w
where lower(w.name) = lower('Primera prueba')
  and w.active = 1
  and not exists (
    select 1 from public.locations l
    where l.warehouse_id = w.id
      and l.kind = 'central'
      and lower(l.name) = lower('General')
  );

