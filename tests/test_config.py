from app.config import Settings


def test_empty_process_environment_overrides_dotenv_secret(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text("OPENAI_API_KEY=secret-from-file\n", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "")

    settings = Settings(_env_file=dotenv)

    assert settings.openai_api_key == ""
    assert settings.model_ready is False
