from app.config import Settings
from app.model_router import ModelRouter


def test_model_router_uses_sensitive_and_fallback_models():
    settings = Settings(
        chat_model="general-model",
        sensitive_chat_model="private-model",
        chat_fallback_models="fallback-a,fallback-b",
    )
    router = ModelRouter(settings)

    assert router.model_names("普通制度问题") == [
        "general-model",
        "fallback-a",
        "fallback-b",
    ]
    assert router.model_names("手机号 13800138000 的员工") == [
        "private-model",
        "fallback-a",
        "fallback-b",
    ]
