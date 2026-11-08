"""Command line helpers (normally not needed — everything is done from the web panel).

python -m app.cli create-admin EMAIL PASSWORD   # create/promote an admin (e.g. if you lost access)
python -m app.cli reset-password EMAIL PASSWORD
"""
import sys


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd in ("create-admin", "reset-password") and len(sys.argv) == 4:
        from sqlalchemy import select

        from .db import SessionLocal, User, init_db, utcnow
        from .security import hash_password
        init_db()
        email = sys.argv[2].lower()
        with SessionLocal() as s:
            u = s.scalars(select(User).where(User.email == email)).first()
            if u is None:
                if cmd == "reset-password":
                    sys.exit(f"no user {email}")
                u = User(email=email, password_hash="", accepted_terms_at=utcnow())
                s.add(u)
            u.password_hash = hash_password(sys.argv[3])
            if cmd == "create-admin":
                u.is_admin, u.status = True, "active"
            s.commit()
        print(f"ok: {email}")
        return
    print(__doc__)


if __name__ == "__main__":
    main()
