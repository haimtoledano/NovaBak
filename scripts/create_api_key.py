#!/usr/bin/env python3
"""Create a NovaBak API key for automation."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import SessionLocal, User, init_db
import auth


def main():
    parser = argparse.ArgumentParser(description="Create a NovaBak API key")
    parser.add_argument("--name", default="automation", help="Key label")
    parser.add_argument("--username", default="admin", help="User to attach key to")
    args = parser.parse_args()

    init_db()
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == args.username).first()
        if not user:
            print(f"User '{args.username}' not found", file=sys.stderr)
            return 1
        raw_key, api_key = auth.create_api_key(db, user.id, args.name)
        print(f"API key created: id={api_key.id} name={api_key.name}")
        print(raw_key)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
