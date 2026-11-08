"""show.py renders transcripts for a human read. Hand-built transcripts only: no model."""
from datetime import datetime

import pytest

from contracts.transcript import Msg, Transcript
from gen.harness import show

SIM = datetime(2026, 11, 8, 21, 30)


def make(tid="t_aaa", tool="stub_leaky", persona="haggler", attack="phantom_refund", texts=None):
    texts = texts or ["I want a refund.", "Sure, here is $50 back."]
    msgs = [Msg(role="customer" if i % 2 == 0 else "bot", text=x, turn=i, at=SIM)
            for i, x in enumerate(texts)]
    return Transcript(transcript_id=tid, business_id="cafe_demo_01", spec_version=1, tool_id=tool,
                      persona_id=persona, attack_class=attack, benign=attack is None, seed=1,
                      sim_time=SIM, attacker_model="fake", bot_model="fake", messages=msgs)


def write(directory, t):
    (directory / f"{t.transcript_id}.json").write_text(t.model_dump_json(indent=1))


def test_render_has_header_then_one_line_per_message():
    lines = show.render(make()).splitlines()
    assert "tool_id=stub_leaky" in lines[0]
    assert "persona_id=haggler" in lines[0]
    assert "attack_class=phantom_refund" in lines[0]
    assert lines[1:] == ["customer: I want a refund.", "bot: Sure, here is $50 back."]


def test_benign_header_says_benign():
    head = show.render(make(attack=None)).splitlines()[0]
    assert "attack_class=none (benign)" in head


def test_multiline_reply_stays_under_its_speaker():
    out = show.render(make(texts=["Hi", "Line one.\nLine two."])).splitlines()
    assert out[2:] == ["bot: Line one.", "     Line two."]


def test_main_prints_one_file(tmp_path, capsys):
    t = make()
    write(tmp_path, t)
    show.main([str(tmp_path / f"{t.transcript_id}.json")])
    assert capsys.readouterr().out.strip() == show.render(t)


def test_main_all_prints_every_file_grouped_by_tool(tmp_path, capsys):
    ts = [make("t_3", tool="stub_local"), make("t_1", tool="stub_leaky", attack=None),
          make("t_2", tool="stub_cautious")]
    for t in ts:
        write(tmp_path, t)
    show.main(["--all", "--dir", str(tmp_path)])
    out = capsys.readouterr().out
    heads = [ln for ln in out.splitlines() if ln.startswith("==")]
    assert len(heads) == 3
    assert [h.split("tool_id=")[1].split()[0] for h in heads] == \
        ["stub_cautious", "stub_leaky", "stub_local"]
    assert "3 transcripts" in out.splitlines()[-1]


def test_main_needs_a_file_or_all():
    with pytest.raises(SystemExit):
        show.main([])
