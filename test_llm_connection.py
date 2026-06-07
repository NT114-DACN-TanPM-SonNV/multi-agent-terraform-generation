#!/usr/bin/env python3
"""Quick diagnostic script to test LLM API connectivity."""
import os
import sys
from pathlib import Path

# Load environment
from dotenv import load_dotenv
load_dotenv()

if os.environ.get("LLM_USE_PROXY", "").lower() not in ("1", "true", "yes"):
    for _proxy_key in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
    ):
        os.environ.pop(_proxy_key, None)

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

def test_deepseek_connection():
    """Test if Deepseek API is accessible."""
    print("=" * 70)
    print("Testing Deepseek API Connection")
    print("=" * 70)

    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
    provider = os.environ.get("LLM_PROVIDER", "deepseek").lower()

    if provider != "deepseek":
        print(f"[FAIL] LLM_PROVIDER={provider} (not deepseek). Skipping Deepseek test.")
        return False

    if not api_key:
        print("[FAIL] DEEPSEEK_API_KEY not set in .env")
        return False

    print(f"[OK] API Key configured: {api_key[:20]}...{api_key[-10:]}")
    print(f"[OK] Model: {model}")
    print()

    try:
        from langchain_openai import ChatOpenAI

        print("Attempting to instantiate ChatOpenAI...")
        client = ChatOpenAI(
            model=model,
            max_tokens=256,
            temperature=0,
            api_key=api_key,
            base_url="https://api.deepseek.com/v1",
        )
        print("[OK] Client instantiated")

        print("\nAttempting to make a test API call...")
        result = client.invoke([{"role": "user", "content": "Say 'OK' in one word."}])
        response = result.content.strip()

        print(f"[OK] API call successful!")
        print(f"[OK] Response: {response}")
        return True

    except Exception as e:
        print(f"[FAIL] Error: {type(e).__name__}: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def test_nvidia_connection():
    """Test if NVIDIA API is accessible."""
    print("\n" + "=" * 70)
    print("Testing NVIDIA API Connection")
    print("=" * 70)

    api_key = os.environ.get("NVIDIA_API_KEY", "").strip()
    model = os.environ.get("NVIDIA_MODEL", "meta/llama-3.3-70b-instruct")

    if not api_key:
        print("[SKIP] NVIDIA_API_KEY not set in .env (OK if using Deepseek)")
        return None

    print(f"[OK] API Key configured: {api_key[:20]}...{api_key[-10:]}")
    print(f"[OK] Model: {model}")
    print()

    try:
        from langchain_nvidia_ai_endpoints import ChatNVIDIA

        print("Attempting to instantiate ChatNVIDIA...")
        client = ChatNVIDIA(model=model, max_tokens=256, temperature=0)
        print("[OK] Client instantiated")

        print("\nAttempting to make a test API call...")
        result = client.invoke([{"role": "user", "content": "Say 'OK' in one word."}])
        response = result.content.strip()

        print(f"[OK] API call successful!")
        print(f"[OK] Response: {response}")
        return True

    except Exception as e:
        print(f"[FAIL] Error: {type(e).__name__}: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    print("LLM Connectivity Diagnostic\n")

    deepseek_ok = test_deepseek_connection()
    nvidia_ok = test_nvidia_connection()

    print("\n" + "=" * 70)
    print("Summary")
    print("=" * 70)

    if deepseek_ok:
        print("[OK] Deepseek API is working")
    elif nvidia_ok:
        print("[WARN] Deepseek API failed, but NVIDIA API is working")
        print("  -> Switch to NVIDIA: unset LLM_PROVIDER or set to 'nvidia'")
    else:
        print("[FAIL] All LLM APIs failed")
        print("\nNext steps:")
        print("  1. Check your internet connection")
        print("  2. Verify API keys in .env are correct")
        print("  3. Check if the API endpoint is accessible (e.g., ping api.deepseek.com)")
        print("  4. Check for rate limiting or account issues with your API provider")
