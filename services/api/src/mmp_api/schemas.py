"""Request and response models.

Response models are explicit rather than "serialise the row". A row contains
``password_hash`` and ``key_hash``; a response never should, and the reliable
way to guarantee that is to name the fields that go out.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

# Reverse-DNS, as both stores require. Validated here so a typo is a 422 at
# registration rather than an attribution that silently never matches.
ANDROID_PACKAGE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*(\.[a-zA-Z][a-zA-Z0-9_]*)+$")
IOS_BUNDLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-]*(\.[A-Za-z0-9][A-Za-z0-9\-]*)+$")

Role = Literal["owner", "admin", "member", "viewer"]
Platform = Literal["android", "ios", "cross_platform"]


class Registration(BaseModel):
    email: EmailStr
    password: Annotated[str, Field(min_length=12, max_length=1024)]
    name: Annotated[str, Field(min_length=1, max_length=255)]
    organization_name: Annotated[str, Field(min_length=1, max_length=255)]


class Login(BaseModel):
    email: EmailStr
    password: Annotated[str, Field(max_length=1024)]


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    email: str
    name: str
    created_at: dt.datetime


class OrganizationOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    timezone: str
    role: Role | None = None


class OrganizationCreate(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=255)]
    timezone: str = "UTC"


class MemberInvite(BaseModel):
    email: EmailStr
    role: Role = "member"


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    name: str
    role: Role


class AppCreate(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=255)]
    platform: Platform
    android_package_name: str | None = None
    ios_bundle_id: str | None = None
    timezone: str = "UTC"
    install_window_days: Annotated[int, Field(ge=1, le=30)] = 7
    event_window_days: Annotated[int, Field(ge=1, le=90)] = 30

    @field_validator("android_package_name")
    @classmethod
    def _check_package(cls, v: str | None) -> str | None:
        if v is not None and not ANDROID_PACKAGE.match(v):
            raise ValueError("must look like com.example.app")
        return v

    @field_validator("ios_bundle_id")
    @classmethod
    def _check_bundle(cls, v: str | None) -> str | None:
        if v is not None and not IOS_BUNDLE.match(v):
            raise ValueError("must be reverse-DNS, e.g. com.example.App")
        return v


class AppUpdate(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=255)] | None = None
    timezone: str | None = None
    status: Literal["active", "paused", "disabled"] | None = None
    install_window_days: Annotated[int, Field(ge=1, le=30)] | None = None
    event_window_days: Annotated[int, Field(ge=1, le=90)] | None = None


class AppOut(BaseModel):
    id: uuid.UUID
    name: str
    platform: Platform
    android_package_name: str | None
    ios_bundle_id: str | None
    timezone: str
    status: str
    install_window_days: int
    event_window_days: int
    created_at: dt.datetime


class ApiKeyCreate(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=120)] = "default"
    environment: Literal["dev", "prod"] = "dev"
    kind: Literal["sdk", "s2s"] = "sdk"


class ApiKeyOut(BaseModel):
    """A stored key, as it can safely be shown again.

    Note the absence of any field that could reconstruct the credential. After
    creation, the prefix is all we have and all anyone gets.
    """

    id: uuid.UUID
    app_id: uuid.UUID
    name: str
    kind: str
    key_prefix: str
    environment: str
    status: str
    last_used_at: dt.datetime | None
    created_at: dt.datetime


class ApiKeyCreated(ApiKeyOut):
    """Returned exactly once, from the creation call only."""

    api_key: str
    warning: str = "Store this key now. It cannot be retrieved again."
