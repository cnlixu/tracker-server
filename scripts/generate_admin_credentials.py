"""Interactively generate production administrator authentication settings."""

from __future__ import annotations

from getpass import getpass
import re
import secrets

from argon2 import PasswordHasher


USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def main() -> None:
    username = input("Administrator username [admin]: ").strip() or "admin"
    if not USERNAME_PATTERN.fullmatch(username):
        raise SystemExit(
            "Username must contain 1-64 letters, digits, dots, underscores, or hyphens."
        )

    password = getpass("Administrator password: ")
    confirmation = getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("Passwords do not match.")
    if len(password) < 12:
        raise SystemExit("Password must contain at least 12 characters.")

    password_hash = PasswordHasher().hash(password)
    session_secret = secrets.token_urlsafe(48)

    print("\nAdd these values to /opt/tracker/.env:")
    print(f"ADMIN_USERNAME={username}")
    print(f"ADMIN_PASSWORD_HASH={password_hash}")
    print(f"SESSION_SECRET={session_secret}")
    print("AUTH_SESSION_MAX_AGE_SECONDS=28800")


if __name__ == "__main__":
    main()
