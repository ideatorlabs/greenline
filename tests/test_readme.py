"""The README quickstart is the example file, and it runs."""

from __future__ import annotations

import re
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def python_blocks(text: str) -> list[str]:
    return re.findall(r"```python\n(.*?)```", text, flags=re.S)


def test_readme_quickstart_matches_example_and_runs(capsys: pytest.CaptureFixture[str]) -> None:
    source = (ROOT / "examples" / "quickstart.py").read_text()
    code = source.split('"""', 2)[2].lstrip("\n")  # drop the module docstring
    assert python_blocks((ROOT / "README.md").read_text())[0] == code

    namespace: dict[str, object] = {}
    exec(compile(code, "README.md", "exec"), namespace)
    out = capsys.readouterr().out.splitlines()
    assert out == ["template sent", "[store-1] 15550101: Thanks!", "text sent"]

    runpy.run_path(str(ROOT / "examples" / "quickstart.py"), run_name="__main__")
    assert capsys.readouterr().out.splitlines()[0] == "template sent"


def test_other_readme_snippets_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from greenline.config import Settings
    from greenline.providers import build_provider

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("GREENLINE_VERIFY_TOKEN", "readme-token")
    production, extending = python_blocks((ROOT / "README.md").read_text())[1:3]

    ns: dict[str, object] = {}
    exec(compile(production, "README.md", "exec"), ns)
    client = TestClient(ns["app"])  # type: ignore[arg-type]
    r = client.get(
        "/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "readme-token", "hub.challenge": "ok"},
    )
    assert r.text == "ok" and (tmp_path / "greenline.db").exists()

    exec(compile(extending, "README.md", "exec"), {})
    assert build_provider(Settings(provider="my_bsp")).name == "my_bsp"
