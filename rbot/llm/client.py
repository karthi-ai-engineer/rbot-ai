"""Connection to the company LLM gateway (OpenAI-compatible, vLLM backend)."""
import os

import httpx
from dotenv import load_dotenv
from openai import OpenAI

from rbot import ROOT

load_dotenv(ROOT / ".env")

BASE_URL = os.getenv("TWAVE_BASE_URL", "https://tw-gateway.twave.co.jp/v1")
MODEL = os.getenv("TWAVE_MODEL", "unsloth/Qwen3.8-27B-NVFP4")


def get_client(timeout: float = 120.0, connect_timeout: float = 4.0) -> OpenAI:
    api_key = os.getenv("twave_api")
    if not api_key:
        raise RuntimeError("twave_api is not set in .env")
    # Some new TCP connections to the gateway hang at connect (observed intermittently
    # from this network); a short connect timeout + retries makes the next attempt succeed fast.
    return OpenAI(
        base_url=BASE_URL,
        api_key=api_key,
        timeout=httpx.Timeout(timeout, connect=connect_timeout),
        max_retries=3,
    )
