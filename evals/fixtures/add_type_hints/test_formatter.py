from formatter import format_user, initials


def test_format_user_behavior():
    assert format_user("Ada", 37) == "Ada (37)"


def test_initials_behavior():
    assert initials("ada lovelace") == "AL"


def test_public_functions_have_annotations():
    assert format_user.__annotations__ == {
        "name": str,
        "age": int,
        "return": str,
    }
    assert initials.__annotations__ == {
        "full_name": str,
        "return": str,
    }
