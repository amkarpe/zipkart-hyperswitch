"""Server-side pricing. The browser never sends a price; it sends SKUs and quantities/weights.

All money is integer cents. Weights are Decimal pounds (exact), multiplied by a
per-lb price and rounded half-up to whole cents once per line.
"""
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

TAX_RATE_BPS = 825            # 8.25% on taxable items only (soda, paper goods)
DELIVERY_FEE_CENTS = 399
MAX_FIXED_QTY = 24
WEIGHT_STEP_LB = Decimal("0.25")
MAX_WEIGHT_LB = Decimal("10")


class PricingError(ValueError):
    pass


@dataclass(frozen=True)
class PricedLine:
    sku: str
    name: str
    pricing_type: str
    unit_price_cents: int
    unit_label: str
    taxable: bool
    quantity: int | None
    estimated_weight_lb: Decimal | None
    estimated_line_cents: int
    hold_line_cents: int      # estimate + buffer for weighed lines; == estimate for fixed


@dataclass(frozen=True)
class Quote:
    lines: list[PricedLine]
    subtotal_cents: int           # estimate
    tax_cents: int                # estimate
    delivery_fee_cents: int
    estimated_total_cents: int
    hold_items_cents: int         # sent to Hyperswitch as `amount`
    hold_tax_cents: int           # sent as `order_tax_amount`
    hold_amount_cents: int        # == Hyperswitch net_amount (amount + shipping_cost + order_tax_amount)

    @property
    def weight_buffer_cents(self) -> int:
        # Defined as the difference, so estimate + buffer == hold holds to the cent.
        return self.hold_amount_cents - self.estimated_total_cents


@dataclass(frozen=True)
class Totals:
    subtotal_cents: int
    tax_cents: int
    delivery_fee_cents: int
    total_cents: int


def totals(lines: list[tuple[int, bool]], delivery_fee_cents: int) -> Totals:
    """(line_cents, taxable) pairs -> subtotal, tax, delivery, total.

    The single place an order total is computed after checkout: the headroom
    check, the final capture amount and the receipt all call this, so they
    can't disagree with each other.
    """
    subtotal = sum(cents for cents, _ in lines)
    tax = tax_for(sum(cents for cents, taxable in lines if taxable))
    return Totals(subtotal, tax, delivery_fee_cents, subtotal + tax + delivery_fee_cents)


def _round_cents(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def tax_for(taxable_cents: int) -> int:
    return _round_cents(Decimal(taxable_cents) * TAX_RATE_BPS / Decimal(10_000))


def weighed_line_cents(price_per_lb_cents: int, weight_lb: Decimal) -> int:
    return _round_cents(Decimal(price_per_lb_cents) * weight_lb)


def weight_buffer_cents(estimated_line_cents: int, buffer_percent: int) -> int:
    # Round the buffer UP: a hold one cent short means a capture we can't make.
    return int((Decimal(estimated_line_cents) * buffer_percent / Decimal(100)).to_integral_value(ROUND_CEILING))


def price_cart(cart: list[dict], products: dict[str, dict], buffer_percent: int) -> Quote:
    """cart: [{"sku", "quantity"?, "weight_lb"?}]; products: sku -> products row."""
    if not cart:
        raise PricingError("Cart is empty.")

    seen: set[str] = set()
    lines: list[PricedLine] = []
    for entry in cart:
        sku = entry.get("sku")
        product = products.get(sku)
        if product is None:
            raise PricingError(f"Unknown product: {sku}")
        if not product["in_stock"]:
            raise PricingError(f"{product['name']} is out of stock.")
        if sku in seen:
            raise PricingError(f"{product['name']} appears twice in the cart.")
        seen.add(sku)

        if product["pricing_type"] == "fixed":
            qty = entry.get("quantity")
            if not isinstance(qty, int) or isinstance(qty, bool) or not 1 <= qty <= MAX_FIXED_QTY:
                raise PricingError(f"{product['name']}: quantity must be 1–{MAX_FIXED_QTY}.")
            est = product["unit_price_cents"] * qty
            lines.append(PricedLine(sku, product["name"], "fixed", product["unit_price_cents"],
                                    product["unit_label"], product["taxable"], qty, None, est, est))
        else:
            try:
                weight = Decimal(str(entry.get("weight_lb")))
            except Exception:
                raise PricingError(f"{product['name']}: invalid weight.")
            if not (WEIGHT_STEP_LB <= weight <= MAX_WEIGHT_LB) or weight % WEIGHT_STEP_LB != 0:
                raise PricingError(f"{product['name']}: weight must be {WEIGHT_STEP_LB}–{MAX_WEIGHT_LB} lb "
                                   f"in {WEIGHT_STEP_LB} lb steps.")
            weight = weight.quantize(Decimal("0.01"))
            est = weighed_line_cents(product["unit_price_cents"], weight)
            hold = est + weight_buffer_cents(est, buffer_percent)
            lines.append(PricedLine(sku, product["name"], "weighed", product["unit_price_cents"],
                                    product["unit_label"], product["taxable"], None, weight, est, hold))

    subtotal = sum(l.estimated_line_cents for l in lines)
    tax = tax_for(sum(l.estimated_line_cents for l in lines if l.taxable))
    hold_items = sum(l.hold_line_cents for l in lines)
    hold_tax = tax_for(sum(l.hold_line_cents for l in lines if l.taxable))
    return Quote(
        lines=lines,
        subtotal_cents=subtotal,
        tax_cents=tax,
        delivery_fee_cents=DELIVERY_FEE_CENTS,
        estimated_total_cents=subtotal + tax + DELIVERY_FEE_CENTS,
        hold_items_cents=hold_items,
        hold_tax_cents=hold_tax,
        hold_amount_cents=hold_items + hold_tax + DELIVERY_FEE_CENTS,
    )
