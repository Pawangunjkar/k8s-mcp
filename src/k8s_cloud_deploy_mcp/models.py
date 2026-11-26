"""Pydantic report contracts for read-only and mutating tool payloads."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ReadOnlyReport(BaseModel):
    model_config = ConfigDict(extra="allow")

    ok: bool
    access: Literal["read-only"]
    tool: str


class MutationReport(BaseModel):
    model_config = ConfigDict(extra="allow")

    ok: bool
    access: Literal["mutating"]
    applied: bool
    confirmation_required: bool
    tool: str
