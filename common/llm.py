"""The only door to any model. Disk cache keyed on backend+model+prompt+seed+temperature+num_ctx.
Reads cache/demo first, then the role's own cache (cache/llm/<CT_ROLE>). CACHE_ONLY=1 forbids live
calls (CI and the judge-facing demo). Writes are atomic."""
import hashlib
import json
import os
import tempfile
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CacheMiss(RuntimeError):
    pass


def load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


load_dotenv()


def model_for(role: str) -> str:
    """role in {attacker, bot, judge, forge}; set ATTACKER_MODEL etc. in .env."""
    value = os.environ.get(f"{role.upper()}_MODEL")
    if not value:
        raise RuntimeError(f"set {role.upper()}_MODEL in .env (local tags: `ollama list`)")
    return value


def assert_independent_judge() -> None:
    """Necessary, not sufficient: names must differ AND the judge should be a different family."""
    judge = model_for("judge")
    if judge in {model_for("attacker"), model_for("bot")}:
        raise RuntimeError("JUDGE_MODEL must differ from ATTACKER_MODEL and BOT_MODEL")


def _write_dir() -> Path:
    return Path(os.environ.get("CT_CACHE_DIR")
                or ROOT / "cache" / "llm" / os.environ.get("CT_ROLE", "shared"))


def _read_dirs():
    return [Path(os.environ.get("CT_DEMO_DIR") or ROOT / "cache" / "demo"), _write_dir()]


def cache_key(backend: str, model: str, prompt: str, seed: int, temperature: float,
              system: str, num_ctx: int) -> str:
    blob = json.dumps([backend, model, system, prompt, seed, temperature, num_ctx],
                      ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def complete(prompt: str, *, model: str, seed: int = 0, temperature: float = 0.7,
             system: str = "", num_ctx: int = 0) -> str:
    backend = os.environ.get("BACKEND", "gemma")
    num_ctx = num_ctx or int(os.environ.get("NUM_CTX", "8192"))
    name = cache_key(backend, model, prompt, seed, temperature, system, num_ctx) + ".json"
    for d in _read_dirs():
        if (d / name).exists():
            return json.loads((d / name).read_text())["text"]
    if os.environ.get("CACHE_ONLY") == "1":
        raise CacheMiss(f"no cached output for backend={backend} model={model} seed={seed}")
    if backend == "gemma":
        text = _ollama(prompt, model, seed, temperature, system, num_ctx)
    elif backend == "frontier":
        text = _frontier(prompt, model, seed, temperature, system)
    else:
        raise ValueError(f"unknown BACKEND={backend}")
    _atomic_write(_write_dir() / name, {"backend": backend, "model": model, "seed": seed,
                                        "temperature": temperature, "num_ctx": num_ctx,
                                        "text": text})
    return text


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, suffix=".tmp") as f:
        json.dump(payload, f, ensure_ascii=False)
        tmp = f.name
    os.replace(tmp, path)


def _ollama(prompt, model, seed, temperature, system, num_ctx) -> str:
    url = os.environ.get("OLLAMA_URL", "http://localhost:11434") + "/api/generate"
    body = {"model": model, "prompt": prompt, "system": system, "stream": False,
            "options": {"seed": seed, "temperature": temperature, "num_ctx": num_ctx}}
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=float(os.environ.get("LLM_TIMEOUT", "300"))) as r:
        return json.loads(r.read())["response"]


def _frontier(prompt, model, seed, temperature, system) -> str:
    raise NotImplementedError("wire your API client here via a contract-change PR; key from .env")
