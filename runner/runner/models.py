from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class RuntimeConfig(BaseModel):
    backend: Literal["process"] = "process"
    python: str | None = None
    memory: str | None = None
    cpu: str | None = None


class DeployConfig(BaseModel):
    name: str
    type: str = Field(default="web")
    start: str
    port: int | None = None
    domains: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    ingress: dict[str, Any] | None = None
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)


class IngressSpec(BaseModel):
    mode: Literal["custom-domain", "platform-subdomain", "platform-path"] = "custom-domain"
    domains: list[str] = Field(default_factory=list)
    subdomain: str | None = None
    path: str | None = None


class AppSummary(BaseModel):
    name: str
    last_deploy: str | None = None
    type: str = "web"
    port: int | None = None
    domains: list[str] = Field(default_factory=list)
    platform_path: str | None = None
    running: bool | None = None
