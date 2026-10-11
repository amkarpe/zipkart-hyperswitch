// ZipKart checkout. The browser only ever sees SKUs, quantities/weights, the
// server's quote, a client_secret and the publishable key. It never computes
// a price that the server trusts.

const HYPER_SDK_URL = "https://beta.hyperswitch.io/v1/HyperLoader.js";
const WEIGHT_STEP = 0.25;

const state = {
  products: [],
  cart: new Map(),   // sku -> { quantity } | { weight_lb }
  locked: false,     // true once an order exists: the cart is frozen into it
  orderId: null,
  hyper: null,
  widgets: null,
  returnUrl: null,
};

const $ = (id) => document.getElementById(id);
const money = (cents) => (cents / 100).toLocaleString("en-US", { style: "currency", currency: "USD" });

async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const detail = body.error || (Array.isArray(body.detail) ? body.detail.map((d) => d.msg).join("; ") : body.detail);
    throw new Error(detail || `Request failed (${resp.status})`);
  }
  return body;
}

// ── Catalog ──────────────────────────────────────────────────────────────
function renderCatalog() {
  const root = $("catalog");
  root.innerHTML = "";
  const byCategory = new Map();
  for (const p of state.products) {
    if (!byCategory.has(p.category)) byCategory.set(p.category, []);
    byCategory.get(p.category).push(p);
  }
  for (const [category, products] of byCategory) {
    const h = document.createElement("div");
    h.className = "category";
    h.textContent = category;
    root.appendChild(h);
    const grid = document.createElement("div");
    grid.className = "grid";
    for (const p of products) grid.appendChild(productCard(p));
    root.appendChild(grid);
  }
}

function productCard(p) {
  const card = document.createElement("div");
  card.className = "product";
  const unit = p.pricing_type === "weighed" ? "/ lb" : p.unit_label === "each" ? "each" : `· ${p.unit_label}`;
  card.innerHTML = `
    <div class="emoji">${p.emoji}</div>
    <div class="name"></div>
    <div class="price">${money(p.unit_price_cents)} ${unit}
      ${p.pricing_type === "weighed" ? '<span class="badge">by weight</span>' : ""}</div>
    <div class="controls"></div>`;
  card.querySelector(".name").textContent = p.name;

  const controls = card.querySelector(".controls");
  const line = state.cart.get(p.sku);
  if (!line) {
    const add = document.createElement("button");
    add.textContent = "Add";
    add.disabled = state.locked;
    add.onclick = () => {
      state.cart.set(p.sku, p.pricing_type === "weighed" ? { weight_lb: 1 } : { quantity: 1 });
      cartChanged();
    };
    controls.appendChild(add);
  } else {
    const stepper = document.createElement("div");
    stepper.className = "stepper";
    const minus = document.createElement("button");
    const plus = document.createElement("button");
    const label = document.createElement("span");
    minus.textContent = "−";
    plus.textContent = "+";
    minus.disabled = plus.disabled = state.locked;
    label.textContent = p.pricing_type === "weighed" ? `${line.weight_lb.toFixed(2)} lb` : `${line.quantity}`;
    minus.onclick = () => step(p, -1);
    plus.onclick = () => step(p, +1);
    stepper.append(minus, label, plus);
    controls.appendChild(stepper);
  }
  return card;
}

function step(p, dir) {
  const line = state.cart.get(p.sku);
  if (p.pricing_type === "weighed") {
    line.weight_lb = Math.round((line.weight_lb + dir * WEIGHT_STEP) * 100) / 100;
    if (line.weight_lb < WEIGHT_STEP) state.cart.delete(p.sku);
    else line.weight_lb = Math.min(line.weight_lb, 10);
  } else {
    line.quantity += dir;
    if (line.quantity < 1) state.cart.delete(p.sku);
    else line.quantity = Math.min(line.quantity, 24);
  }
  cartChanged();
}

function cartItems() {
  return [...state.cart.entries()].map(([sku, l]) => ({ sku, ...l }));
}

// ── Quote (display only; the server re-prices on order creation) ─────────
let quoteTimer = null;
function cartChanged() {
  renderCatalog();
  clearTimeout(quoteTimer);
  quoteTimer = setTimeout(refreshQuote, 150);
}

async function refreshQuote() {
  const items = cartItems();
  $("continue-btn").disabled = items.length === 0 || state.locked;
  if (items.length === 0) {
    $("cart-lines").textContent = "Add something tasty.";
    $("cart-lines").className = "muted";
    $("totals").classList.add("hidden");
    return;
  }
  try {
    renderQuote(await api("/api/quote", { method: "POST", body: JSON.stringify({ items }) }));
  } catch (e) {
    $("cart-lines").textContent = e.message;
  }
}

function renderQuote(q) {
  const lines = $("cart-lines");
  lines.className = "";
  lines.innerHTML = "";
  for (const l of q.lines) {
    const row = document.createElement("div");
    row.className = "row";
    const desc = l.pricing_type === "weighed" ? `${l.name} · ~${l.estimated_weight_lb} lb` : `${l.name} × ${l.quantity}`;
    row.innerHTML = `<span></span><span>${money(l.estimated_line_cents)}</span>`;
    row.firstChild.textContent = desc;
    lines.appendChild(row);
  }
  $("t-subtotal").textContent = money(q.subtotal_cents);
  $("t-tax").textContent = money(q.tax_cents);
  $("t-delivery").textContent = money(q.delivery_fee_cents);
  $("t-total").textContent = money(q.estimated_total_cents);
  const hasWeighed = q.lines.some((l) => l.pricing_type === "weighed");
  $("t-weighed-note").textContent = hasWeighed
    ? "Items sold by weight are estimates. You pay for the actual weight we pick."
    : "";
  $("totals").classList.remove("hidden");
}

// ── Order + payment ──────────────────────────────────────────────────────
$("details-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const btn = $("continue-btn");
  btn.disabled = true;
  $("details-error").classList.add("hidden");
  const f = new FormData(ev.target);
  const customer = Object.fromEntries(f.entries());
  customer.state = customer.state.toUpperCase();
  if (!customer.line2) delete customer.line2;
  try {
    const session = await api("/api/orders", {
      method: "POST",
      body: JSON.stringify({ items: cartItems(), customer }),
    });
    lockCart(session.order_id);
    await showPayment(session);
  } catch (e) {
    $("details-error").textContent = e.message;
    $("details-error").classList.remove("hidden");
    btn.disabled = false;
  }
});

function lockCart(orderId) {
  state.locked = true;
  state.orderId = orderId;
  // A refresh resumes this order instead of creating a second one.
  history.replaceState(null, "", `/?order=${encodeURIComponent(orderId)}`);
  renderCatalog();
  $("details-panel").classList.add("hidden");
}

async function showPayment(session) {
  if (!session.client_secret) {
    // Already authorized / in progress / finished: the status page knows what to say.
    window.location.href = `/orders/${encodeURIComponent(session.order_id)}/return`;
    return;
  }
  state.returnUrl = session.return_url;
  $("order-id").textContent = session.order_id;
  $("hold-message").innerHTML =
    `We'll place a temporary hold of <strong>${money(session.hold_amount_cents)}</strong>. ` +
    `You'll only be charged for what we deliver.`;
  $("payment-panel").classList.remove("hidden");

  await loadScript(HYPER_SDK_URL);
  state.hyper = window.Hyper(session.publishable_key);
  state.widgets = state.hyper.widgets({
    clientSecret: session.client_secret,
    appearance: { theme: matchMedia("(prefers-color-scheme: dark)").matches ? "midnight" : "default" },
  });
  const unifiedCheckout = state.widgets.create("payment", {
    wallets: { walletReturnUrl: session.return_url },
    displaySavedPaymentMethodsCheckbox: false,  // we don't store cards in this prototype
  });
  unifiedCheckout.mount("#unified-checkout");
  $("pay-btn").disabled = false;
}

$("pay-btn").addEventListener("click", async () => {
  const btn = $("pay-btn");
  btn.disabled = true;
  btn.textContent = "Placing hold…";
  $("payment-error").classList.add("hidden");
  // redirect: "always" sends every outcome through the return page, which asks
  // our server for the truth. The browser's view of the result is never trusted.
  const { error } = await state.hyper.confirmPayment({
    widgets: state.widgets,
    confirmParams: { return_url: state.returnUrl },
    redirect: "always",
  });
  if (error) {
    $("payment-error").textContent = error.message || "Payment could not be completed.";
    $("payment-error").classList.remove("hidden");
    btn.disabled = false;
    btn.textContent = "Place order";
  }
});

function loadScript(src) {
  return new Promise((resolve, reject) => {
    if (window.Hyper) return resolve();
    const s = document.createElement("script");
    s.src = src;
    s.onload = resolve;
    s.onerror = () => reject(new Error("Couldn't load the payment form. Check your connection."));
    document.head.appendChild(s);
  });
}

// ── Resume an existing order (?order=ID): refresh, expired session, failed card ──
async function resume(orderId) {
  const order = await api(`/api/orders/${encodeURIComponent(orderId)}`);
  for (const it of order.items) {
    const p = state.products.find((x) => x.sku === it.sku);
    if (!p) continue;
    state.cart.set(p.sku, it.pricing_type === "weighed"
      ? { weight_lb: Number(it.estimated_weight_lb) }
      : { quantity: it.quantity });
  }
  renderQuote({
    lines: order.items.map((i) => ({ ...i })),
    subtotal_cents: order.subtotal_cents,
    tax_cents: order.tax_cents,
    delivery_fee_cents: order.delivery_fee_cents,
    estimated_total_cents: order.estimated_total_cents,
  });
  lockCart(order.order_id);
  try {
    await showPayment(await api(`/api/orders/${encodeURIComponent(orderId)}/payment-session`, { method: "POST" }));
  } catch (e) {
    $("payment-panel").classList.remove("hidden");
    $("payment-error").textContent = e.message;
    $("payment-error").classList.remove("hidden");
  }
}

(async function init() {
  state.products = await api("/api/products");
  renderCatalog();
  const orderId = new URLSearchParams(location.search).get("order");
  if (orderId) {
    try {
      await resume(orderId);
    } catch (e) {
      history.replaceState(null, "", "/");
      $("details-error").textContent = `Couldn't resume order: ${e.message}`;
      $("details-error").classList.remove("hidden");
    }
  }
})();
