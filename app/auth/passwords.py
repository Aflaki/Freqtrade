"""Password policy (NIST-style): length, variety, not common, not part of the email, not in known breaches."""
from __future__ import annotations

import hashlib
import logging

import httpx

log = logging.getLogger("relay.passwords")

# the most common passwords from public breach lists (lower-case); anything here is refused
COMMON = set("""
123456 123456789 12345678 password qwerty123 qwerty 1q2w3e4r 123123 111111 1234567890 password1 12345 1234567
000000 iloveyou 123321 qwertyuiop abc123 654321 dragon monkey 1qaz2wsx 1q2w3e qazwsx 666666 sunshine princess
football baseball welcome admin admin123 letmein trustno1 master shadow passw0rd p@ssw0rd p@ssword password123
password12 password1234 qwerty12 qwerty1234 asdfghjkl zxcvbnm 987654321 superman batman michael jordan23 liverpool
chelsea arsenal barcelona realmadrid starwars pokemon whatever freedom hello123 hello1234 iloveyou1 zaq12wsx
q1w2e3r4 q1w2e3r4t5 1qaz2wsx3edc changeme secret123 bitcoin bitcoin123 crypto crypto123 binance binance123
ethereum trading trader123 freqtrade 11111111 00000000 12341234 88888888 aa123456 a123456 a1234567 abcd1234
abc12345 1234qwer qwer1234 asdf1234 test1234 test123 guest 159753 147258369 123qwe 123qweasd 1q2w3e4r5t
iran1234 tehran tehran123 ali123456 reza123 mohammad
""".split())


def strength(password: str) -> int:
    """0..4 like common strength meters (used for the UI and the minimum policy)."""
    classes = sum(any(f(c) for c in password) for f in (str.islower, str.isupper, str.isdigit,
                                                        lambda c: not c.isalnum()))
    score = 0
    if len(password) >= 8:
        score += 1
    if len(password) >= 12:
        score += 1
    if classes >= 3:
        score += 1
    if len(password) >= 16 or (len(password) >= 12 and classes == 4):
        score += 1
    return min(score, 4)


def problems(password: str, email: str = "", name: str = "", min_length: int = 10) -> list[str]:
    """i18n keys of what is wrong with the password (empty = acceptable)."""
    out = []
    p = password or ""
    low = p.lower()
    if len(p) < min_length:
        out.append("pw_short")
    classes = sum(any(f(c) for c in p) for f in (str.islower, str.isupper, str.isdigit, lambda c: not c.isalnum()))
    if len(p) < 16 and classes < 3:
        out.append("pw_variety")
    if low in COMMON or low.rstrip("!.@#1") in COMMON:
        out.append("pw_common")
    local = (email or "").split("@")[0].lower()
    if (len(local) >= 4 and local in low) or (name and len(name) >= 4 and name.lower().replace(" ", "") in low.replace(" ", "")):
        out.append("pw_personal")
    if len(set(p)) <= 3:
        out.append("pw_common")
    return sorted(set(out))


async def breached(password: str) -> bool:
    """Have I Been Pwned (k-anonymity: only the first 5 hex chars of the SHA-1 leave the server). Fails open."""
    sha = hashlib.sha1(password.encode()).hexdigest().upper()
    prefix, suffix = sha[:5], sha[5:]
    try:
        async with httpx.AsyncClient(timeout=4, headers={"Add-Padding": "true"}) as c:
            r = await c.get(f"https://api.pwnedpasswords.com/range/{prefix}")
            if r.status_code != 200:
                return False
            for line in r.text.splitlines():
                h, _, count = line.partition(":")
                if h == suffix and int(count or 0) > 0:
                    return True
    except Exception as e:
        log.info("breach check skipped: %s", e)
    return False
