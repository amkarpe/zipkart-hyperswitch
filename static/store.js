// Store console. Routes: #/ (queue) and #/order/{id}. Every action goes to the
// server, which re-checks state and headroom under the order lock; this page only
// displays what the server returns.

const POLL_MS = 3000;
let pollTimer = null;
let countdownTimer = null;
let current = null;            // last store view of the open order
let subPicker = null;          // { itemId, data } while choosing a substitute

async function call(method, path, body) {
  const resp = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await resp.json().catch(() => ({}));
  if (resp.status === 401 && !path.endsWith("/login")) { showLogin(); throw new Error("Please log in."); }
  if (!resp.ok) throw new Error(data.error || data.detail || `Request failed (${resp.status})`);
  return data;
}

function show(id) {
  for (const s of ["login", "queue", "order"]) $(s).classList.toggle("hidden", s !== id);
  $("logout-btn").classList.toggle("hidden", id === "login");
}

function showLogin() {
  clearTimeout(pollTimer);
  show("login");
  $("password").focus();
}

$("login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("login-error").classList.add("hidden");
  try {
    await call("POST", "/store/api/login", { password: $("password").value });
    $("password").value = "";
    route();
  } catch (e) {
    $("login-error").textContent = e.message;
    $("login-error").classList.remove("hidden");
  }
});

$("logout-btn").addEventListener("click", async () => {
  await call("POST", "/store/api/logout").catch(() => {});
  showLogin();
});

const STATUS_PILL = {
  authorized: ["Ready to pick", ""], picking: ["Picking", "warn"], finalizing: ["Finalizing", "warn"],
  capturing: ["Capturing", "warn"], captured: ["Captured", ""], cancelling: ["Cancelling", "warn"],
  cancelled: ["Cancelled", "bad"], needs_review: ["Needs review", "bad"],
};
const pill = (status) => {
  const [label, cls] = STATUS_PILL[status] || [status, ""];
  return el("span", { class: `pill ${cls}` }, label);
};

// ── Queue ────────────────────────────────────────────────────────────────
async function loadQueue() {
  const orders = await call("GET", "/store/api/orders");
  const box = $("queue");
  box.replaceChildren();
  const panel = el("div", { class: "panel" }, el("h2", {}, "Orders"));
  if (!orders.length) panel.append(el("p", { class: "muted" }, "No orders with a confirmed hold yet."));
  const table = el("table", { class: "lines" },
    el("tr", {}, el("th", {}, "Order"), el("th", {}, "Customer"), el("th", {}, "Lines"), el("th", {}, "Hold"),
       el("th", {}, "Status"), el("th", {}, "")));
  for (const o of orders) {
    table.append(el("tr", {},
      el("td", {}, el("code", {}, o.id)),
      el("td", {}, o.customer_name),
      el("td", {}, String(o.lines)),
      el("td", {}, money(o.hold_amount_cents)),
      el("td", {}, pill(o.status)),
      el("td", {}, el("button", { onclick: () => openOrder(o) }, o.status === "authorized" ? "Start picking" : "Open"))));
  }
  if (orders.length) panel.append(table);
  box.append(panel, await sweepPanel());
  show("queue");
}

async function sweepPanel() {
  const s = await call("GET", "/store/api/sweep").catch(() => null);
  const when = s?.last_run_at ? `${Math.max(0, Math.round((Date.now() - Date.parse(s.last_run_at)) / 1000))}s ago` : "never";
  const changes = s?.last_result ? Object.entries(s.last_result)
    .filter(([k, v]) => Array.isArray(v)).reduce((n, [, v]) => n + v.length, 0) : 0;
  const info = el("span", { class: "muted" },
    `Payment reconciliation: last run ${when}${s?.last_result ? `, ${changes} fix${changes === 1 ? "" : "es"}` : ""}${s?.running ? " (running now)" : ""}`);
  const btn = el("button", { onclick: async () => {
    btn.disabled = true; btn.textContent = "Running…";
    try { await call("POST", "/store/api/sweep"); } catch (e) { alert(e.message); }
    route();
  } }, "Run now");
  return el("div", { class: "panel row", style: "align-items:center" }, info, btn);
}

async function openOrder(o) {
  try {
    if (o.status === "authorized") await call("POST", `/store/api/orders/${encodeURIComponent(o.id)}/start`);
    location.hash = `#/order/${encodeURIComponent(o.id)}`;
  } catch (e) { alert(e.message); }
}

// ── One order ────────────────────────────────────────────────────────────
async function loadOrder(id) {
  // Don't clobber a weight the shopper is typing or the substitute picker.
  if (current && current.order_id === id && (document.activeElement?.closest?.("#order input, #order select") || subPicker)) {
    schedule(id);
    return;
  }
  render(await call("GET", `/store/api/orders/${encodeURIComponent(id)}`));
  schedule(id);
}

function schedule(id) {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(() => loadOrder(id).catch(() => {}), POLL_MS);
}

async function act(promise, okMessage) {
  const msg = $("order-msg");
  msg.className = "muted";
  msg.textContent = "Working…";
  try {
    const result = await promise;
    const view = result.order || result;
    subPicker = null;
    render(view, okMessage || (result.outcome ? `Result: ${result.outcome.replace("_", " ")}` : ""));
  } catch (e) {
    msg.className = "error";
    msg.textContent = e.message;
  }
}

const api = (path, body) => call("POST", `/store/api/orders/${encodeURIComponent(current.order_id)}${path}`, body);

function lineDescription(i) {
  if (i.pricing_type === "weighed") {
    return el("span", {}, `${i.name} · ordered ${i.estimated_weight_lb} lb @ ${money(i.unit_price_cents)}/lb`,
      el("br"), el("span", { class: "muted" },
        `Allowed: 0.01–${i.max_weight_lb} lb (up to +${current.weight_tolerance_percent}%)`));
  }
  return `${i.name} · ${i.quantity} × ${money(i.unit_price_cents)}`;
}

function pickControls(i) {
  if (current.status !== "picking") return null;
  const box = el("div", { class: "inline" });
  if (i.pricing_type === "fixed") {
    const qty = el("input", { type: "number", min: "1", max: String(i.quantity), value: String(i.picked_quantity ?? i.quantity),
      id: `q-${i.id}`, "aria-label": "Picked quantity" });
    box.append(qty, el("button", { onclick: () => act(api(`/items/${i.id}/pick`, { picked_quantity: Number(qty.value) })) },
      i.status === "picked" ? "Update" : "Picked"));
  } else {
    const w = el("input", { type: "text", inputmode: "decimal", placeholder: "lb", value: i.actual_weight_lb ?? "",
      id: `w-${i.id}`, size: "6", "aria-label": `Actual weight in pounds, 0.01 to ${i.max_weight_lb}` });
    box.append(w, el("span", { class: "muted" }, `lb (0.01–${i.max_weight_lb})`),
      el("button", { onclick: () => act(api(`/items/${i.id}/pick`, { actual_weight_lb: w.value.trim() })) },
        i.status === "picked" ? "Update" : "Weighed"));
  }
  if (i.status === "pending") {
    box.append(
      el("button", { onclick: () => openSubPicker(i) }, "Out of stock → substitute"),
      el("button", { class: "danger", onclick: () => confirm(`Remove ${i.name} from the order?`) && act(api(`/items/${i.id}/out-of-stock`)) }, "Out of stock → remove"));
  } else {
    box.append(el("button", { class: "danger", onclick: () => confirm(`Remove ${i.name} from the order?`) && act(api(`/items/${i.id}/out-of-stock`)) }, "Remove"));
  }
  return box;
}

async function openSubPicker(item) {
  try {
    subPicker = { itemId: item.id, data: await call("GET", `/store/api/orders/${encodeURIComponent(current.order_id)}/items/${item.id}/substitutes`) };
    render(current);
  } catch (e) { alert(e.message); }
}

function subPickerPanel() {
  const d = subPicker.data;
  const panel = el("div", { class: "prompt" },
    el("div", { class: "row" }, el("strong", {}, `Substitute for ${d.original_name}`),
      el("span", {}, `Room left in hold: ${money(d.headroom_cents)}`)),
    el("p", { class: "muted", style: "margin:4px 0 8px" },
      "Only items that fit the remaining hold can be offered. The customer has " +
      `${current.substitution_timeout_seconds}s to answer; then their preference applies.`));
  const table = el("table", { class: "lines" });
  for (const c of d.candidates) {
    const amount = c.pricing_type === "fixed"
      ? el("input", { type: "number", min: "1", max: "24", value: String(c.default_quantity), "aria-label": "Quantity" })
      : el("input", { type: "text", inputmode: "decimal", value: c.default_weight_lb, size: "6", "aria-label": "Weight (lb)" });
    const offer = el("button", {
      onclick: () => act(api(`/items/${d.item_id}/substitute`, c.pricing_type === "fixed"
        ? { sku: c.sku, quantity: Number(amount.value) } : { sku: c.sku, weight_lb: amount.value.trim() }),
        "Substitute offered to the customer."),
    }, "Offer");
    if (!c.fits) offer.disabled = true;
    table.append(el("tr", {},
      el("td", {}, `${c.emoji} ${c.name}`),
      el("td", {}, `${money(c.unit_price_cents)} / ${c.pricing_type === "weighed" ? "lb" : c.unit_label}`),
      el("td", {}, el("div", { class: "inline" }, amount, c.pricing_type === "weighed" ? el("span", { class: "muted" }, "lb") : null)),
      el("td", {}, money(c.line_cents)),
      el("td", {}, c.fits ? offer : el("span", { class: "pill bad" }, "Over hold"))));
  }
  panel.append(table, el("div", { class: "actions" },
    el("button", { onclick: () => { subPicker = null; render(current); } }, "Close")));
  return panel;
}

function kpi(label, value) {
  return el("div", { class: "kpi" }, el("div", { class: "v" }, value), el("div", { class: "l" }, label));
}

function render(v, message = "") {
  current = v;
  const box = $("order");
  box.replaceChildren();
  const pref = v.substitution_preference === "best_match" ? "Best match" : "Remove if out of stock";
  const head = el("div", { class: "panel" },
    el("div", { class: "row" },
      el("div", {}, el("a", { href: "#/" }, "← Orders"), " ", el("code", {}, v.order_id), " ", pill(v.status)),
      el("span", { class: "muted" }, `${v.customer_name} · ${v.address}`)),
    el("p", { class: "muted", style: "margin:6px 0 10px" }, `Customer preference if out of stock: ${pref}`),
    el("div", { class: "kpis" },
      kpi("Authorized (capturable)", v.capturable_cents == null ? "—" : money(v.capturable_cents)),
      kpi("Committed so far", money(v.committed_total_cents)),
      kpi("Room left in hold", v.headroom_cents == null ? "—" : money(v.headroom_cents)),
      kpi(v.captured_amount_cents != null ? "Captured" : "Final (preview)",
          v.captured_amount_cents != null ? money(v.captured_amount_cents)
            : v.final_preview ? money(v.final_preview.total_cents) : "—")));
  if (v.review_reason) head.append(el("div", { class: "error" }, v.review_reason));
  box.append(head);

  if (subPicker) box.append(subPickerPanel());

  const table = el("table", { class: "lines" },
    el("tr", {}, el("th", {}, "Item"), el("th", {}, "Status"), el("th", {}, "Line"), el("th", {}, "Pick")));
  const subsFor = new Map(v.items.filter((i) => i.substitute_for_item_id).map((i) => [i.substitute_for_item_id, i]));
  for (const i of v.items) {
    if (i.substitute_for_item_id) continue;
    let status = el("span", { class: "pill" }, i.status.replace("_", " "));
    if (i.status === "pending") status = el("span", { class: "muted" }, "To pick");
    if (i.status === "removed") status = el("span", { class: "pill bad" }, "Removed");
    const amount = i.final_line_cents != null ? money(i.final_line_cents)
      : ["removed", "substituted"].includes(i.status) ? el("span", { class: "strike" }, money(i.estimated_line_cents))
      : el("span", { class: "muted" }, `≤ ${money(i.hold_line_cents)}`);
    table.append(el("tr", {},
      el("td", {}, lineDescription(i)),
      el("td", {}, status),
      el("td", {}, amount),
      el("td", {}, ["pending", "picked"].includes(i.status) ? pickControls(i) : null)));
    const s = subsFor.get(i.id);
    if (s) {
      const sDesc = s.pricing_type === "fixed" ? `${s.name} × ${s.quantity}` : `${s.name} · ${s.actual_weight_lb} lb`;
      let sStatus;
      if (s.status === "proposed") sStatus = el("span", {}, el("span", { class: "pill warn" }, "Waiting for customer"), " ",
        el("span", { class: "countdown", "data-deadline": String(Date.now() + s.seconds_left * 1000) }, `${s.seconds_left}s`));
      else if (s.status === "picked") sStatus = el("span", { class: "pill" }, "Accepted");
      else sStatus = el("span", { class: "pill bad" }, "Declined");
      table.append(el("tr", {},
        el("td", {}, `↳ ${sDesc}`), el("td", {}, sStatus), el("td", {}, money(s.final_line_cents)), el("td", {})));
    }
  }
  box.append(el("div", { class: "panel" }, table));

  const actions = el("div", { class: "panel" });
  const msg = el("div", { id: "order-msg", class: message ? "muted" : "" }, message);
  if (v.status === "picking") {
    // When blocked, the button itself says why (not a grey button with a hint elsewhere).
    const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
    const label = v.can_finalize
      ? `Finalize & capture ${money(v.final_preview.total_cents)}`
      : v.unpicked
        ? `Can't finalize yet: ${plural(v.unpicked, "line", "lines")} still to pick`
        : `Can't finalize yet: waiting for the customer on ${plural(v.awaiting_customer, "substitution", "substitutions")}`;
    const finalize = el("button", { class: v.can_finalize ? "primary" : "primary blocked",
      onclick: () => confirm(`Finalize and charge the customer ${money(v.final_preview.total_cents)}?`) && act(api("/capture")) }, label);
    finalize.disabled = !v.can_finalize;
    actions.append(finalize);
  }
  if (v.can_retry_capture) actions.append(
    el("p", { class: "muted" }, "The last capture call didn't confirm. Checking first looks up the payment and only re-sends the capture if none went through."),
    el("button", { class: "primary", onclick: () => act(api("/capture")) }, "Check payment & finish capture"));
  if (v.can_cancel) actions.append(el("button", { class: "secondary danger", style: "margin-top:10px",
    onclick: () => confirm("Cancel this order and release the customer's hold?") && act(api("/cancel")) }, "Cancel order"));
  actions.append(msg);
  // No actions and nothing to say (captured / cancelled): no empty card.
  if (actions.children.length > 1 || message) box.append(actions);
  show("order");

  clearInterval(countdownTimer);
  countdownTimer = setInterval(() => {
    for (const node of document.querySelectorAll(".countdown[data-deadline]")) {
      node.textContent = `${Math.max(0, Math.ceil((Number(node.dataset.deadline) - Date.now()) / 1000))}s`;
    }
  }, 250);
}

// ── Router ───────────────────────────────────────────────────────────────
async function route() {
  clearTimeout(pollTimer);
  subPicker = null;
  try {
    await call("GET", "/store/api/me");
  } catch { return; }  // showLogin() already ran
  const m = location.hash.match(/^#\/order\/(.+)$/);
  try {
    if (m) await loadOrder(decodeURIComponent(m[1]));
    else {
      current = null;
      await loadQueue();
      pollTimer = setTimeout(route, 5000);
    }
  } catch (e) {
    if (e.message !== "Please log in.") alert(e.message);
  }
}

window.addEventListener("hashchange", route);
route();
