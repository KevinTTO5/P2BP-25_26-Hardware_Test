"""Connect progress adapters to subprocess calls without owning install logic.

The step modules still choose every command, privilege seam and install
argument.  These helpers only arrange the pipe or file observation needed by
``progress.follow_apt`` and ``progress.follow_download``.  A duck-typed
Context without ``run_observed`` takes the original direct-call path, which
keeps the small scripted Contexts used by step tests valid.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from . import progress


def apt(
    ctx: Any,
    *args: str,
    runner: Callable[..., Any] | None = None,
    **kwargs: Any,
):
    """Run one apt transaction and consume its machine-readable status fd."""
    invoke = runner or ctx.run_root
    observed = getattr(ctx, "run_observed", None)
    if observed is None:
        return invoke("apt-get", *args, **kwargs)

    read_fd, write_fd = os.pipe()
    try:

        def run():
            try:
                return invoke(
                    "apt-get",
                    *args,
                    *progress.apt_status_fd_args(write_fd),
                    pass_fds=(write_fd,),
                    **kwargs,
                )
            finally:
                os.close(write_fd)

        with os.fdopen(read_fd, encoding="utf-8", errors="replace") as lines:
            return observed(
                run,
                lambda is_running: progress.follow_apt(
                    lines,
                    renderer=ctx.progress,
                ),
            )
    except BaseException:
        try:
            os.close(read_fd)
        except OSError:
            pass
        try:
            os.close(write_fd)
        except OSError:
            pass
        raise


def download(
    ctx: Any,
    path: Any,
    url: str,
    run: Callable[[], Any],
    *,
    task: str | None = None,
    probe_content_length: bool = True,
    known_total: int | None = None,
):
    """Run one fetch while following the destination file's real byte count."""
    observed = getattr(ctx, "run_observed", None)
    if observed is None:
        return run()

    total = known_total
    if total is None and probe_content_length:
        total = progress.content_length(url)
    return observed(
        run,
        lambda is_running: progress.follow_download(
            path,
            total,
            is_running=is_running,
            renderer=ctx.progress,
            task=task,
        ),
    )
