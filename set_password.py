"""
Create or rotate a portal account.

  python set_password.py <username>              -> generates a password, prints it
  python set_password.py <username> <password>   -> uses the given password

Only the werkzeug hash is stored in users.json.
"""
import json
import os
import secrets
import sys

from werkzeug.security import generate_password_hash

USERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.json")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    username = sys.argv[1].strip().lower()
    password = sys.argv[2] if len(sys.argv) > 2 else secrets.token_urlsafe(9)

    users = {}
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE) as f:
            users = json.load(f)
    users[username] = generate_password_hash(password)
    with open(USERS_FILE, "w") as f:
        json.dump(users, f, indent=2)
    os.chmod(USERS_FILE, 0o600)
    print(f"{username}: {password}")


if __name__ == "__main__":
    main()
