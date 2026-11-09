"""The container health check (Phase 11): which heartbeat is this container's own process's,
and what the command answers. No Redis: reading is replaced where the command is run."""

from collections.abc import Mapping
from typing import Any

import pytest

from app.core.config import Settings
from app.observability import alive
from app.observability.heartbeat import Heartbeat


def beat(name: str, role: str, age: float = 1.0) -> Heartbeat:
    return Heartbeat(name=name, role=role, age_seconds=age, payload={"role": role})


def test_the_own_heartbeat_is_this_hosts_process_of_the_role() -> None:
    heartbeats = [
        beat("crawler-abc-7-0a1b2c3d", "crawler", age=4.0),
        beat("crawler-abc-9-0a1b2c3e", "crawler", age=0.5),  # restarted: the newest counts
        beat("crawler-xyz-7-0a1b2c3d", "crawler"),  # a sibling replica
        beat("indexer-abc-8-0a1b2c3d", "indexer"),
    ]

    found = alive.own_heartbeat(heartbeats, role="crawler", host="abc")

    assert found is not None
    assert found.name == "crawler-abc-9-0a1b2c3e"
    assert alive.own_heartbeat(heartbeats, role="scheduler", host="abc") is None
    assert alive.own_heartbeat(heartbeats, role="crawler", host="other") is None


@pytest.mark.parametrize(
    "name",
    [
        "crawler-abc-def-7-0a1b2c3d",  # the host abc-def, not abc
        "crawler-abc-7-0a1b2c3",  # not a process name
        "crawler-abc-x7-0a1b2c3d",
        "crawler-abc-7-0A1B2C3D",
        "crawler-abc-7-0a1b2c3d-extra",
    ],
)
def test_no_other_name_passes_for_the_hosts_process(name: str) -> None:
    assert alive.own_heartbeat([beat(name, "crawler")], role="crawler", host="abc") is None


def test_roles_with_underscores_match_their_hyphenated_process_names() -> None:
    found = alive.own_heartbeat(
        [beat("index-dispatcher-abc-7-0a1b2c3d", "index_dispatcher")],
        role="index_dispatcher",
        host="abc",
    )
    assert found is not None
    # The name alone is not enough: the heartbeat's own role must agree.
    assert (
        alive.own_heartbeat([beat("crawler-abc-7-0a1b2c3d", "indexer")], role="crawler", host="abc")
        is None
    )


def run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    found: Heartbeat | Exception | None,
    role: str = "crawler",
) -> tuple[int, str, str, Mapping[str, Any]]:
    seen: dict[str, Any] = {}

    async def fake(settings: Settings, *, role: str, host: str) -> Heartbeat | None:
        seen.update(role=role, host=host)
        if isinstance(found, Exception):
            raise found
        return found

    monkeypatch.setattr(alive, "find_own_heartbeat", fake)
    code = alive.main([role], settings=Settings(_env_file=None), host="abc")
    out, err = capsys.readouterr()
    return code, out, err, seen


def test_the_command_is_healthy_with_a_live_heartbeat(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _, seen = run(monkeypatch, capsys, beat("crawler-abc-7-0a1b2c3d", "crawler", 2.0))
    assert code == 0
    assert out == "healthy: crawler-abc-7-0a1b2c3d, last heartbeat 2.0 s ago\n"
    assert seen == {"role": "crawler", "host": "abc"}


def test_the_command_is_unhealthy_without_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err, _ = run(monkeypatch, capsys, None, role="index_dispatcher")
    assert code == 1
    assert err == "unhealthy: no live index_dispatcher heartbeat from this host\n"


def test_the_command_is_unhealthy_when_redis_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err, _ = run(monkeypatch, capsys, ConnectionRefusedError("refused"))
    assert code == 1
    assert err == "unhealthy: cannot read the heartbeats (ConnectionRefusedError)\n"


def test_the_command_refuses_an_unknown_role(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        alive.main(["worker"], settings=Settings(_env_file=None), host="abc")
    assert stopped.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
