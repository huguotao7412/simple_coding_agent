TAX_RATE = 0.08


def invoice_total(items):
    subtotal = 0
    for item in items:
        subtotal += item["quantity"] * item["unit_price"]
    return round(subtotal + subtotal * TAX_RATE, 2)


def cart_total(items):
    subtotal = 0
    for item in items:
        subtotal += item["quantity"] * item["unit_price"]
    return round(subtotal + subtotal * TAX_RATE, 2)
