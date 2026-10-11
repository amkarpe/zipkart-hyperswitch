// Return page: Hyperswitch redirects here after confirm. We ignore the status in
// the query string and ask our server, which fetches the payment from Hyperswitch.

const $ = (id) => document.getElementById(id);
const money = (cents) => (cents / 100).toLocaleString("en-US", { style: "currency", currency: "USD" });
const orderId = decodeURIComponent(location.pathname.split("/")[2] || "");
const IN_FLIGHT = new Set(["creating", "requires_confirmation", "requires_customer_action", "processing"]);
const MAX_POLLS = 8;

function show(icon, title, body, action) {
  $("s-icon").textContent = icon;
  $("s-title").textContent = title;
  $("s-body").textContent = body || "";
  $("s-action").innerHTML = "";
  if (action) $("s-action").appendChild(action);
}

function retryLink(label) {
  const a = document.createElement("a");
  a.href = `/?order=${encodeURIComponent(orderId)}`;
  a.innerHTML = `<button class="primary" style="max-width:260px;margin-top:12px"></button>`;
  a.firstChild.textContent = label;
  return a;
}

function renderSummary(order) {
  $("s-order").textContent = order.order_id;
  const lines = $("s-lines");
  lines.innerHTML = "";
  for (const i of order.items) {
    const row = document.createElement("div");
    row.className = "row";
    row.innerHTML = `<span></span><span>${money(i.estimated_line_cents)}</span>`;
    row.firstChild.textContent = i.pricing_type === "weighed" ? `${i.name} · ~${i.estimated_weight_lb} lb` : `${i.name} × ${i.quantity}`;
    lines.appendChild(row);
  }
  $("s-total").textContent = money(order.estimated_total_cents);
  $("s-hold").textContent = money(order.hold_amount_cents);
  $("summary").classList.remove("hidden");
}

function render(order) {
  renderSummary(order);
  const p = order.payment || {};
  if (order.status === "authorized" || order.status === "picking" || order.status === "finalizing") {
    show("✅", "Order placed!",
      `We placed a temporary hold of ${money(order.hold_amount_cents)} on your card. ` +
      `You'll only be charged for what we deliver; the rest of the hold is released after we finalize your order.`);
    return "done";
  }
  if (order.status === "needs_review") {
    show("⚠️", "We're checking your payment", "Something didn't match on our side. Our team will contact you shortly; you won't be charged twice.");
    return "done";
  }
  if (order.status !== "pending_payment") {
    show("ℹ️", `Order ${order.status.replace("_", " ")}`, "");
    return "done";
  }
  if (IN_FLIGHT.has(p.status)) {
    show("⏳", "Confirming your payment…", "This usually takes a few seconds.");
    return "pending";
  }
  if (p.status === "failed") {
    show("❌", "Your card wasn't accepted",
      p.message || "The payment was declined. Please try a different card.",
      retryLink("Try another card"));
    return "done";
  }
  // requires_payment_method, cancelled, create_failed: the payment wasn't completed.
  show("💳", "Payment not completed", "Your order is saved. Finish paying to place it.", retryLink("Continue to payment"));
  return "done";
}

async function sync() {
  const resp = await fetch(`/api/orders/${encodeURIComponent(orderId)}/sync`, { method: "POST" });
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(body.error || `Request failed (${resp.status})`);
  return body;
}

(async function poll() {
  for (let i = 0; i < MAX_POLLS; i++) {
    try {
      if (render(await sync()) === "done") return;
    } catch (e) {
      show("⚠️", "Couldn't load your order", e.message);
      return;
    }
    await new Promise((r) => setTimeout(r, 2500));
  }
  show("⏳", "Still confirming your payment",
    "This is taking longer than usual. You can close this page: we'll update your order as soon as the payment settles.",
    retryLink("Check again"));
})();
