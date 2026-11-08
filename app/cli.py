"""python -m app.cli genkeys | create-admin EMAIL PASSWORD"""
import secrets
import sys

from cryptography.fernet import Fernet


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "genkeys":
        print(f"SECRET_KEY={secrets.token_urlsafe(48)}")
        print(f"MASTER_KEY={Fernet.generate_key().decode()}")
        print(f"WEBHOOK_SECRET={secrets.token_urlsafe(24)}")
        return
    if cmd == "create-admin" and len(sys.argv) == 4:
        from sqlalchemy import select

        from .db import SessionLocal, User, init_db, utcnow
        from .security import hash_password
        init_db()
        email = sys.argv[2].lower()
        with SessionLocal() as s:
            u = s.scalars(select(User).where(User.email == email)).first()
            if u is None:
                u = User(email=email, password_hash=hash_password(sys.argv[3]), accepted_terms_at=utcnow())
                s.add(u)
            u.is_admin, u.status = True, "active"
            s.commit()
        print(f"admin ready: {email}")
        return
    print(__doc__)


if __name__ == "__main__":
    main()
