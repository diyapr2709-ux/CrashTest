"""Prints transcripts for a human read: a header line, then "customer: ..." / "bot: ..." lines.
    python -m gen.harness.show data/dev/transcripts/t_xxx.json
    python -m gen.harness.show --all [--dir data/dev/transcripts]
--all groups by tool, then attack class (benign last), then persona."""
import argparse
from pathlib import Path
from typing import List, Optional

from contracts.transcript import Transcript

TRANSCRIPT_DIR = Path("data/dev/transcripts")


def header(t: Transcript) -> str:
    attack = t.attack_class or "none (benign)"
    return (f"== {t.transcript_id}  tool_id={t.tool_id}  persona_id={t.persona_id}  "
            f"attack_class={attack}  seed={t.seed}")


def render(t: Transcript) -> str:
    lines = [header(t)]
    for m in t.messages:
        prefix = f"{m.role}: "
        lines.append(prefix + m.text.replace("\n", "\n" + " " * len(prefix)))
    return "\n".join(lines)


def load_dir(directory: Path) -> List[Transcript]:
    ts = [Transcript.model_validate_json(p.read_text()) for p in directory.glob("*.json")]
    return sorted(ts, key=lambda t: (t.tool_id, t.benign, t.attack_class or "", t.persona_id,
                                     t.transcript_id))


def main(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="print transcripts as customer/bot lines")
    ap.add_argument("file", type=Path, nargs="?")
    ap.add_argument("--all", action="store_true", help="print every file in --dir")
    ap.add_argument("--dir", type=Path, default=TRANSCRIPT_DIR)
    args = ap.parse_args(argv)
    if args.all == (args.file is not None):
        ap.error("pass one transcript file or --all")
    if args.file:
        print(render(Transcript.model_validate_json(args.file.read_text())))
        return
    ts = load_dir(args.dir)
    print("\n\n".join(render(t) for t in ts))
    print(f"\n{len(ts)} transcripts in {args.dir}")


if __name__ == "__main__":
    main()
