from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # eBay Browse API credentials (get from https://developer.ebay.com/my/keys)
    ebay_client_id: str
    ebay_client_secret: str

    # Pushover credentials (https://pushover.net)
    pushover_token: str  # Application API token
    pushover_user: str   # User/group key

    # Ollama
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2"  # default model; overridable per-search

    # Web UI (serves the monitoring/editing interface and REST API).
    # 0.0.0.0 so the Docker port mapping works; the UI has no authentication,
    # so only expose it on a trusted network.
    web_host: str = "0.0.0.0"
    web_port: int = 8787

    # Paths (set automatically inside Docker; override locally if needed)
    searches_file: str = "/app/searches.yaml"
    data_dir: str = "/data"

    # Auto-archiving of listings that are no longer active on eBay.
    # Each cycle verifies up to prune_batch_size least-recently-checked hits
    # (batched 20 per API call) and archives the ended/removed ones.
    prune_enabled: bool = True
    prune_interval_seconds: int = 3600
    prune_batch_size: int = 200

    log_level: str = "INFO"
