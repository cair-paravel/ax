from __future__ import annotations

from fastapi import HTTPException

from runner.config import PLATFORM_BASE_DOMAIN
from runner.models import DeployConfig, IngressSpec


def effective_ingress(cfg: DeployConfig) -> IngressSpec:
    if cfg.ingress is not None:
        return IngressSpec.model_validate(cfg.ingress)
    return IngressSpec(mode="custom-domain", domains=list(cfg.domains))


def effective_domains(cfg: DeployConfig | None) -> tuple[list[str], str | None]:
    if cfg is None:
        return ([], None)
    ing = effective_ingress(cfg)
    if ing.mode == "custom-domain":
        return (list(ing.domains), None)
    if ing.mode == "platform-subdomain":
        if not PLATFORM_BASE_DOMAIN:
            raise HTTPException(status_code=500, detail="PLATFORM_BASE_DOMAIN not set for platform-subdomain ingress")
        if not ing.subdomain:
            raise HTTPException(status_code=400, detail="ingress.subdomain required for platform-subdomain")
        return ([f"{ing.subdomain}.{PLATFORM_BASE_DOMAIN}"], None)
    if ing.mode == "platform-path":
        if not PLATFORM_BASE_DOMAIN:
            raise HTTPException(status_code=500, detail="PLATFORM_BASE_DOMAIN not set for platform-path ingress")
        if not ing.path or not ing.path.startswith("/"):
            raise HTTPException(status_code=400, detail="ingress.path must start with '/' for platform-path")
        return ([PLATFORM_BASE_DOMAIN], ing.path.rstrip("/"))
    return ([], None)
