from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    keycloak_root_url: str
    keycloak_realm_name: str
    keycloak_client_id: str
    keycloak_client_secret: str
    keycloak_admin_password: str
    keycloak_redirect_uri: str
    postgres_password: str
    postgres_port: int
    postgres_path: str
    postgres_host: str
    postgres_user: str
    storage_path: str
    app_admin_username: str
    app_admin_email: str
    app_admin_password: str
    s3_endpoint_url: str
    s3_access_key: str
    s3_secret_key: str
    s3_bucket_name: str
    s3_region_name: str
    max_upload_size_mb: int
    # ключ шифрования refresh-токенов, несколько через запятую = ротация
    session_secret_key: str
    # ключ шифрования ПДн в базе (контакты), несколько через запятую = ротация
    pii_encryption_key: str
    # выключать только для локальной разработки по http
    session_cookie_secure: bool = True
    session_revalidate_seconds: int = 300
    session_revalidate_hard_limit_seconds: int = 900
    # куда вернуть браузер после входа
    post_login_redirect_url: str = "/front"
    # фоновые процессы: в тестах и втором процессе без них можно обойтись
    workers_enabled: bool = True
    session_cleanup_interval_seconds: int = 3600
    pause_expiry_interval_seconds: int = 30
    # сторож застоя и сроков: раз в час (дни календарные, точнее не нужно)
    watcher_interval_seconds: int = 3600
    # демо для жюри: эталонный воркфлоу и учётки ролей при старте (О 30)
    demo_mode: bool = False
    # общий пароль демо-учёток - публичный, в README
    demo_password: str = "quoll-demo"
    org_queue_interval_seconds: int = 30
    reconciler_interval_seconds: int = 60
    # очередь выгрузок отчётов и чистка их файлов
    report_worker_interval_seconds: int = 2
    report_cleanup_interval_seconds: int = 3600
    # процессов рендера отчётов в одном процессе приложения - под число CPU
    report_render_processes: int = 2
    # по умолчанию у движка 5 + 10: предпросмотр держит два соединения
    db_pool_size: int = 20
    db_max_overflow: int = 10
    # импорт: пределы только для него, у вложений свои
    import_max_size_mb: int = 10
    import_max_rows: int = 10000
    import_max_registry_rows: int = 2000
    import_draft_ttl_hours: int = 24
    import_applied_ttl_days: int = 7
    import_worker_interval_seconds: int = 2
    import_cleanup_interval_seconds: int = 3600

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()  # pyright: ignore [reportCallIssue]
