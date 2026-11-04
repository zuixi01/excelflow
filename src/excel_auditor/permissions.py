from __future__ import annotations


def can_manage(request) -> bool:
    return bool(getattr(request.state, "authenticated", False)) and getattr(request.state, "role", "") in {"owner", "admin", "service"}


def creator_scope(request) -> str | None:
    return None if can_manage(request) else request.state.user_id


def with_creators(items: list[dict], tenant: str, database) -> list[dict]:
    users = database.list_users(tenant) if database else []
    usernames = {user.id: user.username for user in users}
    owners = [user for user in users if user.role == "owner"]
    if tenant == "local" and len(owners) == 1:
        usernames.setdefault("local", owners[0].username)
    for item in items:
        creator = item.get("created_by_id") or item.get("payload", {}).get("actor")
        item.update(created_by_id=creator, created_by_username=usernames.get(creator, creator or ""))
    return items
