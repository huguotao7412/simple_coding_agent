from pricing import cart_total, invoice_total


ITEMS = [
    {"quantity": 2, "unit_price": 10.0},
    {"quantity": 1, "unit_price": 5.0},
]


def test_invoice_total():
    assert invoice_total(ITEMS) == 27.0


def test_cart_total():
    assert cart_total(ITEMS) == 27.0
