from greeter import main


def test_default_greeting():
    assert main([]) == "Hello, world!"


def test_named_greeting():
    assert main(["--name", "Ada"]) == "Hello, Ada!"


def test_shout_flag_uppercases_output():
    assert main(["--name", "Ada", "--shout"]) == "HELLO, ADA!"
