"""Command-line arguments of the long-running processes; invalid ones stop before any I/O."""

from types import ModuleType

import pytest

from app.core.config import Settings
from app.scheduler import index_dispatcher, scheduler, search, status
from app.workers import crawler, indexer, local_crawler


@pytest.mark.parametrize("module", [crawler, indexer])
@pytest.mark.parametrize("value", ["0", "65", "two"])
def test_the_workers_reject_an_invalid_concurrency(
    module: ModuleType, value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as stopped:
        module.main(["--concurrency", value])

    assert stopped.value.code == 2
    assert "--concurrency" in capsys.readouterr().err


def test_the_local_driver_rejects_an_invalid_concurrency() -> None:
    with pytest.raises(SystemExit) as stopped:
        local_crawler.main(["00000000-0000-0000-0000-000000000000", "--concurrency", "0"])

    assert stopped.value.code == 2


@pytest.mark.parametrize("module", [scheduler, index_dispatcher])
def test_the_dispatching_processes_take_no_arguments(module: ModuleType) -> None:
    with pytest.raises(SystemExit) as stopped:
        module.main(["--job", "x"])

    assert stopped.value.code == 2


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["not-a-uuid", "query"],
        ["00000000-0000-0000-0000-000000000000"],  # no query
        ["00000000-0000-0000-0000-000000000000", "q", "--limit", "0"],
        ["00000000-0000-0000-0000-000000000000", "q", "--limit", "51"],
        ["00000000-0000-0000-0000-000000000000", "q", "--offset", "-1"],
        ["00000000-0000-0000-0000-000000000000", "q", "--offset", "1001"],
    ],
)
def test_the_search_command_rejects_invalid_arguments(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        search.main(arguments)

    assert stopped.value.code == 2


@pytest.mark.parametrize("module", [crawler, scheduler, status, indexer, index_dispatcher, search])
def test_help_names_the_command(module: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        module.main(["--help"])

    assert stopped.value.code == 0
    assert "python -m app." in capsys.readouterr().out


def test_an_indexer_refuses_more_tasks_than_its_connection_pool_holds(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as stopped:
        indexer.main(["--concurrency", "11"], settings=Settings(_env_file=None))  # pool: 10

    assert stopped.value.code == 2
    assert "connection pool" in capsys.readouterr().err
