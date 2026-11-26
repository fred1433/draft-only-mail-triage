"""Generates the example outputs with a real local Qwen, through the real job client.

The job's LLMClient talks to a local OpenAI-compatible server (Ollama) through the same
AllowlistTransport; the mailbox is the fake. Each prompt and raw output is written to
examples/<model>/, keyed by the prompt hash, so `python -m triage demo` can replay them
without a model.

    python tools/generate_examples.py --base-url http://127.0.0.1:11535/v1 --model qwen3:8b

Run it under the machine's local-model guard (one local model at a time)."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import time
from email import message_from_bytes
from email.policy import default as default_policy
from pathlib import Path
from urllib.parse import urlsplit

from triage.auth import StaticToken
from triage.job import prepare
from triage.llm import LLMClient
from triage.providers.gmail import GmailProvider
from triage_fakes import FakeGmail, prompt_hash
from triage_fakes.world import CORPUS, ROOT, build_world


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--quantization", default="Q4_K_M")
    p.add_argument("--parameters", default="8.2B")
    p.add_argument("--out", default=str(ROOT / "examples" / "qwen3-8b"))
    a = p.parse_args()
    host = urlsplit(a.base_url).netloc
    world = build_world(lambda m: "", llm_base_url=a.base_url, passthrough=(host,))
    provider = GmailProvider(world.http, FakeGmail.LABEL, StaticToken())
    llm = LLMClient(world.http, a.base_url, a.model, extra_body={"reasoning_effort": "none"})
    names = {message_from_bytes(f.read_bytes(), policy=default_policy)["Message-ID"]: f.name
             for f in CORPUS.glob("*.eml")}
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for mid in provider.list_new():
        msg = provider.fetch(mid)
        name = names[msg.rfc_message_id]
        if not msg.text.strip() and not msg.attachments:
            print(f"{name}: empty, the job does not ask the model")
            continue
        messages, detected, target = prepare(msg)
        t0 = time.monotonic()
        output = llm.complete(messages)
        took = round(time.monotonic() - t0, 1)
        record = {
            "email": f"corpus/{name}",
            "model": {"name": a.model, "parameters": a.parameters, "quantization": a.quantization,
                      "runtime": "Ollama, OpenAI-compatible endpoint, local Mac (Apple M1 Max), thinking off",
                      "temperature": 0},
            "generated_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "detected_language": detected, "target_language": target,
            "calls": [{"prompt_sha256": prompt_hash(messages), "messages": messages,
                       "output": output, "seconds": took}],
        }
        (out / (Path(name).stem + ".json")).write_text(json.dumps(record, indent=1, ensure_ascii=False))
        print(f"{name}: {took}s, {len(output)} chars")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
