from tournament.cli import main


def test_keys_check_ok(env_file, capsys):
    env_file("ALPACA_DEV_KEY_ID=PKTEST\nALPACA_DEV_SECRET_KEY=s3cret\n")
    assert main(["keys", "check", "--account", "dev"]) == 0
    assert "s3cret" not in capsys.readouterr().out


def test_keys_check_reports_missing(env_file, capsys):
    assert main(["keys", "check", "--account", "dev"]) == 1
    assert ".env.example" in capsys.readouterr().err


def test_keys_check_reports_insecure_file(env_file, capsys):
    env_file("ALPACA_DEV_KEY_ID=PKTEST\nALPACA_DEV_SECRET_KEY=s3cret\n", 0o644)
    assert main(["keys", "check", "--account", "dev"]) == 1
    assert "chmod 600" in capsys.readouterr().err


def test_hello_without_keys_fails_cleanly(env_file):
    assert main(["hello", "--account", "dev"]) == 1
