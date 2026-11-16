import smtplib

from app import mailer, settings
from app.config import EmailConfig


class FakeSMTP:
    instances: list = []

    def __init__(self, server, port, timeout=None, context=None):
        self.logged_in, self.sent = None, []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, message):
        self.sent.append(message)


def email() -> EmailConfig:
    return EmailConfig(smtp_server="smtp.example.com", to_addrs=["a@example.com"])


def test_the_login_comes_from_the_username_and_password_variables(monkeypatch):
    FakeSMTP.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("JABS_SMTP_USERNAME", "me@example.com")
    monkeypatch.setenv("JABS_SMTP_PASSWORD", "secret")
    assert mailer.send(email(), "Subject", "Body") is True
    server = FakeSMTP.instances[0]
    assert server.logged_in == ("me@example.com", "secret") and server.sent[0]["From"] == "me@example.com"


def test_the_older_short_names_still_work(monkeypatch):
    monkeypatch.setenv("JABS_SMTP_USER", "old")
    monkeypatch.setenv("JABS_SMTP_PASS", "oldpw")
    assert settings.smtp_login() == ("old", "oldpw")


def test_the_new_names_win_over_the_old_ones(monkeypatch):
    monkeypatch.setenv("JABS_SMTP_USER", "old")
    monkeypatch.setenv("JABS_SMTP_PASS", "oldpw")
    monkeypatch.setenv("JABS_SMTP_USERNAME", "new")
    monkeypatch.setenv("JABS_SMTP_PASSWORD", "newpw")
    assert settings.smtp_login() == ("new", "newpw")


def test_no_login_variables_mean_no_login(monkeypatch):
    assert settings.smtp_login() == ("", "")
