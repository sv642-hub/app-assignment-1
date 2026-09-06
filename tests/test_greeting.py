from app_assignment_1 import greeting


def test_greeting_default():
    assert greeting() == "Hello, world!"


def test_greeting_name():
    assert greeting("team") == "Hello, team!"
