"""Command-line arguments of the long-running processes; invalid ones stop before any I/O."""

from types import ModuleType

import pytest

from app.scheduler import scheduler, status
from app.workers import crawler, local_crawler


@pytest.mark.parametrize("value", ["0", "65", "two"])
def test_the_worker_rejects_an_invalid_concurrency(
    value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as stopped:
        crawler.main(["--concurrency", value])

    assert stopped.value.code == 2
    assert "--concurrency" in capsys.readouterr().err


def test_the_local_driver_rejects_an_invalid_concurrency() -> None:
    with pytest.raises(SystemExit) as stopped:
        local_crawler.main(["00000000-0000-0000-0000-000000000000", "--concurrency", "0"])

    assert stopped.value.code == 2


def test_the_scheduler_takes_no_arguments() -> None:
    with pytest.raises(SystemExit) as stopped:
        scheduler.main(["--job", "x"])

    assert stopped.value.code == 2


@pytest.mark.parametrize("module", [crawler, scheduler, status])
def test_help_names_the_command(module: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        module.main(["--help"])

    assert stopped.value.code == 0
    assert "python -m app." in capsys.readouterr().out
