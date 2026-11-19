"""
Logging in against Active Directory (or any LDAP directory that accepts
user@domain binds).

The user's own credentials are the bind: no service account and no stored
directory password. The directory is asked two things - do these credentials
bind, and is this account in one of the allowed groups (nested groups
included, via AD's LDAP_MATCHING_RULE_IN_CHAIN) - and nothing it says
changes the account's role after the first login. Roles are granted here, in
the back office, so a group rename in AD cannot quietly make somebody an
administrator of the network map.

ldap3 is synchronous; every call here runs in a worker thread so that a
domain controller that takes its full timeout to refuse does not stall every
other request.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import ssl
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import urlparse

from app.core.sysconf import LdapSettings

logger = logging.getLogger(__name__)

# AD's "member of, through any number of nested groups" matching rule
_IN_CHAIN = "1.2.840.113556.1.4.1941"


@dataclass
class DirectoryUser:
    username: str
    display_name: str
    email: Optional[str]


class LdapUnavailable(Exception):
    """No configured server could be reached (as opposed to a wrong password)."""


def configured(c: LdapSettings) -> bool:
    return bool(c.enabled and c.urls and c.domain and c.base_dn)


def clean_username(raw: str) -> str:
    """DOMAIN\\alice, alice@corp.local and Alice are all 'alice'."""
    return (raw or "").strip().split("\\")[-1].split("@")[0].lower()


def _server(c: LdapSettings, url: str):
    from ldap3 import ALL, Server, Tls
    tls = None
    if url.lower().startswith("ldaps://"):
        tls = Tls(validate=ssl.CERT_REQUIRED, ca_certs_data=c.ca_pem.strip() or None)
    return Server(url, use_ssl=tls is not None, tls=tls, get_info=ALL,
                  connect_timeout=c.timeout)


def _bind(c: LdapSettings, user: str, password: str, urls: Optional[List[str]] = None):
    """Bind to the first server that answers. A wrong password is final."""
    from ldap3 import Connection
    from ldap3.core.exceptions import LDAPBindError, LDAPException
    last: Optional[Exception] = None
    for url in urls or c.urls:
        try:
            return Connection(_server(c, url), user=user, password=password,
                              auto_bind=True, receive_timeout=c.timeout)
        except LDAPBindError:
            # Every server would say the same, and trying them all would count
            # several failures against the account's lockout in AD.
            raise
        except (LDAPException, ssl.SSLError, OSError) as exc:
            logger.warning("LDAP server %s unusable, trying the next: %s", url, exc)
            last = exc
    raise LdapUnavailable(str(last) if last else "沒有設定 LDAP 伺服器")


def _filter(username: str, groups: List[str]) -> str:
    from ldap3.utils.conv import escape_filter_chars
    who = f"(sAMAccountName={escape_filter_chars(username)})"
    if not groups:
        return who
    terms = "".join(f"(memberOf:{_IN_CHAIN}:={escape_filter_chars(g)})" for g in groups)
    return f"(&{who}{terms if len(groups) == 1 else f'(|{terms})'})"


def _lookup(conn, c: LdapSettings, username: str, groups: Optional[List[str]] = None):
    from ldap3 import SUBTREE
    conn.search(c.base_dn, _filter(username, c.groups if groups is None else groups),
                SUBTREE, attributes=["displayName", "mail"])
    return conn.entries[0] if conn.entries else None


def _authenticate(c: LdapSettings, username: str, password: str) -> Optional[DirectoryUser]:
    from ldap3.core.exceptions import LDAPBindError, LDAPException
    # An empty password is an *anonymous* bind, which many directories accept.
    # Without this check, any username with an empty password would log in.
    if not password or not configured(c):
        return None
    try:
        conn = _bind(c, f"{username}@{c.domain}", password)
    except LDAPBindError:
        return None
    try:
        entry = _lookup(conn, c, username)
    except LDAPException as exc:
        logger.warning("LDAP search for %s failed: %s", username, exc)
        return None
    finally:
        conn.unbind()
    if entry is None:
        logger.info("LDAP account %s not found under the base DN or not in an allowed group", username)
        return None
    name = str(entry.displayName) if entry.displayName else username
    mail = str(entry.mail) if entry.mail else None
    return DirectoryUser(username=username, display_name=name, email=mail)


async def authenticate(c: LdapSettings, username: str, password: str) -> Optional[DirectoryUser]:
    """The directory account, or None for wrong credentials / not allowed.

    Raises LdapUnavailable when no server could be asked at all, so the
    caller can say "the directory is down" instead of "wrong password".
    """
    return await asyncio.to_thread(_authenticate, c, username, password)


def _tls_hint(exc: Exception) -> str:
    text = str(exc)
    if "CERTIFICATE_VERIFY_FAILED" in text or "certificate verify failed" in text:
        return f"TLS 憑證驗證失敗：網域控制站的憑證不是由受信任的 CA 簽發，請上傳公司 CA 憑證（{text}）"
    if "invalidCredentials" in text or "data 52e" in text:
        return "帳號或密碼錯誤"
    if "data 775" in text:
        return "帳號已被 AD 鎖定"
    if "data 533" in text:
        return "帳號已停用"
    return text


def _test(c: LdapSettings, username: str, password: str) -> List[Tuple[Optional[bool], str, str]]:
    from ldap3 import Connection
    from ldap3.core.exceptions import LDAPException
    steps: List[Tuple[Optional[bool], str, str]] = []
    if not c.urls or not c.domain or not c.base_dn:
        return [(False, "設定", "伺服器網址、網域、搜尋起點（Base DN）都必須填寫")]
    reachable: List[str] = []
    for i, url in enumerate(c.urls):
        u = urlparse(url)
        port = u.port or (636 if u.scheme == "ldaps" else 389)
        label = "網路連線" if len(c.urls) == 1 else f"網路連線（第 {i + 1} 台）"
        try:
            socket.create_connection((u.hostname, port), timeout=c.timeout).close()
        except OSError as exc:
            steps.append((False, label, f"{u.hostname}:{port} 無法連線（{exc}）。確認 DNS、防火牆與網域控制站"))
            continue
        try:
            Connection(_server(c, url), auto_bind=True, receive_timeout=c.timeout).unbind()
        except (LDAPException, ssl.SSLError, OSError) as exc:
            steps.append((False, label, f"{u.hostname}:{port} 連得上，但 LDAP 失敗：{_tls_hint(exc)}"))
            continue
        reachable.append(url)
        steps.append((True, label, f"{u.hostname}:{port} 連得上，"
                      + ("TLS 憑證驗證通過" if u.scheme == "ldaps" else "LDAP 有回應")))
    if not reachable:
        return steps
    if any(urlparse(u).scheme == "ldap" for u in reachable):
        steps.append((None, "加密", "ldap:// 沒有加密，使用者的密碼會以明文送到網域控制站，建議改用 ldaps://"))
    if not username:
        steps.append((None, "帳號驗證", "填入測試帳號與密碼可以進一步確認登入與群組設定"))
        return steps
    username = clean_username(username)
    if not password:
        steps.append((False, "帳號驗證", "要填測試密碼（空白密碼會變成匿名登入，一律拒絕）"))
        return steps
    try:
        conn = _bind(c, f"{username}@{c.domain}", password, reachable)
    except (LDAPException, LdapUnavailable, ssl.SSLError, OSError) as exc:
        steps.append((False, "帳號驗證", _tls_hint(exc)))
        return steps
    steps.append((True, "帳號驗證", f"{username}@{c.domain} 登入成功"))
    try:
        try:
            anywhere = _lookup(conn, c, username, groups=[])
        except LDAPException as exc:
            steps.append((False, "搜尋帳號", f"{exc}（確認搜尋起點 Base DN）"))
            return steps
        if anywhere is None:
            steps.append((False, "搜尋帳號", "在搜尋起點下找不到這個帳號，確認 Base DN"))
            return steps
        name = str(anywhere.displayName) if anywhere.displayName else username
        steps.append((True, "搜尋帳號", name + (f"（{anywhere.mail}）" if anywhere.mail else "")))
        if c.groups:
            hit = [g for g in c.groups if _lookup(conn, c, username, groups=[g]) is not None]
            if hit:
                steps.append((True, "群組", "是允許登入群組的成員：" + "、".join(hit)))
            else:
                steps.append((False, "群組", "不是任何一個允許登入群組的成員，這個帳號會被拒絕"))
    finally:
        conn.unbind()
    return steps


async def test(c: LdapSettings, username: str = "", password: str = ""):
    return await asyncio.to_thread(_test, c, username, password)
