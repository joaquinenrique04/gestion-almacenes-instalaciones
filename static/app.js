const state = { snapshot: null, meta: null, accountData: null, noticeTimer: null, warehouseId: Number(localStorage.getItem("warehouseId")) || null };
const viewTitles = {
  inicio: "Resumen",
  inventario: "Inventario",
  movimientos: "Movimientos",
  personal: "Personal",
  articulos: "Artículos",
  ingreso: "Ingreso al almacén",
  transferencia: "Transferencia a técnico",
  almacenes: "Almacenes",
  cuentas: "Cuentas y permisos",
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : new Intl.DateTimeFormat("es-PE", {
    day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit",
  }).format(date);
}

function formatNumber(value) {
  return new Intl.NumberFormat("es-PE", { maximumFractionDigits: 3 }).format(Number(value || 0));
}

function showNotice(message, kind = "success") {
  const notice = $("#notice");
  notice.textContent = message;
  notice.className = `notice ${kind}`;
  notice.hidden = false;
  window.clearTimeout(state.noticeTimer);
  state.noticeTimer = window.setTimeout(() => { notice.hidden = true; }, 5500);
  notice.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function showView(name) {
  if (!viewTitles[name] || !canOpenView(name)) return;
  $$(".view").forEach((view) => view.classList.toggle("active", view.id === `view-${name}`));
  $$(".nav-item").forEach((button) => button.classList.toggle("active", button.dataset.view === name));
  $("#page-crumb").textContent = viewTitles[name];
  if (name === "ingreso") renderTransactionLines("receipt");
  if (name === "transferencia") {
    renderTechnicianOptions();
    renderTransactionLines("transfer");
  }
  if (name === "cuentas") loadAccountManager();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && path !== "/api/auth/login") showLogin();
    throw new Error(body.error || "No se pudo completar la operación.");
  }
  return body;
}

async function loadSnapshot() {
  try {
    state.snapshot = await request("/api/snapshot");
    state.meta = state.snapshot.access;
    $("#auth-screen").hidden = true;
    $("#app-shell").hidden = false;
    renderAll();
  } catch (error) {
    showNotice(error.message, "error");
  }
}

function renderAll() {
  if (!state.snapshot) return;
  const ids = state.snapshot.warehouses.map((warehouse) => warehouse.id);
  if (!ids.includes(state.warehouseId)) state.warehouseId = ids[0] || null;
  localStorage.setItem("warehouseId", state.warehouseId || "");
  renderWarehouseContext();
  renderWarehouses();
  applyAccess();
  $("#user-label").textContent = `${state.meta?.user?.name || ""} · ${state.meta?.user?.role || ""}`;
  renderLocationOptions();
  $("#employee-warehouse-id").value = state.warehouseId || "";
  $("#subwarehouse-warehouse-id").value = state.warehouseId || "";
  $("#employee-warehouse-id").disabled = !state.warehouseId;
  $("#subwarehouse-warehouse-id").disabled = !state.warehouseId;
  $("#item-warehouse-id").value = state.warehouseId || "";
  renderMetrics();
  renderEmployees();
  renderItems();
  renderInventory("#inventory-preview-table", 6);
  renderInventory("#inventory-table");
  renderMovements("#recent-movements", 5, false);
  renderMovements("#movement-table", 30, true);
  renderTechnicianOptions();
}

function currentPermissions() {
  if (!state.meta) return new Set();
  return new Set(state.meta.warehouses.find((warehouse) => warehouse.id === state.warehouseId)?.permissions || []);
}

function canOpenView(name) {
  const view = $(`#view-${name}`);
  if (!view) return false;
  if (view.hasAttribute("data-admin-only")) return Boolean(state.meta?.user?.is_admin);
  const permission = view.dataset.viewPermission;
  return !permission || currentPermissions().has(permission);
}

function applyAccess() {
  const permissions = currentPermissions();
  const admin = Boolean(state.meta?.user?.is_admin);
  $$('[data-admin-only]').forEach((element) => { element.hidden = !admin; });
  const canManageSharedCatalog = admin || (state.meta.warehouses.length > 0 &&
    state.meta.warehouses.length === state.snapshot.warehouses.length &&
    state.meta.warehouses.every((warehouse) => warehouse.permissions.includes("manage_items")));
  $$('[data-permission]').forEach((element) => {
    const allowed = element.dataset.permission === "manage_items" ? canManageSharedCatalog : permissions.has(element.dataset.permission);
    element.hidden = !admin && !allowed;
  });
  $$('.nav-list').forEach((nav) => {
    nav.hidden = !nav.querySelector(':scope > .nav-item:not([hidden])');
  });
  const catalogNav = document.querySelector('[aria-label="Catálogos"]');
  const catalogLabel = document.querySelector(".secondary-label");
  catalogLabel.hidden = catalogNav.hidden;
  $("#warehouse-selector").disabled = admin ? false : state.meta.warehouses.length < 2;
  if (!canOpenView("inicio")) showView("inicio");
  const selected = document.querySelector(".view.active");
  if (selected && !canOpenView(selected.id.replace("view-", ""))) showView("inicio");
}

function showLogin(message = "") {
  state.snapshot = null;
  state.meta = null;
  $("#app-shell").hidden = true;
  $("#auth-screen").hidden = false;
  const notice = $("#auth-notice");
  notice.textContent = message;
  notice.hidden = !message;
}

async function startSession() {
  try {
    state.meta = await request("/api/auth/me");
    await loadSnapshot();
  } catch (error) {
    showLogin();
  }
}

function renderLocationOptions() {
  const locations = (state.snapshot.locations || []).filter((location) => location.warehouse_id === state.warehouseId && location.kind === "central");
  for (const selector of ["#receipt-location", "#transfer-location"]) {
    const select = $(selector);
    if (!select) continue;
    const prior = select.value;
    select.innerHTML = locations.length ? locations.map((location) => `<option value="${location.id}">${escapeHtml(location.name)}</option>`).join("") : '<option value="">Crea un almacén primero</option>';
    select.disabled = !locations.length;
    if (locations.some((location) => String(location.id) === prior)) select.value = prior;
  }
}

function activeInventory() {
  return (state.snapshot?.inventory || []).filter((row) => row.warehouse_id === state.warehouseId);
}

function activeMovements() {
  return (state.snapshot?.movements || []).filter((movement) => movement.source_warehouse_id === state.warehouseId || movement.destination_warehouse_id === state.warehouseId);
}

function renderWarehouseContext() {
  const select = $("#warehouse-selector");
  select.innerHTML = (state.snapshot.warehouses || []).map((warehouse) => `<option value="${warehouse.id}">${escapeHtml(warehouse.name)}</option>`).join("");
  select.value = state.warehouseId || "";
}

function renderWarehouses() {
  const warehouses = state.snapshot.warehouses || [];
  $("#warehouse-count").textContent = warehouses.length;
  if (!warehouses.length) {
    $("#warehouse-list").innerHTML = '<div class="empty-list">No hay almacenes activos.</div>';
    return;
  }
  $("#warehouse-list").innerHTML = warehouses.map((warehouse) => {
    const locations = state.snapshot.locations.filter((location) => location.warehouse_id === warehouse.id && location.kind === "central");
    return `<div class="warehouse-card ${warehouse.id === state.warehouseId ? "selected" : ""}"><div class="warehouse-card-heading"><span class="metric-icon mint">▣</span><span><strong>${escapeHtml(warehouse.name)}</strong><small>${locations.length} subalmacén${locations.length === 1 ? "" : "es"}</small></span><button class="text-button" data-select-warehouse="${warehouse.id}">Abrir</button></div><div class="warehouse-locations">${locations.map((location) => `<span class="location-tag">${escapeHtml(location.name)}</span>`).join("")}</div><small class="warehouse-note">Módulos habilitados: resumen, inventario, movimientos, personal, artículos, ingresos y transferencias.</small></div>`;
  }).join("");
}

function renderMetrics() {
  const employees = state.snapshot.employees.filter((employee) => employee.warehouse_id === state.warehouseId || employee.role !== "Técnico");
  const inventory = activeInventory();
  const movements = activeMovements();
  const items = state.snapshot.items;
  $("#metric-employees").textContent = employees.filter((employee) => employee.active).length;
  $("#metric-items").textContent = items.length;
  $("#metric-units").textContent = inventory.length;
  $("#metric-movements").textContent = movements.length;
}

function renderEmployees() {
  const employees = state.snapshot.employees.filter((employee) => employee.warehouse_id === state.warehouseId || employee.role !== "Técnico");
  $("#employee-count").textContent = employees.length;
  if (!employees.length) {
    $("#employee-list").innerHTML = '<div class="empty-list">Aún no hay trabajadores. Registra el primero en este formulario.</div>';
    return;
  }
  $("#employee-list").innerHTML = employees.map((employee) => {
    const name = `${employee.first_name} ${employee.last_name}`;
    const initials = `${employee.first_name[0] || ""}${employee.last_name[0] || ""}`.toUpperCase();
    return `<div class="person-row"><span class="avatar">${escapeHtml(initials)}</span><span class="person-main"><strong>${escapeHtml(name)}</strong><small>${escapeHtml(employee.code)}${employee.location_name ? ` · ${escapeHtml(employee.location_name)}` : ""}${employee.warehouse_name ? ` · ${escapeHtml(employee.warehouse_name)}` : ""}</small></span><span class="role-chip">${escapeHtml(employee.role)}</span></div>`;
  }).join("");
}

function renderItems() {
  const items = state.snapshot.items;
  $("#item-count").textContent = items.length;
  if (!items.length) {
    $("#item-list").innerHTML = '<div class="empty-list">El catálogo está vacío. Registra un material o equipo.</div>';
    return;
  }
  $("#item-list").innerHTML = items.map((item) => `<div class="catalog-row"><span class="metric-icon ${item.item_type === "Equipo" ? "gold" : "mint"}">${item.item_type === "Equipo" ? "◈" : "▤"}</span><span class="catalog-main"><strong>${escapeHtml(item.name)}</strong><small><span class="catalog-code">${escapeHtml(item.code)}</span> · ${escapeHtml(item.item_type)} · ${escapeHtml(item.unit)}${item.serial_control ? " · Control por serie" : ""}</small></span></div>`).join("");
}

function inventoryTable(rows) {
  if (!rows.length) return '<div class="empty-state">Todavía no hay existencias registradas.</div>';
  return `<div class="table-wrap"><table class="data-table"><thead><tr><th>Artículo</th><th>Tipo</th><th>Ubicación</th><th>Saldo</th><th>Series</th></tr></thead><tbody>${rows.map((row) => `<tr><td class="item-cell"><strong>${escapeHtml(row.item_name)}</strong><small>${escapeHtml(row.item_code)}</small></td><td><span class="type-tag ${row.item_type === "Equipo" ? "equipment" : ""}">${escapeHtml(row.item_type)}</span></td><td><span class="location-tag ${row.location_kind === "technician" ? "technician" : ""}">${escapeHtml(row.location_name)}</span></td><td class="stock-value">${formatNumber(row.quantity)} ${escapeHtml(row.unit)}</td><td class="serial-list">${row.serials?.length ? escapeHtml(row.serials.join(", ")) : "—"}</td></tr>`).join("")}</tbody></table></div>`;
}

function renderInventory(selector, limit = null) {
  const rows = activeInventory();
  $(selector).innerHTML = inventoryTable(limit ? rows.slice(0, limit) : rows);
}

function renderMovements(selector, limit, full) {
  const movements = activeMovements().slice(0, limit);
  if (!movements.length) {
    $(selector).innerHTML = '<div class="empty-state">No hay movimientos todavía. Registra un ingreso para comenzar.</div>';
    return;
  }
  if (!full) {
    $(selector).innerHTML = movements.map((movement) => `<div class="recent-row"><span class="movement-icon ${movement.kind === "Ingreso" ? "mint" : "blue"}">${movement.kind === "Ingreso" ? "↓" : "⇄"}</span><span class="recent-main"><strong>${escapeHtml(movement.kind)} · ${escapeHtml(movement.document)}</strong><small>${escapeHtml(movement.item_names || "")} · ${escapeHtml(movement.destination_name || "")}</small></span><span class="recent-date">${formatDate(movement.created_at)}</span></div>`).join("");
    return;
  }
  $(selector).innerHTML = `<div class="table-wrap"><table class="data-table"><thead><tr><th>Fecha</th><th>Tipo</th><th>Documento</th><th>Artículo(s)</th><th>Origen</th><th>Destino</th><th>Responsable</th></tr></thead><tbody>${movements.map((movement) => `<tr><td>${formatDate(movement.created_at)}</td><td><span class="status-tag">${escapeHtml(movement.kind)}</span></td><td>${escapeHtml(movement.document)}</td><td>${escapeHtml(movement.item_names || "")}</td><td>${escapeHtml(movement.source_name || "—")}</td><td>${escapeHtml(movement.destination_name || "—")}</td><td>${escapeHtml(movement.responsible)}</td></tr>`).join("")}</tbody></table></div>`;
}

function renderTechnicianOptions() {
  const select = $("#transfer-employee");
  if (!select || !state.snapshot) return;
  const prior = select.value;
  const technicians = state.snapshot.employees.filter((employee) => employee.role === "Técnico" && employee.active && employee.location_id && employee.warehouse_id === state.warehouseId);
  select.innerHTML = '<option value="">Selecciona técnico</option>' + technicians.map((employee) => `<option value="${employee.id}">${escapeHtml(employee.first_name)} ${escapeHtml(employee.last_name)}</option>`).join("");
  if (technicians.some((employee) => String(employee.id) === prior)) select.value = prior;
}

function itemOptions(kind) {
  const items = state.snapshot?.items || [];
  return items.filter((item) => kind !== "transfer" || Number(activeInventory().find((line) => line.item_id === item.id && line.location_kind === "central")?.quantity || 0) > 0);
}

function renderTransactionLines(kind) {
  const container = kind === "receipt" ? $("#receipt-lines") : $("#transfer-lines");
  if (!container || !state.snapshot) return;
  if (!container.children.length) addTransactionLine(kind);
}

function addTransactionLine(kind) {
  const container = kind === "receipt" ? $("#receipt-lines") : $("#transfer-lines");
  const items = itemOptions(kind);
  const options = items.map((item) => `<option value="${item.id}">${escapeHtml(item.code)} · ${escapeHtml(item.name)}</option>`).join("");
  const row = document.createElement("div");
  row.className = "transaction-line";
  row.dataset.kind = kind;
  row.innerHTML = `<label>Artículo<select data-field="item" required><option value="">Selecciona artículo</option>${options}</select><small class="line-available"></small></label><label>Cantidad<input data-field="quantity" type="number" min="0.001" step="0.001" required placeholder="0"></label><label class="serial-field" hidden>Números de serie<textarea data-field="serials" rows="2" placeholder="Una serie por línea"></textarea></label><button type="button" class="remove-line" title="Quitar artículo" aria-label="Quitar artículo">×</button>`;
  container.append(row);
  const itemSelect = $('[data-field="item"]', row);
  const quantityInput = $('[data-field="quantity"]', row);
  const serialArea = $('[data-field="serials"]', row);
  const serialLabel = $(".serial-field", row);
  const availableLabel = $(".line-available", row);
  itemSelect.addEventListener("change", () => {
    const item = state.snapshot.items.find((candidate) => candidate.id === Number(itemSelect.value));
    serialLabel.hidden = !item?.serial_control;
    quantityInput.readOnly = Boolean(item?.serial_control);
    quantityInput.value = "";
    serialArea.value = "";
    const stock = activeInventory().find((line) => line.item_id === item?.id && line.location_kind === "central");
    if (kind === "transfer" && stock) {
      const serialHint = stock.serials?.length ? ` · Series: ${stock.serials.join(", ")}` : "";
      availableLabel.textContent = `Disponible en central: ${formatNumber(stock.quantity)} ${stock.unit}${serialHint}`;
    } else {
      availableLabel.textContent = item?.serial_control ? "Una serie por línea; la cantidad se calculará automáticamente." : "";
    }
  });
  serialArea.addEventListener("input", () => {
    const serials = parseSerialText(serialArea.value);
    quantityInput.value = serials.length ? String(serials.length) : "";
  });
  $(".remove-line", row).addEventListener("click", () => {
    row.remove();
    if (!container.children.length) addTransactionLine(kind);
  });
}

function parseSerialText(value) {
  return value.split(/[\n,\r]+/).map((serial) => serial.trim()).filter(Boolean);
}

function transactionLines(form) {
  return $$(".transaction-line", form).map((row) => ({
    item_id: Number($('[data-field="item"]', row).value),
    quantity: Number($('[data-field="quantity"]', row).value),
    serials: parseSerialText($('[data-field="serials"]', row).value),
  }));
}

async function submitForm(form, path, payload, successMessage) {
  const button = $("button[type=submit]", form);
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Guardando…";
  try {
    const result = await request(path, { method: "POST", body: JSON.stringify(payload) });
    form.reset();
    $("#receipt-lines").replaceChildren();
    $("#transfer-lines").replaceChildren();
    await loadSnapshot();
    showNotice(successMessage, "success");
    if (form.id === "receipt-form") renderTransactionLines("receipt");
    if (form.id === "transfer-form") renderTransactionLines("transfer");
    return result;
  } catch (error) {
    showNotice(error.message, "error");
    return null;
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

function formObject(form) {
  return Object.fromEntries(new FormData(form).entries());
}

async function loadAccountManager() {
  if (!state.meta?.user?.is_admin) return;
  try {
    state.accountData = await request("/api/admin/accounts");
    const employeeSelect = $("#account-employee");
    const linked = new Set(state.accountData.accounts.map((account) => account.employee_id).filter(Boolean));
    employeeSelect.innerHTML = '<option value="">Selecciona trabajador</option>' + state.accountData.employees.filter((employee) => !linked.has(employee.id)).map((employee) => `<option value="${employee.id}">${escapeHtml(employee.first_name)} ${escapeHtml(employee.last_name)} · ${escapeHtml(employee.code)}</option>`).join("");
    $("#account-role").innerHTML = state.accountData.roles.map((role) => `<option>${escapeHtml(role)}</option>`).join("");
    if (state.accountData.roles.includes("Almacenero")) $("#account-role").value = "Almacenero";
    renderGrantEditor();
    renderAccountList();
  } catch (error) {
    showNotice(error.message, "error");
  }
}

function renderGrantEditor(selected = null) {
  if (!state.accountData) return;
  const role = $("#account-role").value || "Almacenero";
  const defaults = new Set(state.accountData.role_defaults[role] || []);
  if (!selected && !state.snapshot?.access?.user?.is_admin) return;
  if (!selected) {
    const employeeId = Number($("#account-employee").value);
    const employee = state.accountData.employees.find((candidate) => candidate.id === employeeId);
    const assignedWarehouse = employee?.warehouse_id || state.accountData.warehouses[0]?.id;
    selected = assignedWarehouse ? {[assignedWarehouse]: defaults} : {};
  }
  const admin = role === "Administrador";
  $("#account-employee").required = !admin && !$("#account-id").value;
  $("#account-employee").disabled = admin || Boolean($("#account-id").value);
  $("#account-grants").hidden = admin;
  if (admin) {
    $("#account-grants").innerHTML = '<div class="info-note"><span>ⓘ</span><p>El administrador accede a todos los almacenes y funciones.</p></div>';
    return;
  }
  $("#account-grants").innerHTML = state.accountData.warehouses.map((warehouse) => {
    const checked = selected ? Object.hasOwn(selected, warehouse.id) : true;
    const permissions = selected?.[warehouse.id] || (checked ? defaults : new Set());
    return `<fieldset class="grant-warehouse"><legend><label class="grant-warehouse-title"><input type="checkbox" data-grant-warehouse="${warehouse.id}" ${checked ? "checked" : ""}><strong>${escapeHtml(warehouse.name)}</strong></label></legend><div class="permission-options">${Object.entries(state.accountData.permission_labels).map(([key, label]) => `<label><input type="checkbox" data-grant-permission="${key}" data-warehouse-id="${warehouse.id}" ${permissions.has(key) ? "checked" : ""}><span>${escapeHtml(label)}</span></label>`).join("")}</div></fieldset>`;
  }).join("");
  $$('[data-grant-warehouse]').forEach((checkbox) => {
    const warehouseId = checkbox.dataset.grantWarehouse;
    $$(`[data-warehouse-id="${warehouseId}"]`).forEach((permission) => { permission.disabled = !checkbox.checked; });
    checkbox.addEventListener("change", () => {
      $$(`[data-warehouse-id="${warehouseId}"]`).forEach((permission) => { permission.disabled = !checkbox.checked; });
    });
  });
}

function renderAccountList() {
  const accounts = state.accountData?.accounts || [];
  $("#account-count").textContent = accounts.length;
  if (!accounts.length) {
    $("#account-list").innerHTML = '<div class="empty-list">Todavía no hay cuentas registradas.</div>';
    return;
  }
  $("#account-list").innerHTML = accounts.map((account) => `<div class="account-row"><div><strong>${escapeHtml(account.username)}</strong><small>${escapeHtml(account.role)}${account.first_name ? ` · ${escapeHtml(account.first_name)} ${escapeHtml(account.last_name)}` : ""}</small><small>${account.role === "Administrador" ? "Todos los almacenes y funciones" : account.warehouses.map((warehouse) => `${escapeHtml(warehouse.name)} (${warehouse.permissions.length} permisos)`).join(" · ")}</small></div>${account.role === "Administrador" ? "" : `<button class="button secondary" data-edit-account="${account.id}">Editar accesos</button>`}</div>`).join("");
}

function editAccountAccess(accountId) {
  const account = state.accountData.accounts.find((candidate) => candidate.id === accountId);
  if (!account) return;
  $("#account-id").value = account.id;
  $("#account-form-title").textContent = `Permisos de ${account.username}`;
  $("#account-form button[type=submit]").textContent = "Guardar permisos";
  $("#account-employee-wrap").hidden = true;
  $("#account-username-wrap").hidden = true;
  $("#account-password-wrap").hidden = true;
  $("#account-role").value = account.role;
  $("#account-role").disabled = true;
  const selected = Object.fromEntries(account.warehouses.map((warehouse) => [warehouse.id, new Set(warehouse.permissions)]));
  renderGrantEditor(selected);
}

function resetAccountForm() {
  const form = $("#account-form");
  form.reset();
  $("#account-id").value = "";
  $("#account-form-title").textContent = "Crear cuenta";
  $("#account-form button[type=submit]").textContent = "Crear cuenta";
  $("#account-employee-wrap").hidden = false;
  $("#account-username-wrap").hidden = false;
  $("#account-password-wrap").hidden = false;
  $("#account-role").disabled = false;
  if (state.accountData?.roles.includes("Almacenero")) $("#account-role").value = "Almacenero";
  renderGrantEditor();
}

function selectedAccountAccess() {
  return $$("[data-grant-warehouse]:checked").map((warehouseCheckbox) => {
    const warehouseId = Number(warehouseCheckbox.dataset.grantWarehouse);
    const permissions = $$(`[data-warehouse-id="${warehouseId}"]:checked`).map((checkbox) => checkbox.dataset.grantPermission);
    return { warehouse_id: warehouseId, permissions };
  });
}

document.addEventListener("click", (event) => {
  const editButton = event.target.closest("[data-edit-account]");
  if (editButton) editAccountAccess(Number(editButton.dataset.editAccount));
  const button = event.target.closest("[data-view]");
  if (button) showView(button.dataset.view);
  const warehouseButton = event.target.closest("[data-select-warehouse]");
  if (warehouseButton) {
    state.warehouseId = Number(warehouseButton.dataset.selectWarehouse);
    localStorage.setItem("warehouseId", state.warehouseId);
    renderAll();
    showView("inicio");
  }
});

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    state.meta = await request("/api/auth/login", {method: "POST", body: JSON.stringify(formObject(form))});
    form.reset();
    await loadSnapshot();
  } catch (error) {
    showLogin(error.message);
  }
});

$("#logout-button").addEventListener("click", async () => {
  try { await request("/api/auth/logout", {method: "POST", body: "{}"}); } catch {}
  showLogin("Sesión cerrada.");
});

$("#account-role").addEventListener("change", () => renderGrantEditor());
$("#account-employee").addEventListener("change", () => renderGrantEditor());
$("#account-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = formObject(form);
  const access = selectedAccountAccess();
  try {
    if (data.account_id) {
      await request(`/api/admin/accounts/${data.account_id}/access`, {method: "PUT", body: JSON.stringify({access})});
      showNotice("Permisos actualizados.", "success");
    } else {
      data.access = access;
      if (data.role !== "Administrador" && !access.some((grant) => grant.warehouse_id === Number(state.accountData.employees.find((employee) => employee.id === Number(data.employee_id))?.warehouse_id))) {
        throw new Error("Incluye el almacén asignado a ese trabajador.");
      }
      await request("/api/admin/accounts", {method: "POST", body: JSON.stringify(data)});
      showNotice("Cuenta creada con los accesos asignados.", "success");
    }
    resetAccountForm();
    await loadAccountManager();
  } catch (error) { showNotice(error.message, "error"); }
});

$("#refresh-button").addEventListener("click", loadSnapshot);
$("#warehouse-selector").addEventListener("change", (event) => {
  state.warehouseId = Number(event.currentTarget.value) || null;
  localStorage.setItem("warehouseId", state.warehouseId || "");
  renderAll();
});
$("#today-label").textContent = new Intl.DateTimeFormat("es-PE", { weekday: "long", day: "numeric", month: "long" }).format(new Date());

$("#employee-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  await submitForm(event.currentTarget, "/api/employees", formObject(event.currentTarget), "Trabajador registrado.");
});

$("#warehouse-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const created = await submitForm(form, "/api/warehouses", formObject(form), "Almacén creado con el subalmacén General.");
  if (created?.result?.id) {
    state.warehouseId = created.result.id;
    localStorage.setItem("warehouseId", created.result.id);
    renderAll();
    if (state.meta?.user?.is_admin) await loadAccountManager();
  }
});

$("#subwarehouse-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  await submitForm(event.currentTarget, "/api/subwarehouses", formObject(event.currentTarget), "Subalmacén agregado.");
});

$("#item-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = formObject(event.currentTarget);
  data.serial_control = $("[name=serial_control]", event.currentTarget).checked;
  await submitForm(event.currentTarget, "/api/items", data, "Artículo registrado en el catálogo.");
});

$("#add-receipt-line").addEventListener("click", () => addTransactionLine("receipt"));
$("#add-transfer-line").addEventListener("click", () => addTransactionLine("transfer"));

$("#receipt-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = formObject(form);
  const document = `${data.document_type}: ${data.document_number}`;
  await submitForm(form, "/api/receipts", {
    document, warehouse_id: state.warehouseId, location_id: data.location_id, responsible: data.responsible, notes: data.notes,
    lines: transactionLines(form),
  }, "Ingreso registrado en el almacén central.");
});

$("#transfer-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = formObject(form);
  await submitForm(form, "/api/transfers", {
    employee_id: data.employee_id, warehouse_id: state.warehouseId, source_location_id: data.source_location_id, document: data.document,
    responsible: data.responsible, notes: data.notes,
    lines: transactionLines(form),
  }, "Transferencia registrada en el almacén del técnico.");
});

startSession();

