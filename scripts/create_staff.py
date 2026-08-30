"""Provision a staff account (Phase 2.5).

There is deliberately no self-signup endpoint: the staff tier is admin-provisioned
(see docs/phases/phase-2.4-auth-plan.md). Before this script the only way to create an
account was hand-written SQL, which made the operator dashboard unusable.

Usage:
    python scripts/create_staff.py --org org_cambridge --name "City of Cambridge" \
        --email ops@cambridge.gov --role admin

The password is read from the POTHOLE_STAFF_PASSWORD environment variable, or
prompted for interactively — never passed as an argument, so it stays out of the
shell history and the process list.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
import uuid
from pathlib import Path

# Run directly (`python scripts/create_staff.py`) without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import EmailStr, TypeAdapter, ValidationError  # noqa: E402

from app.auth.passwords import hash_password  # noqa: E402
from app.database import create_pool  # noqa: E402

# The login route validates with EmailStr (app/models/auth.py). Validate the same
# way here, or you can provision an account that is unable to ever log in — e.g.
# EmailStr rejects reserved TLDs like ".local".
_EMAIL = TypeAdapter(EmailStr)

ROLES = ("admin", "staff", "viewer")  # mirrors org_member's CHECK constraint


async def create_staff(
    *, org_id: str, org_name: str, email: str, password: str, role: str, full_name: str | None
) -> str:
    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO org (org_id, name) VALUES ($1, $2) "
                    "ON CONFLICT (org_id) DO NOTHING",
                    org_id,
                    org_name,
                )
                # Email uniqueness is enforced case-insensitively by
                # idx_staff_user_email_lower, so look up the same way.
                existing = await conn.fetchval(
                    "SELECT user_id FROM staff_user WHERE lower(email) = lower($1)", email
                )
                if existing:
                    raise SystemExit(f"A staff user already exists for {email} ({existing}).")

                user_id = f"usr_{uuid.uuid4().hex}"
                await conn.execute(
                    "INSERT INTO staff_user (user_id, email, password_hash, full_name) "
                    "VALUES ($1, $2, $3, $4)",
                    user_id,
                    email,
                    hash_password(password),
                    full_name,
                )
                await conn.execute(
                    "INSERT INTO org_member (org_id, user_id, role) VALUES ($1, $2, $3)",
                    org_id,
                    user_id,
                    role,
                )
                return user_id
    finally:
        # Close the pool we made directly — database.close_pool() only closes the
        # module-global pool, which create_pool() never assigns.
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a staff account for the dashboard.")
    parser.add_argument("--org", required=True, help="org id, e.g. org_cambridge")
    parser.add_argument("--name", help="org display name (defaults to the org id)")
    parser.add_argument("--email", required=True)
    parser.add_argument("--role", default="staff", choices=ROLES)
    parser.add_argument("--full-name", default=None, help="the person's name")
    args = parser.parse_args()

    try:
        _EMAIL.validate_python(args.email)
    except ValidationError as e:
        sys.exit(f"{args.email} is not a usable login email: {e.errors()[0]['msg']}")

    password = os.environ.get("POTHOLE_STAFF_PASSWORD") or getpass.getpass("Password: ")
    if not password:
        sys.exit("A password is required.")

    user_id = asyncio.run(
        create_staff(
            org_id=args.org,
            org_name=args.name or args.org,
            email=args.email,
            password=password,
            role=args.role,
            full_name=args.full_name,
        )
    )
    print(f"Created {user_id} ({args.email}) as '{args.role}' in {args.org}.")


if __name__ == "__main__":
    main()
