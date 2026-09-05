"""User, role, referral and notification repositories."""

from __future__ import annotations

import secrets
import string
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import selectinload

from app.core.timeutils import utcnow
from app.db.models.user import (
    Notification,
    Permission,
    Referral,
    RestockSubscription,
    Role,
    User,
    user_roles,
)
from app.db.repositories.base import BaseRepository, Page
from app.domain.enums import Language, NotificationKind, ReferralStatus, RoleName, UserStatus

_REFERRAL_ALPHABET = string.ascii_uppercase + string.digits


def generate_referral_code(length: int = 8) -> str:
    return "".join(secrets.choice(_REFERRAL_ALPHABET) for _ in range(length))


class UserRepository(BaseRepository[User]):
    model = User

    async def get_by_telegram_id(self, telegram_id: int) -> User | None:
        stmt = select(User).where(User.telegram_id == telegram_id)
        return await self.session.scalar(stmt)

    async def get_by_referral_code(self, code: str) -> User | None:
        stmt = select(User).where(User.referral_code == code.strip().upper())
        return await self.session.scalar(stmt)

    async def get_or_create(
        self,
        telegram_id: int,
        *,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        language: Language = Language.EN,
    ) -> tuple[User, bool]:
        """Fetch or create a user, refreshing their Telegram profile fields."""
        user = await self.get_by_telegram_id(telegram_id)
        if user is not None:
            changed = False
            for field, value in (
                ("username", username),
                ("first_name", first_name),
                ("last_name", last_name),
            ):
                if value is not None and getattr(user, field) != value:
                    setattr(user, field, value)
                    changed = True
            user.last_seen_at = utcnow()
            if user.is_bot_blocked:
                user.is_bot_blocked = False
                changed = True
            if changed:
                await self.session.flush()
            return user, False

        # Referral codes are short; retry on the (rare) collision.
        for _ in range(5):
            code = generate_referral_code()
            if await self.get_by_referral_code(code) is None:
                break
        else:  # pragma: no cover - astronomically unlikely
            code = generate_referral_code(12)

        user = User(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            language=language,
            referral_code=code,
            last_seen_at=utcnow(),
        )
        return await self.add(user), True

    async def search(
        self, query: str, *, page: int = 1, per_page: int = 10
    ) -> Page[User]:
        term = query.strip().lstrip("@")
        stmt = select(User).order_by(User.created_at.desc())
        if term:
            filters: list[Any] = [
                User.username.ilike(f"%{term}%"),
                User.first_name.ilike(f"%{term}%"),
                User.last_name.ilike(f"%{term}%"),
            ]
            if term.isdigit():
                filters.append(User.telegram_id == int(term))
            stmt = stmt.where(or_(*filters))
        return await self.paginate(stmt, page=page, per_page=per_page)

    async def set_status(self, user: User, status: UserStatus) -> User:
        user.status = status
        await self.session.flush()
        return user

    async def mark_bot_blocked(self, telegram_id: int) -> None:
        await self.session.execute(
            update(User).where(User.telegram_id == telegram_id).values(is_bot_blocked=True)
        )

    async def record_purchase(self, user: User, amount: Any) -> None:
        """Maintain the profile counters inside the order transaction."""
        user.completed_orders_count += 1
        user.total_spent = (user.total_spent or 0) + amount
        await self.session.flush()

    async def broadcast_targets(
        self,
        *,
        audience: str,
        language: Language | None = None,
        active_since: datetime | None = None,
        after_id: uuid.UUID | None = None,
        limit: int = 500,
    ) -> list[User]:
        """Resumable cursor over broadcast recipients."""
        stmt = (
            select(User)
            .where(User.status == UserStatus.ACTIVE)
            .where(User.is_bot_blocked.is_(False))
            .where(User.notifications_enabled.is_(True))
            .order_by(User.id)
            .limit(limit)
        )
        if after_id is not None:
            stmt = stmt.where(User.id > after_id)
        if language is not None:
            stmt = stmt.where(User.language == language)
        if audience == "active" and active_since is not None:
            stmt = stmt.where(User.last_seen_at >= active_since)
        if audience == "resellers":
            stmt = stmt.where(User.reseller_account.has())
        return list((await self.session.scalars(stmt)).all())


class RoleRepository(BaseRepository[Role]):
    model = Role

    async def get_by_name(self, name: RoleName) -> Role | None:
        stmt = select(Role).where(Role.name == name).options(selectinload(Role.permissions))
        return await self.session.scalar(stmt)

    async def list_all(self) -> list[Role]:
        stmt = select(Role).order_by(Role.name)
        return list((await self.session.scalars(stmt)).all())

    async def assign(self, user: User, role: Role, granted_by: User | None = None) -> None:
        existing = await self.session.execute(
            select(user_roles).where(
                user_roles.c.user_id == user.id, user_roles.c.role_id == role.id
            )
        )
        if existing.first() is not None:
            return
        await self.session.execute(
            user_roles.insert().values(
                user_id=user.id,
                role_id=role.id,
                granted_by_id=granted_by.id if granted_by else None,
                granted_at=utcnow(),
            )
        )

    async def revoke(self, user: User, role: Role) -> None:
        await self.session.execute(
            user_roles.delete().where(
                user_roles.c.user_id == user.id, user_roles.c.role_id == role.id
            )
        )

    async def ensure_permission(self, code: str, description: str = "") -> Permission:
        permission = await self.session.scalar(
            select(Permission).where(Permission.code == code)
        )
        if permission is None:
            permission = Permission(code=code, description=description)
            self.session.add(permission)
            await self.session.flush()
        return permission


class ReferralRepository(BaseRepository[Referral]):
    model = Referral

    async def get_for_user(self, referred_user_id: uuid.UUID) -> Referral | None:
        stmt = select(Referral).where(Referral.referred_user_id == referred_user_id)
        return await self.session.scalar(stmt)

    async def list_for_referrer(
        self, referrer_id: uuid.UUID, *, page: int = 1, per_page: int = 10
    ) -> Page[Referral]:
        stmt = (
            select(Referral)
            .where(Referral.referrer_id == referrer_id)
            .order_by(Referral.created_at.desc())
        )
        return await self.paginate(stmt, page=page, per_page=per_page)

    async def stats(self, referrer_id: uuid.UUID) -> dict[str, Any]:
        stmt = select(
            func.count(Referral.id),
            func.count(Referral.id).filter(
                Referral.status.in_([ReferralStatus.QUALIFIED, ReferralStatus.REWARDED])
            ),
            func.coalesce(func.sum(Referral.reward_amount), 0),
        ).where(Referral.referrer_id == referrer_id)
        invited, qualified, earned = (await self.session.execute(stmt)).one()
        return {"invited": invited or 0, "qualified": qualified or 0, "earned": earned or 0}


class NotificationRepository(BaseRepository[Notification]):
    model = Notification

    async def list_for_user(
        self, user_id: uuid.UUID, *, page: int = 1, per_page: int = 8
    ) -> Page[Notification]:
        stmt = (
            select(Notification)
            .where(Notification.user_id == user_id)
            .order_by(Notification.created_at.desc())
        )
        return await self.paginate(stmt, page=page, per_page=per_page)

    async def unread_count(self, user_id: uuid.UUID) -> int:
        stmt = select(func.count(Notification.id)).where(
            Notification.user_id == user_id, Notification.read_at.is_(None)
        )
        return int((await self.session.scalar(stmt)) or 0)

    async def mark_all_read(self, user_id: uuid.UUID) -> None:
        await self.session.execute(
            update(Notification)
            .where(Notification.user_id == user_id, Notification.read_at.is_(None))
            .values(read_at=utcnow())
        )

    async def create(
        self,
        user_id: uuid.UUID,
        *,
        kind: NotificationKind,
        title: str,
        body: str = "",
        payload: dict[str, Any] | None = None,
    ) -> Notification:
        return await self.add(
            Notification(
                user_id=user_id,
                kind=kind,
                title=title,
                body=body,
                payload=payload or {},
            )
        )

    async def pending_push(self, limit: int = 100) -> list[Notification]:
        stmt = (
            select(Notification)
            .where(Notification.pushed_at.is_(None))
            .order_by(Notification.created_at)
            .limit(limit)
        )
        return list((await self.session.scalars(stmt)).all())


class RestockRepository(BaseRepository[RestockSubscription]):
    model = RestockSubscription

    async def subscribe(
        self, user_id: uuid.UUID, product_id: uuid.UUID
    ) -> tuple[RestockSubscription, bool]:
        stmt = select(RestockSubscription).where(
            RestockSubscription.user_id == user_id,
            RestockSubscription.product_id == product_id,
        )
        existing = await self.session.scalar(stmt)
        if existing is not None:
            if existing.notified_at is not None:
                existing.notified_at = None
                await self.session.flush()
            return existing, False
        subscription = RestockSubscription(user_id=user_id, product_id=product_id)
        return await self.add(subscription), True

    async def pending_for_product(self, product_id: uuid.UUID) -> list[RestockSubscription]:
        stmt = select(RestockSubscription).where(
            RestockSubscription.product_id == product_id,
            RestockSubscription.notified_at.is_(None),
        )
        return list((await self.session.scalars(stmt)).all())
