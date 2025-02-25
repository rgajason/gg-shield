import itertools
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from functools import partial
from typing import Callable, Iterable, Iterator, List, Optional, Set

from click import UsageError
from pygitguardian import GGClient
from pygitguardian.config import MULTI_DOCUMENT_LIMIT

from ggshield.core.cache import Cache
from ggshield.core.config import Config
from ggshield.core.constants import MAX_WORKERS
from ggshield.core.errors import ExitCode, handle_exception
from ggshield.core.git_shell import get_list_commit_SHA, is_git_dir
from ggshield.core.text_utils import create_progress_bar, display_error
from ggshield.core.types import IgnoredMatch
from ggshield.output import OutputHandler
from ggshield.scan import Commit, Results, ScanCollection, ScanContext, SecretScanner


# We add a maximal value to avoid silently consuming all threads on powerful machines
SCAN_THREADS = 4


@contextmanager
def cd(newdir: str) -> Iterator[None]:
    prevdir = os.getcwd()
    os.chdir(os.path.expanduser(newdir))
    try:
        yield
    finally:
        os.chdir(prevdir)


def scan_repo_path(
    client: GGClient,
    cache: Cache,
    output_handler: OutputHandler,
    config: Config,
    scan_context: ScanContext,
    repo_path: str,
) -> int:  # pragma: no cover
    try:
        if not is_git_dir(repo_path):
            raise UsageError(f"{repo_path} is not a git repository")

        with cd(repo_path):
            return scan_commit_range(
                client=client,
                cache=cache,
                commit_list=get_list_commit_SHA("--all"),
                output_handler=output_handler,
                exclusion_regexes=set(),
                matches_ignore=config.secret.ignored_matches,
                scan_context=scan_context,
                ignored_detectors=config.secret.ignored_detectors,
                ignore_known_secrets=config.ignore_known_secrets,
            )
    except Exception as error:
        return handle_exception(error, config.verbose)


def scan_commits_content(
    commits: List[Commit],
    client: GGClient,
    cache: Cache,
    matches_ignore: Iterable[IgnoredMatch],
    scan_context: ScanContext,
    progress_callback: Callable[..., None],
    ignored_detectors: Optional[Set[str]] = None,
    ignore_known_secrets: bool = False,
) -> ScanCollection:  # pragma: no cover
    try:
        commit_files = list(itertools.chain.from_iterable(c.files for c in commits))

        progress_callback(advance=len(commits))
        scanner = SecretScanner(
            client=client,
            cache=cache,
            scan_context=scan_context,
            ignored_matches=matches_ignore,
            ignored_detectors=ignored_detectors,
            ignore_known_secrets=ignore_known_secrets,
        )
        results = scanner.scan(
            commit_files,
            scan_threads=SCAN_THREADS,
        )
    except Exception as exc:
        results = Results.from_exception(exc)

    result_for_files = {result.file: result for result in results.results}
    scans = []
    for commit in commits:
        results_for_commit_files = [
            result_for_files[file] for file in commit.files if file in result_for_files
        ]
        scans.append(
            ScanCollection(
                commit.sha or "unknown",
                type="commit",
                results=Results(
                    results=results_for_commit_files,
                    errors=results.errors,
                ),
                optional_header=commit.optional_header,
                extra_info=commit.info._asdict(),
            )
        )

    return ScanCollection(id=scan_context.command_id, type="commit-ranges", scans=scans)


def get_commits_by_batch(
    commits: Iterable[Commit],
    batch_max_size: int = MULTI_DOCUMENT_LIMIT,
) -> Iterator[List[Commit]]:
    """
    Given a list of commit shas yield the commit files
    by biggest batches possible of length at most MULTI_DOCUMENT_LIMIT
    """
    current_count = 0
    batch = []
    for commit in commits:
        num_files = len(commit.files)
        if current_count + num_files < batch_max_size:
            batch.append(commit)
            current_count += num_files
        else:
            # The first batch can remain empty if it has too many files
            if batch:
                yield batch
            current_count = num_files
            batch = [commit]
    # Send the last batch that remains
    yield batch


def scan_commit_range(
    client: GGClient,
    cache: Cache,
    commit_list: List[str],
    output_handler: OutputHandler,
    exclusion_regexes: Set[re.Pattern],
    matches_ignore: Iterable[IgnoredMatch],
    scan_context: ScanContext,
    ignored_detectors: Optional[Set[str]] = None,
    ignore_known_secrets: bool = False,
) -> ExitCode:
    """
    Scan every commit in a range.

    :param client: Public Scanning API client
    :param commit_list: List of commits sha to scan
    :param verbose: Display successful scan's message
    """

    with create_progress_bar(doc_type="commits") as progress:

        task_scan = progress.add_task(
            "[green]Scanning Commits...", total=len(commit_list)
        )

        commits_batch = get_commits_by_batch(
            commits=(
                Commit(sha=sha, exclusion_regexes=exclusion_regexes)
                for sha in commit_list
            ),
        )
        scans: List[ScanCollection] = []

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = []
            for commits in commits_batch:
                futures.append(
                    executor.submit(
                        scan_commits_content,
                        commits,
                        client,
                        cache,
                        matches_ignore,
                        scan_context,
                        partial(progress.update, task_scan),
                        ignored_detectors,
                        ignore_known_secrets,
                    )
                )

            for future in as_completed(futures):
                scan_collection = future.result()
                for scan in scan_collection.scans_with_results:
                    if scan.results and scan.results.errors:
                        for error in scan.results.errors:
                            # Prefix with `\n` since we are in the middle of a progress bar
                            display_error(f"\n{error.description}")
                    scans.append(scan)

    return_code = output_handler.process_scan(
        ScanCollection(id=scan_context.command_id, type="commit-range", scans=scans)
    )
    return return_code
