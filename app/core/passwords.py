"""
Password hashes and session tokens.

scrypt from the standard library rather than bcrypt or argon2 from PyPI: it
is a memory-hard KDF that OpenSSL has shipped for years, and this project
installs on hosts that may only reach an internal PyPI mirror - one less
compiled dependency is one less thing that fails to install there.

The stored form is ``scrypt$n$r$p$salt$hash`` (base64), so the cost can be
raised later without invalidating existing hashes: verify() reads the cost
from the hash, and needs_rehash() says when to write a stronger one.

Also verifies the ``$apr1$`` hashes the proxy's htpasswd held before the
application had logins (scripts/mf-topomon.sh used `openssl passwd -apr1`).
That is what lets an upgrade import the existing account with its existing
password instead of printing a new one; the first successful login then
replaces it with scrypt.
"""
import base64
import hashlib
import hmac
import secrets

# 2**15 * 8 * 128 bytes = 32 MiB per hash, about 50 ms on one core. A login is
# rare; a guess should not be cheap.
_N, _R, _P = 2 ** 15, 8, 1
_MAXMEM = 64 * 1024 * 1024

MIN_LENGTH = 10


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, maxmem=_MAXMEM,
        dklen=32,
    )
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or not password:
        return False
    if stored.startswith("$apr1$"):
        return hmac.compare_digest(apr1(password, stored.split("$")[2]), stored)
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        got = hashlib.scrypt(
            password.encode("utf-8"), salt=base64.b64decode(salt),
            n=int(n), r=int(r), p=int(p), maxmem=_MAXMEM, dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, expected)


def needs_rehash(stored: str | None) -> bool:
    if not stored or not stored.startswith("scrypt$"):
        return True
    try:
        _, n, r, p, *_ = stored.split("$")
        return (int(n), int(r), int(p)) != (_N, _R, _P)
    except ValueError:
        return True


def password_problem(password: str, username: str = "") -> str | None:
    """Why this password is refused, or None. Deliberately short list."""
    if len(password) < MIN_LENGTH:
        return f"密碼至少 {MIN_LENGTH} 個字元"
    if username and username.lower() in password.lower():
        return "密碼不能包含帳號名稱"
    if len(set(password)) < 4:
        return "密碼太單調（至少要有 4 種不同的字元）"
    return None


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii", "replace")).hexdigest()


# -- apr1 (Apache MD5-crypt) ------------------------------------------------
#
# Only ever *verified*, never produced: it is MD5 and exists here to read the
# hashes an older install left behind. The algorithm is Poul-Henning Kamp's
# md5crypt with "$apr1$" as the magic string.

_ITOA64 = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def _to64(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_ITOA64[value & 0x3F])
        value >>= 6
    return "".join(out)


def apr1(password: str, salt: str) -> str:
    magic = b"$apr1$"
    pw = password.encode("utf-8")
    salt_b = salt[:8].encode("ascii")

    ctx = hashlib.md5(pw + magic + salt_b)
    final = hashlib.md5(pw + salt_b + pw).digest()
    for i in range(len(pw), 0, -16):
        ctx.update(final[:min(16, i)])
    i = len(pw)
    while i:
        ctx.update(b"\x00" if i & 1 else pw[:1])
        i >>= 1
    final = ctx.digest()

    for i in range(1000):
        c = hashlib.md5()
        c.update(pw if i & 1 else final)
        if i % 3:
            c.update(salt_b)
        if i % 7:
            c.update(pw)
        c.update(final if i & 1 else pw)
        final = c.digest()

    f = final
    encoded = (
        _to64((f[0] << 16) | (f[6] << 8) | f[12], 4)
        + _to64((f[1] << 16) | (f[7] << 8) | f[13], 4)
        + _to64((f[2] << 16) | (f[8] << 8) | f[14], 4)
        + _to64((f[3] << 16) | (f[9] << 8) | f[15], 4)
        + _to64((f[4] << 16) | (f[10] << 8) | f[5], 4)
        + _to64(f[11], 2)
    )
    return f"$apr1${salt[:8]}${encoded}"
