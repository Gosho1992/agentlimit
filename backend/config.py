from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Vultr Serverless Inference (OpenAI-compatible). Model IDs are checked
    # against GET /models at startup — use IDs exactly as that endpoint lists them.
    vultr_inference_key: str = ""
    vultr_inference_base: str = "https://api.vultrinference.com/v1"
    vultr_model_default: str = "glm-5.2"
    vultr_model_cheap: str = "glm-5.3-flash"

    microsandbox_image: str = "python:3.11-slim"
    microsandbox_memory_mb: int = 512
    microsandbox_cpus: int = 1
    allow_docker_fallback: bool = False
    sandbox_exec_user: str = "nobody"   # agent code runs unprivileged in the guest; "" = root

    workspace_dir: str = "workspace"
    cgroup_root: str = "/sys/fs/cgroup/agentlimit"
    ledger_dir: str = "/var/lib/agentlimit/ledger"
    signer_key_path: str = "/var/lib/agentlimit/keys/signer.ed25519"
    usage_db_path: str = "/var/lib/agentlimit/usage.db"

    db_url: str = "sqlite+aiosqlite:///./agentlimit.db"
    poll_interval_ms: int = 500
    pause_timeout_s: float = 120.0
    vmm_memory_headroom_mb: int = 192   # host memory budget = guest RAM + this
    inflight_drain_s: float = 15.0
    watchdog_stall_s: float = 15.0
    guest_mode_enabled: bool = True      # public "try it yourself": preset scenarios only
    guest_max_live: int = 1              # guest runs at once (MAX_CONCURRENT_SESSIONS still applies)
    guest_per_ip_per_hour: int = 3       # event-loop heartbeat age that triggers the out-of-loop kill
    token_estimate_chars_per_token: float = 3.0   # conservative (real ~3.5-4)
    token_estimate_margin: float = 1.2            # extra safety margin on prompt estimate
    agent_max_completion_tokens: int = 6000       # reasoning models need room before the code
    inference_timeout_s: float = 180.0

    # --- public-deployment protection ---
    access_token: str = ""              # REQUIRED unless auth_disabled
    auth_disabled: bool = False         # only for local development
    allowed_origins: str = ""           # comma-separated; empty = same-origin only
    public_deployment: bool = True      # refuse AUTH_DISABLED / missing token in /ready
    cookie_secure: bool = False         # set true once HTTPS is configured
    max_concurrent_sessions: int = 2
    max_sessions_per_hour: int = 20
    max_tokens_per_day: int = 2_000_000
    frontend_dist: str = "frontend/dist"

    default_cpu_budget_seconds: int = 30
    default_wall_budget_seconds: int = 300
    default_token_budget: int = 50_000
    default_memory_budget_mb: int = 512


settings = Settings()
