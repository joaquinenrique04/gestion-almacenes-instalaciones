# Gestión de almacenes e instalaciones

Prototipo web local para personal, catálogo, ingresos y transferencias de almacén. El diseño toma como referencia funcional las pantallas observadas en el sistema actual de Cystel; no representa su esquema de base de datos.

## Ejecutar en desarrollo

Requiere Python 3.10 o posterior. SQLite funciona sin paquetes externos.

```powershell
cd gestion-almacenes-instalaciones
py app.py
```

Luego abrir `http://127.0.0.1:8765`. La base SQLite se crea en `instance/gestion.sqlite3`. Para una base temporal distinta, definir `GESTION_DB_PATH` antes de iniciar el servidor. Detenerlo con `Ctrl+C`.

### Usar Supabase PostgreSQL como entorno de prueba

El servidor puede usar SQLite o PostgreSQL con la misma API interna. SQLite sigue siendo el valor predeterminado. Para preparar Supabase:

1. Usa un proyecto Supabase **activo y dedicado a pruebas**. No uses proyectos de producción ni proyectos pausados que contengan datos existentes.
2. En el SQL Editor de ese proyecto, ejecuta las migraciones de `supabase/migrations/` en orden. Crean las tablas del prototipo, roles básicos, índices, RLS y un almacén ficticio `Primera prueba` con subalmacén `General`.
3. Crea y activa un entorno Python, instala la dependencia fijada y define la cadena PostgreSQL del panel **Connect** del proyecto. Mantén la cadena solo en una variable de entorno del servidor; nunca la guardes en Git ni en el navegador.

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:GESTION_DATABASE_URL = "<URI copiada de Supabase Connect>"
.\.venv\Scripts\python.exe app.py --create-admin
.\.venv\Scripts\python.exe app.py
```

La conexión PostgreSQL usa el driver `psycopg` desde el backend, con SSL según los parámetros de la cadena de conexión. Para conexiones persistentes, Supabase recomienda conexión directa o el pooler en modo sesión; el pooler de transacción no es adecuado para todas las sesiones largas. La aplicación no utiliza el Data API desde el navegador. La autorización por cuenta y almacén se valida en el servidor; RLS queda como barrera adicional para clientes API.

El primer inicio en Postgres crea únicamente la cuenta Administrador solicitada por consola; las migraciones crean los roles y el almacén de prueba inicial. Para volver a SQLite, elimina `GESTION_DATABASE_URL` y reinicia el servidor.

Si la conexión falla, revisa que el proyecto esté activo, que la contraseña de la cadena sea correcta y que se use una cadena con SSL. La contraseña no debe incluirse en capturas, logs o commits.

**Usar solo con datos ficticios durante esta etapa.** El servidor se enlaza a `127.0.0.1` y ya requiere inicio de sesión, pero aún no tiene HTTPS, auditoría de cambios ni respaldos automáticos. No exponerlo a Internet ni usar datos reales antes de revisar el despliegue, certificados, copias de seguridad y recuperación de cuentas.

## Arquitectura actual

- `app.py`: servidor HTTP de desarrollo, API JSON, validación de operaciones y persistencia en SQLite o PostgreSQL.
- `static/index.html`, `static/styles.css`, `static/app.js`: interfaz web en español, sin librerías externas.
- `tests/test_inventory.py`: pruebas del flujo de inventario, migración y aislamiento entre almacenes.
- `supabase/migrations/`: esquema inicial y datos ficticios para un proyecto Supabase de pruebas con RLS habilitado.
- Tablas: almacenes, empleados, roles, cuentas, sesiones, permisos por almacén, ubicaciones, artículos, movimientos y series.

El primer inicio de sesión requiere crear al administrador con `py app.py --create-admin`; el comando solicita usuario y contraseña y no imprime la contraseña. Luego inicia el servidor con `py app.py`.

La base inicia con el almacén **Primera prueba** y un subalmacén **General**. Desde la pantalla Almacenes se pueden crear más almacenes y subalmacenes; todos comparten las mismas opciones. El selector superior cambia el contexto de trabajo. El inventario, historial, técnicos y operaciones se muestran para el almacén activo. Cada trabajador se asigna a un almacén. Los técnicos solo reciben transferencias de su propio almacén. Las cuentas tienen un rol base (Administrador, Almacenero, Supervisor o Técnico), almacenes explícitamente asignados y permisos que el administrador puede permitir o denegar por cada almacén. Las rutas de escritura y consulta validan la sesión, la asignación y el permiso en el servidor; la interfaz también oculta las opciones no habilitadas. Las sesiones duran 12 horas y se guardan como hashes; las contraseñas se derivan con scrypt. Los intentos fallidos se limitan temporalmente. El catálogo de artículos es compartido, así que su edición requiere ese permiso en todos los almacenes. Al iniciar con una base existente, la migración asigna sus ubicaciones al primer almacén y conserva los registros.

Los saldos de materiales se calculan desde las líneas de movimientos. Para equipos seriados, la ubicación actual se mantiene por número de serie y cada cambio queda asociado a un movimiento. El ingreso y la transferencia se guardan dentro de una transacción SQLite para que una validación fallida no deje movimientos parciales.

## Orden de construcción

1. **Personal:** registrar trabajadores, cargo, estado activo y almacén virtual de cada técnico.
2. **Catálogo:** registrar materiales y equipos, unidad de medida y control por serie cuando corresponda.
3. **Almacén central:** registrar ingresos con documento de origen, subalmacén, fecha, artículo, cantidad y series.
4. **Almacén del técnico:** transferir material o equipos desde el almacén central al técnico y consultar sus saldos y series.
5. **Devoluciones y ajustes:** registrar retorno, avería o corrección autorizada sin perder el historial de movimientos.
6. **SOT:** registrar el servicio y descontar del almacén del técnico los materiales instalados; registrar también los retirados.
7. **Valorización v1:** calcular puntos por número de play y complejidad de zona.

La asistencia, RR. HH. más allá del registro básico, la migración de datos y las integraciones con sistemas de operadores quedan para etapas posteriores.

## Base de almacén y personal

Cada técnico activo tiene una ubicación de inventario propia. Un ingreso suma existencias al almacén receptor; una transferencia resta del origen y suma al destino en una sola operación. El saldo se obtiene de los movimientos confirmados, no se edita directamente.

Para un material fungible se registra cantidad y unidad de medida. Para un equipo seriado se registra cada número de serie de forma individual: una serie solo puede estar en una ubicación a la vez. El sistema debe impedir transferencias superiores al saldo disponible y movimientos repetidos de la misma serie.

Cada movimiento conserva tipo, origen, destino, fecha, responsable, artículo, cantidad, series, documento de referencia y observación. Las correcciones se hacen mediante un nuevo movimiento con motivo y usuario responsable, manteniendo el historial.

**Primer recorrido de prueba:** registrar un técnico, un material y un equipo seriado; ingresarlos al almacén central; transferir una parte y el equipo al técnico; comprobar el saldo y la ubicación de la serie en ambos almacenes. Después se vinculará ese consumo a una SOT.

## SOT

| Grupo | Campos iniciales |
| --- | --- |
| Identificación | Operador, número de SOT, código y nombre de cliente, distrito |
| Servicio | Tecnología HFC/FTTH, clase de trabajo, paquete, número de play |
| Ejecución | Técnico principal, fechas de instalación y validación, observaciones |
| Materiales | Artículos y equipos instalados o retirados, cantidad y serie cuando aplique |
| Control | Estado operativo y estado de validación separados |
| Valorización | Zona compleja, puntos calculados, versión de regla aplicada |

El código de cliente y el número de SOT deben interpretarse dentro de su operador. Las garantías y mantenimientos futuros se relacionarán con el historial del cliente y la SOT de instalación original.

## Regla de valorización v1

La unidad de valorización inicial son **puntos**, no soles:

```text
puntos_base = número_de_play
factor_zona = 2 si zona_compleja; en otro caso, 1
puntos_total = puntos_base × factor_zona
```

| Tipo | Zona normal | Zona compleja |
| --- | ---: | ---: |
| 1 play | 1 punto | 2 puntos |
| 2 play | 2 puntos | 4 puntos |
| 3 play | 3 puntos | 6 puntos |

El cálculo se hace por SOT. Deben guardarse el número de play, la condición de zona compleja, el resultado y la versión `v1` de la regla para conservar la trazabilidad aunque las reglas cambien. Un recálculo futuro requerirá una acción explícita y conservará el cálculo anterior en el historial.

**No intervienen todavía** tecnología, materiales no recurrentes, paquete, operador, garantía, mantenimiento ni ajustes manuales. El campo de precio monetario queda pendiente hasta definir tarifas y moneda; no se debe presentar el puntaje como importe facturable.

## Seguridad pendiente

El prototipo usa cookie HttpOnly y SameSite Strict y valida el origen de las peticiones de escritura. Antes de usarlo desde equipos de la empresa, falta servirlo solo por HTTPS, agregar cambio/restablecimiento de contraseña y auditoría de administración, configurar copias de seguridad y revisar seguridad con el entorno real.

## Decisiones pendientes

- Qué datos mínimos requiere el registro del trabajador y quién puede ver información de RR. HH.
- Si materiales y equipos pertenecen a la empresa o a un operador, y cómo se separan sus existencias.
- Tipos de documento de ingreso y transferencia usados hoy.
- Quién puede aprobar ajustes de inventario y devoluciones por avería.
- Fuente de las SOT: Excel, sistema externo o registro manual.
- Qué tipos de trabajo entran en la valorización v1 además de instalaciones.
- Quién define y verifica la condición de zona compleja.
- Momento en que los puntos pasan de estimados a aprobados.
- Reglas de precio monetario, adicionales, descuentos y cambios de tarifa.
- Estados operativos y de validación exactos, así como sus transiciones.

## Entornos y datos

El desarrollo y las pruebas usarán datos ficticios y un entorno separado de producción. La sesión del sistema actual se utiliza solo para análisis funcional; este documento no contiene datos personales ni credenciales.

La conexión PostgreSQL y la creación de la cuenta Administrador ya se verificaron en Supabase. Falta completar el recorrido de inventario desde la interfaz web contra PostgreSQL (artículo, ingreso y transferencia). Las pruebas automatizadas cubren SQLite y las traducciones básicas del adaptador PostgreSQL.

