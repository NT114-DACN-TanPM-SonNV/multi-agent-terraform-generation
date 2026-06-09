#!/usr/bin/env python3
"""Warm Terraform provider cache from bootstrap/main.tf.

Run once before benchmark:
  python scripts/warm_provider_cache.py

This script uses the minimal Terraform bootstrap config in bootstrap/main.tf
and populates .tf_plugin_cache via terraform init.
"""
import os
import subprocess
import sys
from pathlib import Path

# UTF-8 output on Windows
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = Path(__file__).parent.parent
BOOTSTRAP_DIR = ROOT / "bootstrap"
CACHE_DIR = ROOT / ".tf_plugin_cache"
CACHE_DIR.mkdir(exist_ok=True)


def main() -> int:
    print(f"⏳ Warming provider cache from {BOOTSTRAP_DIR / 'main.tf'}")
    env = {
        **os.environ,
        "TF_PLUGIN_CACHE_DIR": str(CACHE_DIR),
    }
    result = subprocess.run(
        ["terraform", "init", "-no-color"],
        cwd=BOOTSTRAP_DIR,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print("✗ terraform init failed:")
        print(result.stderr or result.stdout)
        return 1

    provider_count = len(list(CACHE_DIR.glob("**/terraform-provider-*")))
    if provider_count == 0:
        print("⚠ Cache không có provider binary — kiểm tra bootstrap/main.tf")
        return 1

    print(f"✓ terraform init success")
    print(f"✓ Cache có {provider_count} provider binary(s)")
    print(f"\n✅ Cache sẵn sàng: {CACHE_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
