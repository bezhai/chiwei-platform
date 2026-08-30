from __future__ import annotations

from pathlib import Path

import pytest

from app.settings import load_settings

DEPLOY_DIR = Path(__file__).resolve().parents[1] / "deploy"
ENTRY_ENV_EXAMPLE = DEPLOY_DIR / "tagger-entry.env.example"
DOCTOR_SCRIPT = DEPLOY_DIR / "doctor-host.sh"

QWEN_ENV = (
    "TAGGER_QWEN_BASE_URL",
    "TAGGER_QWEN_MODEL",
    "TAGGER_QWEN_API_KEY",
    "TAGGER_QWEN_TIMEOUT_SECONDS",
    "TAGGER_QWEN_CONCURRENCY",
    "TAGGER_QWEN_MAX_NEW_TOKENS",
    "TAGGER_QWEN_MAX_VISION_TOKENS",
    "TAGGER_LOCAL_INFER_TIMEOUT_SECONDS",
    "TAGGER_MAX_BATCH_PATHS",
)

# gpu2 的 llama-swap 上实测（Qwen3-VL，1200x1600 图降采样后单图 prompt_tokens ≈ 2234）
SECONDS_PER_IMAGE_AT_CONCURRENCY_2 = 10.4  # 4 图 @并发2 墙钟 41.4s
COLD_LOAD_SECONDS = 28.0  # 显存里是别的模型时，llama-swap 换入 Qwen3-VL 的首请求额外等待


@pytest.fixture(autouse=True)
def _clear_qwen_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in QWEN_ENV:
        monkeypatch.delenv(name, raising=False)


def _entry_env_example() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in ENTRY_ENV_EXAMPLE.read_text("utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def test_qwen_http_defaults() -> None:
    settings = load_settings()
    assert settings.qwen_base_url == ""
    assert settings.qwen_model == ""
    assert settings.qwen_api_key == ""
    assert settings.qwen_timeout_seconds == 180.0
    # llama-server 起的是 -np 2（两个 slot），默认并发对齐 slot 数
    assert settings.qwen_concurrency == 2
    assert settings.qwen_max_new_tokens == 512
    assert settings.qwen_max_vision_tokens == 4096


def test_qwen_http_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAGGER_QWEN_BASE_URL", "http://127.0.0.1:8088/v1")
    monkeypatch.setenv("TAGGER_QWEN_MODEL", "qwen3-vl-8b")
    monkeypatch.setenv("TAGGER_QWEN_API_KEY", "token")
    monkeypatch.setenv("TAGGER_QWEN_TIMEOUT_SECONDS", "300")
    monkeypatch.setenv("TAGGER_QWEN_CONCURRENCY", "4")
    monkeypatch.setenv("TAGGER_QWEN_MAX_NEW_TOKENS", "256")
    monkeypatch.setenv("TAGGER_QWEN_MAX_VISION_TOKENS", "2048")

    settings = load_settings()
    assert settings.qwen_base_url == "http://127.0.0.1:8088/v1"
    assert settings.qwen_model == "qwen3-vl-8b"
    assert settings.qwen_api_key == "token"
    assert settings.qwen_timeout_seconds == 300.0
    assert settings.qwen_concurrency == 4
    assert settings.qwen_max_new_tokens == 256
    assert settings.qwen_max_vision_tokens == 2048


def test_local_infer_timeout_default_matches_the_entry_env_example() -> None:
    # 两处不一致时，沿用旧 env 文件（没写这一项）的部署会静默拿到代码默认值，而超时会 os._exit(1)
    settings = load_settings()
    example = float(_entry_env_example()["TAGGER_LOCAL_INFER_TIMEOUT_SECONDS"])
    assert example == settings.local_infer_timeout_seconds


def test_local_infer_timeout_default_covers_a_full_batch_plus_one_cold_load() -> None:
    # 整批超时必须罩得住 TAGGER_MAX_BATCH_PATHS 上限的完整批次 + 一次模型冷加载
    settings = load_settings()
    budget = settings.max_batch_paths * SECONDS_PER_IMAGE_AT_CONCURRENCY_2 + COLD_LOAD_SECONDS
    assert settings.local_infer_timeout_seconds >= budget


def test_doctor_host_requires_the_local_infer_timeout_for_the_entry_role() -> None:
    # doctor 不检查 = 配置缺失静默降级到代码默认值，正是这次要堵的坑
    text = DOCTOR_SCRIPT.read_text("utf-8")
    entry_block = text.split('if role == "entry":', 1)[1].split("else:", 1)[0]
    assert '"TAGGER_LOCAL_INFER_TIMEOUT_SECONDS"' in entry_block


def test_in_process_model_settings_are_gone() -> None:
    # 进程内 vLLM 的三个旋钮（本地权重路径 / 预加载 / 空闲卸载）随实现一起删除
    settings = load_settings()
    for name in ("qwen_model_path", "preload_local_qwen", "idle_unload_seconds"):
        assert not hasattr(settings, name)
