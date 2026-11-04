"""環境変数から読む設定（秘密情報はここ経由でのみ扱う）。

運用中に画面から変更する値（エスカレーション時間など）は settings テーブル側
（app/settings_store.py）に置く。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "production"  # production / dev / test
    base_url: str = "http://localhost:8080"
    data_dir: Path = Path("/data")
    session_secret: str = "change-me"
    # 設定値の暗号化（api_tokens など）に使う Fernet キー。未設定なら session_secret から導出
    encryption_key: str = ""

    # Discord OAuth2 / Bot
    discord_client_id: str = ""
    discord_client_secret: str = ""
    discord_bot_token: str = ""
    discord_guild_id: str = ""
    discord_channel_orders: str = ""  # フォーラムチャンネル
    discord_channel_listing: str = ""
    discord_channel_daily: str = ""
    discord_channel_weekly: str = ""
    discord_channel_system: str = ""
    discord_role_lv1: str = ""
    discord_role_lv2: str = ""

    # 開発用ログイン（APP_ENV=dev のときのみ有効）
    dev_login: bool = False

    # PSA Public API（任意。あればスクレイピングより優先）
    psa_api_token: str = ""

    # バックアップ先（S3互換オブジェクトストレージ）
    s3_endpoint_url: str = ""
    s3_bucket: str = ""
    s3_prefix: str = "ebay-ops/"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_region: str = "auto"
    backup_retention_days: int = 30  # オブジェクトストレージ側の保持日数
    # VPS ローカル（/data/backups）の保持日数。画像の全量 tar を毎日作るため短くする。
    # オブジェクトストレージ未設定のときは backup_retention_days を使う（唯一の保存先になるため）
    local_backup_retention_days: int = 3

    # eBay CSV の列マッピング（未指定ならリポジトリの config/ebay_csv_columns.json）
    ebay_csv_columns_path: Path = Path(__file__).resolve().parent.parent / "config" / "ebay_csv_columns.json"

    # 背景除去（rembg）。0 にすると常に元画像を使う
    enable_rembg: bool = True
    # u2net（標準・約1.3GB）/ u2netp（軽量）/ isnet-general-use など
    rembg_model: str = "u2net"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "db" / "app.sqlite3"

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.db_path}"

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"

    @property
    def originals_dir(self) -> Path:
        return self.images_dir / "originals"

    @property
    def public_images_dir(self) -> Path:
        # Caddy が /img/ として認証なしで配信するディレクトリ
        return self.images_dir / "public"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def is_dev(self) -> bool:
        return self.app_env in ("dev", "test")

    def ensure_dirs(self) -> None:
        for p in (self.db_path.parent, self.originals_dir, self.public_images_dir,
                  self.exports_dir, self.backups_dir, self.logs_dir):
            p.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_config() -> Config:
    return Config()


def reset_config() -> None:
    get_config.cache_clear()
