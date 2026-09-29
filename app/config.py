from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')
    max_bot_token: str = ''
    max_bot_username: str = ''
    max_api_url: str = 'https://platform-api2.max.ru'
    max_ca_bundle: str = ''
    routerai_api_key: str = ''
    routerai_base_url: str = 'https://routerai.ru/api/v1'
    routerai_model: str = 'inclusionai/ling-3.0-flash-vl'
    public_base_url: str = ''
    webhook_secret: str = ''
    admin_password: str = ''
    bot_mode: Literal['disabled', 'polling', 'webhook'] = 'disabled'
    demo_mode: bool = False
    database_path: str = 'data/buddy.db'
