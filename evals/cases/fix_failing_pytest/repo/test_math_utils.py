import pytest

from math_utils import divide


def test_divide_returns_quotient():
    assert divide(12, 3) == 4


def test_divide_by_zero_raises():
    with pytest.raises(ZeroDivisionError):
        divide(1, 0)
