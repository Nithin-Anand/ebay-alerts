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

    # Paths (set automatically inside Docker; override locally if needed)
    searches_file: str = "/app/searches.yaml"
    data_dir: str = "/data"

    log_level: str = "INFO"
