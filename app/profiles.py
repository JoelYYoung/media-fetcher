"""Per-user, per-site authentication and network profiles."""

from app import db, secret_store
from app.security import SITES, proxy_label


def public_profiles(user_id: str) -> list[dict]:
    result = []
    for site, config in SITES.items():
        item = db.profile(user_id, site) or {}
        proxy = ""
        if item.get("proxy_encrypted"):
            proxy = secret_store.decrypt(item["proxy_encrypted"])
        result.append(
            {
                "site": site,
                "label": config["label"],
                "has_cookies": bool(item.get("cookie_encrypted")),
                "has_proxy": bool(item.get("proxy_encrypted")),
                "proxy": proxy_label(proxy),
                "updated_at": item.get("updated_at"),
            }
        )
    return result


def save_cookies(user_id: str, site: str, cookies: str) -> dict:
    return db.save_profile(
        user_id, site, cookie_encrypted=secret_store.encrypt(cookies)
    )


def remove_cookies(user_id: str, site: str) -> None:
    db.save_profile(user_id, site, cookie_encrypted=None)


def save_proxy(user_id: str, site: str, proxy: str) -> dict:
    encrypted = secret_store.encrypt(proxy) if proxy else None
    return db.save_profile(user_id, site, proxy_encrypted=encrypted)


def runtime_profile(user_id: str, site: str | None) -> dict:
    if not site:
        return {"cookies": "", "proxy": ""}
    item = db.profile(user_id, site) or {}
    return {
        "cookies": secret_store.decrypt(item["cookie_encrypted"])
        if item.get("cookie_encrypted")
        else "",
        "proxy": secret_store.decrypt(item["proxy_encrypted"])
        if item.get("proxy_encrypted")
        else "",
    }
