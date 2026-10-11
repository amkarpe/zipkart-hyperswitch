// Shared by the checkout and order pages. Renders numbers the server computed;
// it never adds anything up itself.

const $ = (id) => document.getElementById(id);
const money = (cents) => (cents / 100).toLocaleString("en-US", { style: "currency", currency: "USD" });

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children) if (c != null) node.append(c);  // strings become text nodes (no HTML injection)
  return node;
}

function row(label, value, cls = "row") {
  return el("div", { class: cls }, el("span", {}, label), el("span", {}, value));
}

function listNames(names) {
  if (names.length <= 2) return names.join(" and ");
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

// estimated total + weight buffer = temporary hold, every number from the server.
function renderHoldBreakdown(container, b) {
  container.innerHTML = "";
  if (!b) return;
  const box = el("div", { class: "breakdown" });
  box.append(row("Estimated total", money(b.estimated_total_cents)));
  if (b.weighed_items.length) {
    box.append(row(`+ Weight buffer (${b.weight_buffer_percent}% on ${listNames(b.weighed_items)})`,
                   money(b.weight_buffer_cents)));
  }
  box.append(row("= Temporary hold", money(b.hold_amount_cents), "row total"));
  const note = b.weighed_items.length
    ? "If weighed items come in heavier, the buffer covers it. You're only charged for the actual weight, " +
      "and any unused amount is released, usually within a few days depending on your bank."
    : "You're only charged for what we deliver. Any unused amount is released, usually within a few days " +
      "depending on your bank.";
  box.append(el("p", { class: "muted" }, note));
  container.append(box);
}
