"""Gateway configuration (TOML).

Example:
    db = "~/.truecost/ledger.db"
    run = "retail-baseline-s300"
    workload = "tau2-retail"
    condition = "baseline"

    [upstreams.strong]
    base_url = "http://127.0.0.1:30011/v1"
    model = "qwen3.8-27b"
    gpu = 0

    [upstreams.cheap]
    base_url = "http://127.0.0.1:30013/v1"
    model = "qwen3.5-4b"
    gpu = 1

    [roles]               # which tier serves non-agent roles
    simulator = "sim"
    judge = "sim"

    [[savers]]
    name = "mask"
    keep_last = 8
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Upstream:
    name: str
    base_url: str
    model: str
    gpu: int | None = None
    price: str | None = None  # priced model id; defaults to `model`
    api_key: str = "local"
    extra_body: dict = field(default_factory=dict)  # defaults merged into every request (request wins)
    force: dict = field(default_factory=dict)       # overrides applied to every request (config wins)
    max_tokens_cap: int | None = None               # clamp max_tokens (runaway generations repeat at T=0)
    strip_grammar: bool = False                     # drop strict tool schemas / response_format (no constrained decoding)
    switchyard: bool = False                        # an external router (NeMo Switchyard) that calls our tiers back
                                                    # through this gateway; see gateway.py "Switchyard mode"
    book_role: str | None = None                    # book calls arriving for this tier under this role (e.g. "router")

    @property
    def price_model(self) -> str:
        return self.price or self.model

    def __post_init__(self):
        if self.api_key.startswith("env:"):  # keep secrets out of config files
            import os

            self.api_key = os.environ.get(self.api_key[4:], "")


@dataclass
class Config:
    db: str = "~/.truecost/ledger.db"
    run: str | None = None
    workload: str | None = None
    condition: str | None = None
    source: str = "gateway"
    default_tier: str = "strong"
    upstreams: dict[str, Upstream] = field(default_factory=dict)
    roles: dict[str, str] = field(default_factory=dict)
    model_map: dict[str, str] = field(default_factory=dict)  # glob on the requested model -> tier
    savers: list[dict] = field(default_factory=list)
    governor: dict | None = None
    nvml: bool = True                   # false: no energy meter (hosted APIs); true meters the tiers that set `gpu`
    nvml_gpus: list[int] | None = None  # GPUs to read (default: all)
    timeout_s: float = 900.0

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        d = dict(d)
        ups = {name: Upstream(name=name, **u) for name, u in d.pop("upstreams", {}).items()}
        known = {f for f in cls.__dataclass_fields__}
        cfg = cls(**{k: v for k, v in d.items() if k in known}, upstreams=ups)
        if cfg.default_tier not in cfg.upstreams and cfg.upstreams:
            cfg.default_tier = next(iter(cfg.upstreams))
        return cfg

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        with open(Path(path).expanduser(), "rb") as f:
            return cls.from_dict(tomllib.load(f))

    def tier_for(self, model: str | None, role: str) -> str:
        """Pick the upstream for a request: role mapping first, then the requested model, then default."""
        if role in self.roles and self.roles[role] in self.upstreams:
            return self.roles[role]
        if model:
            from fnmatch import fnmatch

            for pat, tier in self.model_map.items():
                if fnmatch(model.lower(), pat.lower()) and tier in self.upstreams:
                    return tier
            if model in self.upstreams:
                return model
            for name, u in self.upstreams.items():
                if u.model == model:
                    return name
        return self.default_tier
