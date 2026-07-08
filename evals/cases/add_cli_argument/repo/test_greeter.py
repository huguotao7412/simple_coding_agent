from greeter import main


def test_default_greeting():
    assert main([]) == "Hello, world!"


def test_custom_name():
    assert main(["--name", "Ada"]) == "Hello, Ada!"


def test_shout_flag():
    assert main(["--name", "Ada", "--shout"]) == "HELLO, ADA!"
