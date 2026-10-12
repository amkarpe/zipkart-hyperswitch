// Customer order page: payment result, hold explanation, substitution prompts,
// cancel, and the final receipt. All numbers are server-computed.
//
// While payment is pending it asks the server to sync with Hyperswitch (POST /sync).
// After that it polls GET /api/orders/{id}, which reads our database only.

const orderId = decodeURIComponent(location.pathname.split("/")[2] || "");
const PAYMENT_IN_FLIGHT = new Set(["creating", "requires_confirmation", "requires_customer_action", "processing", "requires_merchant_action"]);
const LIVE_STATUSES = new Set(["pending_payment", "authorized", "picking", "finalizing", "capturing", "cancelling"]);
const REMEDY_LIVE = new Set(["approved", "refund_pending"]);   // keep polling until these settle
const POLL_MS = 3000;
const MAX_PAYMENT_SYNCS = 8;

let paymentSyncs = 0;
let lastStatus = null;
let pollTimer = null;
let countdownTimer = null;
let busy = false;              // a button action is in flight; skip polls so the UI doesn't flicker
const deadlines = new Map();   // substitute item id -> local deadline (ms), from server seconds_left

async function call(method, path, body) {
  const resp = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || data.detail || `Request failed (${resp.status})`);
  return data;
}

function hero(icon, title, body, action) {
  $("s-icon").textContent = icon;
  $("s-title").textContent = title;
  $("s-body").textContent = body || "";
  $("s-action").replaceChildren(...(action ? [action] : []));
}

function linkButton(href, label) {
  return el("a", { href }, el("button", { class: "primary", style: "max-width:260px;margin-top:12px" }, label));
}

function itemLabel(i) {
  if (i.pricing_type === "weighed") {
    return i.actual_weight_lb ? `${i.name} · ${i.actual_weight_lb} lb` : `${i.name} · ~${i.estimated_weight_lb} lb`;
  }
  return `${i.name} × ${i.picked_quantity ?? i.quantity}`;
}

const ITEM_STATUS = {
  pending: ["", ""],
  picked: ["Picked", ""],
  substitution_proposed: ["Out of stock", "warn"],
  substituted: ["Substituted", "warn"],
  removed: ["Removed", "bad"],
  proposed: ["Suggested", "warn"],
  declined: ["Declined", "bad"],
};

function renderItems(order) {
  const showStatus = order.status !== "pending_payment" && order.status !== "authorized";
  const lines = $("s-lines");
  lines.replaceChildren();
  // Each substitute directly under the line it replaces.
  const subs = new Map(order.items.filter((i) => i.substitute_for_item_id).map((i) => [i.substitute_for_item_id, i]));
  const ordered = order.items.filter((i) => !i.substitute_for_item_id).flatMap((i) => subs.has(i.id) ? [i, subs.get(i.id)] : [i]);
  for (const i of ordered) {
    if (i.status === "declined") continue;
    const [label, cls] = ITEM_STATUS[i.status] || [i.status, ""];
    const name = el("span", {}, itemLabel(i), " ");
    if (i.substitute_for_item_id) name.prepend("↳ ");
    if (showStatus && label) name.append(el("span", { class: `pill ${cls}` }, label));
    const cents = i.final_line_cents ?? i.estimated_line_cents;
    lines.append(el("div", { class: "row" }, name,
      el("span", { class: ["removed", "substituted", "substitution_proposed"].includes(i.status) ? "strike" : "" }, money(cents))));
  }
}

function renderPrompts(order) {
  const box = $("prompts");
  box.replaceChildren();
  const now = Date.now();
  for (const p of order.substitution_prompts) {
    deadlines.set(p.substitute_item_id, now + p.seconds_left * 1000);
    const countdown = el("span", { class: "countdown", "data-sub": p.substitute_item_id }, `${p.seconds_left}s`);
    const fallback = order.substitution_preference === "best_match"
      ? "If you don't answer, we'll make the swap." : "If you don't answer, we'll remove it.";
    box.append(el("div", { class: "prompt" },
      el("div", { class: "row" }, el("strong", {}, `${p.original_name} is out of stock.`), countdown),
      el("p", { style: "margin:6px 0 0" }, `Swap for ${p.substitute_description} (${money(p.substitute_line_cents)})?`),
      el("p", { class: "muted", style: "margin:4px 0 0" }, fallback),
      el("div", { class: "actions" },
        el("button", { class: "primary", onclick: () => decide(p.substitute_item_id, "accept") }, "Accept swap"),
        el("button", { class: "secondary", onclick: () => decide(p.substitute_item_id, "decline") }, "Remove from order")),
    ));
  }
  clearInterval(countdownTimer);
  if (order.substitution_prompts.length) countdownTimer = setInterval(tickCountdowns, 250);
}

function tickCountdowns() {
  let expired = false;
  for (const node of document.querySelectorAll(".countdown[data-sub]")) {
    const left = Math.max(0, Math.ceil((deadlines.get(Number(node.dataset.sub)) - Date.now()) / 1000));
    node.textContent = `${left}s`;
    if (left === 0) expired = true;
  }
  if (expired && !busy) refresh();  // the server applies the preference on its next read
}

async function decide(subId, decision) {
  busy = true;
  document.querySelectorAll(".prompt button").forEach((b) => (b.disabled = true));
  try {
    render(await call("POST", `/api/orders/${encodeURIComponent(orderId)}/substitutions/${subId}`, { decision }));
    schedule();
  } catch (e) {
    alert(e.message);  // e.g. "already resolved": the next poll shows what happened
    await refresh();
  } finally {
    busy = false;
  }
}

async function cancelOrder() {
  if (!confirm("Cancel this order? The hold on your card will be released.")) return;
  busy = true;
  try {
    const order = await call("POST", `/api/orders/${encodeURIComponent(orderId)}/cancel`);
    lastStatus = order.status;
    render(order);
    if (LIVE_STATUSES.has(order.status)) schedule();
  } catch (e) {
    alert(e.message);
    await refresh();
  } finally {
    busy = false;
  }
}

function renderReceipt(r) {
  const box = $("receipt");
  box.replaceChildren();
  if (r.changes.length) {
    const changes = el("div", { style: "margin-bottom:10px" });
    for (const c of r.changes) {
      changes.append(el("div", { class: "change" },
        el("div", { class: "row" }, el("strong", {}, c.name),
          el("span", {}, el("span", { class: "strike" }, money(c.estimated_line_cents)), " ", money(c.final_line_cents))),
        el("div", { class: "muted" }, c.detail)));
    }
    box.append(el("h2", { style: "font-size:14px" }, "What changed"), changes);
  }
  box.append(
    row("Items", money(r.subtotal_cents)),
    row("Tax", money(r.tax_cents)),
    row("Delivery", money(r.delivery_fee_cents)),
    row("Charged", money(r.charged_cents), "row total"),
    row("Temporary hold", money(r.hold_amount_cents)),
    row("Released", money(r.released_cents)),
    el("p", { class: "muted" }, "The released amount drops off your card, usually within a few days depending on your bank."),
  );
}

// ── Remedies (after capture): report missing / damaged / expired items ─────
const REASONS = { missing: "Missing", damaged: "Damaged", expired: "Expired" };
let reportOpen = false;   // keep the form open (and its typing) across polls

function renderRemedies(order) {
  const reportable = order.items.filter((i) => i.report);
  const show = order.remedies.length > 0 || reportable.length > 0;
  $("remedy-panel").classList.toggle("hidden", !show);
  if (!show) return;

  const list = $("remedy-list");
  list.replaceChildren();
  for (const r of order.remedies) {
    const what = r.quantity ? `${r.item_name} × ${r.quantity}` : r.item_name;
    const cls = ["refunded", "refunded_manually", "replacement_scheduled"].includes(r.status) ? "" : "warn";
    list.append(el("div", { class: "change" },
      el("div", { class: "row" }, el("strong", {}, `${REASONS[r.reason]}: ${what}`),
        el("span", { class: `pill ${cls}` }, r.resolution === "refund" ? money(r.amount_cents) : "Replacement")),
      el("div", { class: "muted" }, r.message),
      r.note ? el("div", { class: "muted" }, `“${r.note}”`) : null));
  }

  if (reportOpen || !reportable.length) {
    if (!reportable.length) $("report").replaceChildren();
    return;
  }
  $("report").replaceChildren(el("button", { class: "secondary", style: "margin-top:10px",
    onclick: () => { reportOpen = true; renderReportForm(reportable); } }, "Report a problem with an item"));
}

function renderReportForm(reportable) {
  const item = el("select", { id: "r-item" }, ...reportable.map((i) =>
    el("option", { value: String(i.id) }, i.substitute_for_item_id ? `${i.name} (substitute)` : i.name)));
  const reason = el("select", { id: "r-reason" }, ...Object.entries(REASONS).map(([v, l]) => el("option", { value: v }, l)));
  const qty = el("input", { type: "number", id: "r-qty", min: "1", value: "1" });
  const qtyWrap = el("label", {}, "How many? ", qty);
  const refund = el("input", { type: "radio", name: "r-res", value: "refund", checked: "checked" });
  const replace = el("input", { type: "radio", name: "r-res", value: "replacement" });
  const replaceLabel = el("label", { class: "radio" }, replace, el("span", {}, "Free replacement on my next order"));
  const note = el("input", { type: "text", id: "r-note", maxlength: "280", placeholder: "Optional: what happened?" });
  const preview = el("p", { class: "muted" });
  const err = el("div", { class: "error hidden" });

  const current = () => reportable.find((i) => String(i.id) === item.value);
  function update() {
    const i = current();
    const fixed = i.pricing_type === "fixed";
    qtyWrap.classList.toggle("hidden", !fixed);
    if (fixed) { qty.max = String(i.report.max_quantity); if (Number(qty.value) > i.report.max_quantity) qty.value = String(i.report.max_quantity); }
    const missing = reason.value === "missing";
    replaceLabel.classList.toggle("hidden", missing);   // missing -> refund only
    if (missing) refund.checked = true;
    const cents = fixed ? i.report.refund_per_unit_cents * Math.max(1, Number(qty.value) || 1) : i.report.refund_cents;
    preview.textContent = refund.checked
      ? `Refund: ${money(cents)} (includes tax; delivery isn't refunded)`
      : "We'll add a free replacement to your next order.";
  }
  for (const n of [item, reason, qty, refund, replace]) n.addEventListener("change", update);
  qty.addEventListener("input", update);

  const submit = el("button", { class: "primary", onclick: async () => {
    const i = current();
    submit.disabled = true;
    err.classList.add("hidden");
    busy = true;
    try {
      const order = await call("POST", `/api/orders/${encodeURIComponent(orderId)}/remedies`, {
        item_id: i.id, reason: reason.value, resolution: refund.checked ? "refund" : "replacement",
        quantity: i.pricing_type === "fixed" ? Number(qty.value) : null, note: note.value.trim() || null,
      });
      reportOpen = false;
      render(order);
    } catch (e) {
      err.textContent = e.message;
      err.classList.remove("hidden");
      submit.disabled = false;
    } finally { busy = false; }
  } }, "Submit");

  $("report").replaceChildren(el("form", { class: "report", onsubmit: (e) => e.preventDefault() },
    el("label", {}, "Item"), item,
    el("label", {}, "What's wrong?"), reason,
    qtyWrap,
    el("fieldset", { class: "pref" }, el("legend", {}, "What would you like?"),
      el("label", { class: "radio" }, refund, el("span", {}, "Refund")), replaceLabel),
    el("label", {}, "Note"), note,
    preview, err,
    el("div", { class: "actions", style: "display:flex;gap:8px" }, submit,
      el("button", { type: "button", class: "secondary", onclick: () => { reportOpen = false; renderRemedies(lastOrder); } }, "Cancel"))));
  update();
}

let lastOrder = null;

function render(order) {
  lastOrder = order;
  $("s-order").textContent = order.order_id;
  $("s-pref").textContent = order.substitution_preference === "best_match"
    ? "If something's out of stock: suggest a best match" : "If something's out of stock: remove it";
  renderItems(order);
  renderPrompts(order);
  $("summary").classList.remove("hidden");
  $("s-cancel").replaceChildren(...(order.can_cancel && order.status === "authorized"
    ? [el("button", { class: "secondary danger", style: "margin-top:12px", onclick: cancelOrder }, "Cancel order")] : []));

  const showHold = ["authorized", "picking", "finalizing", "capturing"].includes(order.status);
  $("hold-panel").classList.toggle("hidden", !showHold);
  if (showHold) renderHoldBreakdown($("hold-breakdown"), order.hold_breakdown);
  $("receipt-panel").classList.toggle("hidden", !order.receipt);
  if (order.receipt) renderReceipt(order.receipt);
  renderRemedies(order);

  const p = order.payment || {};
  switch (order.status) {
    case "authorized":
      return hero("✅", "Order placed!", `We placed a temporary hold of ${money(order.hold_amount_cents)} on your card. ` +
        "You'll only be charged for what we deliver.");
    case "picking":
      return hero("🛒", "Your shopper is picking your order", order.substitution_prompts.length
        ? "Something's out of stock. Please choose above." : "We'll let you know if anything is out of stock.");
    case "finalizing":
    case "capturing":
      return hero("🧾", "Finalizing your charge", "Your shopper is done. We're charging the final amount now.");
    case "captured":
    case "delivered":
      return hero("🚚", "Picked and on its way", `You were charged ${money(order.receipt.charged_cents)} and ` +
        `${money(order.receipt.released_cents)} of the hold was released.`);
    case "cancelling":
      if (p.status === "requires_merchant_action") return hero("⏳", "Cancellation requested",
        "Your payment is still being reviewed by the payment processor. Your order won't be picked, and we'll " +
        "release the hold as soon as the review finishes.");
      return hero("⏳", "Cancelling your order", "Releasing the hold on your card…");
    case "cancelled":
      return hero("🚫", "Order cancelled", (order.cancelled_by === "store"
        ? "The store cancelled this order. " : "") + "You weren't charged. The temporary hold has been released; " +
        "it usually disappears from your card within a few days depending on your bank.");
    case "needs_review":
      return hero("⚠️", "We're checking your payment", "Something didn't match on our side. Our team will contact you shortly; you won't be charged twice.");
    case "pending_payment":
      if (p.status === "requires_merchant_action") return hero("🔎", "Your payment is being reviewed",
        "Your bank or our payment processor is double-checking this payment. We'll start your order as soon as it's approved; you don't need to pay again.");
      if (PAYMENT_IN_FLIGHT.has(p.status)) return hero("⏳", "Confirming your payment…", "This usually takes a few seconds.");
      if (p.status === "failed") return hero("❌", "Your card wasn't accepted",
        p.message || "The payment was declined. Please try a different card.",
        linkButton(`/?order=${encodeURIComponent(orderId)}`, "Try another card"));
      return hero("💳", "Payment not completed", "Your order is saved. Finish paying to place it.",
        linkButton(`/?order=${encodeURIComponent(orderId)}`, "Continue to payment"));
    default:
      return hero("ℹ️", `Order ${order.status.replace("_", " ")}`, "");
  }
}

async function refresh() {
  let order;
  try {
    // While payment is unsettled, ask the server to check Hyperswitch; otherwise read our DB.
    const syncing = paymentSyncs < MAX_PAYMENT_SYNCS && (paymentSyncs === 0 || lastStatus === "pending_payment");
    if (syncing) {
      paymentSyncs++;
      order = await call("POST", `/api/orders/${encodeURIComponent(orderId)}/sync`);
    } else {
      order = await call("GET", `/api/orders/${encodeURIComponent(orderId)}`);
    }
  } catch (e) {
    hero("⚠️", "Couldn't load your order", e.message);
    return;
  }
  lastStatus = order.status;
  render(order);
  const paymentPending = order.status === "pending_payment" && PAYMENT_IN_FLIGHT.has(order.payment?.status);
  clearTimeout(pollTimer);
  if (order.status === "pending_payment" && !paymentPending) return;  // waiting on the customer, not on us
  if (LIVE_STATUSES.has(order.status) || order.remedies.some((r) => REMEDY_LIVE.has(r.status))) schedule();
}

function schedule() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(() => (busy ? schedule() : refresh()), POLL_MS);
}

// A tab in the background polls less; refresh as soon as the customer looks again.
document.addEventListener("visibilitychange", () => { if (!document.hidden && !busy) refresh(); });
refresh();
