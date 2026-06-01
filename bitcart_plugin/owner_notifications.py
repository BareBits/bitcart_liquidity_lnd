"""Bitcart-backed store-owner email notifier (plugin mode only).

The engine (liquidityhelper.py) no longer carries its own SMTP config. In
plugin mode the plugin injects this notifier via
`liquidityhelper.set_store_owner_notifier(...)`; the engine then calls it as
``await notifier(store_id, subject, body)`` whenever it needs to alert the
operator about a specific store.

We email the **store owner** (``store.user_id -> User.email``, falling back
to the first superuser) using Bitcart's own installation-wide SMTP
(Server Management -> Policies) via ``api.utils.email`` — the same machinery
Bitcart uses for verification / password-reset emails. No plugin-specific
SMTP settings are involved.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from dishka import Scope
from sqlalchemy import select

from api import models
from api.schemas.policies import Policy
from api.services.crud.repositories import StoreRepository, UserRepository
from api.services.settings import SettingService
from api.utils.email import Email

logger = logging.getLogger("liquidityhelper.owner_notifications")


async def _first_superuser_email(user_repo: UserRepository) -> Optional[str]:
    """Email of the oldest superuser, or None."""
    stmt = (
        select(models.User)
        .where(models.User.is_superuser.is_(True))
        .order_by(models.User.created)
        .limit(1)
    )
    user = (await user_repo.session.execute(stmt)).scalar_one_or_none()
    return user.email if user is not None and user.email else None


async def resolve_owner_email(
    store_repo: StoreRepository, user_repo: UserRepository, store_id: str
) -> Optional[str]:
    """Resolve the notification recipient for ``store_id``: the store's
    owner (``store.user_id -> User.email``), falling back to the first
    superuser so an alert is never silently lost. None if neither resolves.
    """
    store = await store_repo.get_one_or_none(id=store_id)
    if store is not None and getattr(store, "user_id", None):
        user = await user_repo.get_one_or_none(id=store.user_id)
        if user is not None and user.email:
            return user.email
    return await _first_superuser_email(user_repo)


def make_store_owner_notifier(
    container: Any,
) -> Callable[[str, str, str], Awaitable[None]]:
    """Build the async notifier the engine calls: ``(store_id, subject, body)``.

    Resolves the store owner's email and sends via Bitcart's
    installation-wide Policy SMTP. No-op (with a warning) when the policy
    SMTP isn't configured or no recipient can be resolved.
    """

    async def notify(store_id: str, subject: str, body: str) -> None:
        async with container(scope=Scope.REQUEST) as rc:
            setting_service: SettingService = await rc.get(SettingService)
            store_repo: StoreRepository = await rc.get(StoreRepository)
            user_repo: UserRepository = await rc.get(UserRepository)

            policy = await setting_service.get_setting(Policy)
            email_obj = Email.get_email(policy)
            if not email_obj.is_enabled():
                logger.warning(
                    "Cannot send low-liquidity email for store %s: Bitcart's "
                    "installation-wide SMTP (Server Management -> Policies) is "
                    "not configured.",
                    store_id,
                )
                return

            recipient = await resolve_owner_email(store_repo, user_repo, store_id)
            if not recipient:
                logger.warning(
                    "Cannot send low-liquidity email for store %s: no store "
                    "owner or admin email could be resolved.",
                    store_id,
                )
                return

            # send_mail is blocking smtplib — run it off the event loop.
            await asyncio.to_thread(email_obj.send_mail, recipient, body, subject)
            logger.info(
                "Sent low-liquidity email for store %s to %s", store_id, recipient
            )

    return notify
