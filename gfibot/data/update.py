import re
import logging
import argparse

from typing import List, Dict, Any
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pymongo.errors import WriteError

from .. import CONFIG, TOKENS, Database
from .rest import RepoFetcher, logger as rest_logger


logger = logging.getLogger(__name__)


def count_by_month(dates: List[datetime]) -> List[Dict[str, Any]]:
    counts = Counter(map(lambda d: (d.year, d.month), dates))
    return sorted(
        [
            {"month": datetime(year=y, month=m, day=1, tzinfo=timezone.utc), "count": c}
            for (y, m), c in counts.items()
        ],
        key=lambda k: k["month"],
    )


def match_issue_numbers(text: str) -> List[int]:
    """
    Match close issue text in a pull request, as documented in:
    https://docs.github.com/en/issues/tracking-your-work-with-issues/linking-a-pull-request-to-an-issue
    """
    numbers = []
    regex = r"(close[sd]?|fix(es|ed)?|resolve[sd]?) \#(\d+)"
    for _, _, number in re.findall(regex, text.lower()):
        numbers.append(int(number))
    return numbers


def update_repo_stat(fetcher: RepoFetcher) -> Dict[str, Any]:
    logger.info("Updating repo: %s/%s", fetcher.owner, fetcher.name)
    with Database() as db:
        repo = db.repos.find_one({"owner": fetcher.owner, "name": fetcher.name})
    if repo is None:
        logger.info("Repo not found in database, creating...")
        repo = {
            "created_at": datetime.now(timezone.utc),
            "updated_at": None,
            "owner": fetcher.owner,
            "name": fetcher.name,
            "language": None,
            "repo_created_at": None,
            "monthly_stars": [],
            "monthly_commits": [],
            "monthly_issues": [],
        }

    for k, v in fetcher.get_stats().items():
        repo[k] = v
    logger.info("Repo stats updated, rate remaining %s", fetcher.get_rate_limit())
    return repo


def update_stars(fetcher: RepoFetcher, since: datetime) -> List[Dict[str, Any]]:
    stars = fetcher.get_stars(since)
    logger.info(
        "%d stars updated, rate remaining %s", len(stars), fetcher.get_rate_limit()
    )
    with Database() as db:
        for star in stars:
            db.repos.stars.replace_one(
                {"owner": fetcher.owner, "name": fetcher.name, "user": star["user"]},
                star,
                upsert=True,
            )
    return stars


def update_commits(fetcher: RepoFetcher, since: datetime) -> List[Dict[str, Any]]:
    commits = fetcher.get_commits(since)
    logger.info(
        "%d commits updated, rate remaining %s",
        len(commits),
        fetcher.get_rate_limit(),
    )
    with Database() as db:
        for commit in commits:
            db.repos.commits.replace_one(
                {
                    "owner": fetcher.owner,
                    "name": fetcher.name,
                    "sha": commit["sha"],
                },
                commit,
                upsert=True,
            )
    return commits


def update_issues(fetcher: RepoFetcher, since: datetime) -> List[Dict[str, Any]]:
    issues = fetcher.get_issues(since)
    logger.info(
        "%d issues updated, rate remaining %s",
        len(issues),
        fetcher.get_rate_limit(),
    )
    with Database() as db:
        for issue in issues:
            db.repos.issues.replace_one(
                {
                    "owner": fetcher.owner,
                    "name": fetcher.name,
                    "number": issue["number"],
                },
                issue,
                upsert=True,
            )
    return issues


def locate_resolved_issues(
    fetcher: RepoFetcher, since: datetime
) -> List[Dict[str, Any]]:
    with Database() as db:
        all_issues = list(
            db.repos.issues.find(
                {
                    "owner": fetcher.owner,
                    "name": fetcher.name,
                    "is_pull": False,
                    "closed_at": {"$gte": since},
                }
            )
        )
        all_commits = sorted(
            db.repos.commits.find({"owner": fetcher.owner, "name": fetcher.name}),
            key=lambda c: c["authored_at"],
        )
    closed_nums = set(map(lambda i: i["number"], all_issues))
    logger.info("%d newly closed issues since %s", len(all_issues), since)

    resolved = defaultdict(
        lambda: {
            "owner": fetcher.owner,
            "name": fetcher.name,
            "number": None,
            "resolver": None,
            "resolver_commit_num": None,
        }
    )

    # Note that, we first get possible issue resolver *using commits*,
    #   then we get possible resolver using PRs.
    # In this way, resolver obtained through PRs has higher priority.
    # Also, all commits and issues are sorted by date, so resolver from later commits
    #   and later PRs have higher priority.
    author2commits = defaultdict(list)
    for c in all_commits:
        author2commits[c["author"]].append(c)
    for c in all_commits:
        if c["author"] is None:
            continue
        commits_before = set()
        for c2 in author2commits[c["author"]]:
            if c2["authored_at"] < c["authored_at"]:
                commits_before.add(c2["sha"])
        for num in match_issue_numbers(c["message"]):
            if num not in closed_nums:
                continue
            logger.debug(
                "Issue #%d resolved in %s by %s (%d prior commits)",
                num,
                c["sha"],
                c["author"],
                len(commits_before),
            )
            resolved[num]["number"] = num
            resolved[num]["resolver"] = c["author"]
            resolved[num]["resolved_in"] = c["sha"]
            resolved[num]["resolver_commit_num"] = len(commits_before)
    logger.info("%d issues found to be resolved by commits", len(resolved))

    for issue in all_issues:
        t1 = issue["closed_at"] - timedelta(minutes=1)
        t2 = issue["closed_at"] + timedelta(minutes=1)
        with Database() as db:
            prs = list(
                db.repos.issues.find(
                    {
                        "owner": fetcher.owner,
                        "name": fetcher.name,
                        "is_pull": True,
                        "merged_at": {"$gt": t1, "$lt": t2},
                    }
                )
            )
        if len(prs) > 0:
            logger.debug(
                "Candidate PRs %s for issue %d, rate remaining = %s",
                [pr["number"] for pr in prs],
                issue["number"],
                fetcher.get_rate_limit(),
            )
        for pr in prs:
            pr_details = fetcher.get_pull_detail(pr["number"])
            text = [pr["title"], pr["body"], *pr_details["comments"]]
            text = "\n".join([t for t in text if t is not None])
            commits_before = set()
            for c in author2commits[pr["user"]]:
                if (
                    c["authored_at"] < pr["merged_at"]
                    and c["sha"] not in pr_details["commits"]
                ):
                    commits_before.add(c["sha"])
            if issue["number"] in match_issue_numbers(text):
                logger.debug(
                    "Issue #%d resolved in #%d by %s (%d prior commits)",
                    issue["number"],
                    pr["number"],
                    pr["user"],
                    len(commits_before),
                )
                resolved[issue["number"]]["number"] = issue["number"]
                resolved[issue["number"]]["resolver"] = pr["user"]
                resolved[issue["number"]]["resolved_in"] = pr["number"]
                resolved[issue["number"]]["resolver_commit_num"] = len(commits_before)
    logger.info("%d issues found to be resolved by commits/PRs", len(resolved))
    return list(resolved.values())


def update_resolved_issues(
    fetcher: RepoFetcher, since: datetime
) -> List[Dict[str, Any]]:
    """Fetch data for issues that will be used for RecGFI training."""
    resolved_issues = locate_resolved_issues(fetcher, since)
    for resolved in resolved_issues:
        logger.debug(
            "Fetching details for issue #%d, rate remaining = %s",
            resolved["number"],
            fetcher.get_rate_limit(),
        )
        resolved["events"] = fetcher.get_issue_detail(resolved["number"])["events"]
    with Database() as db:
        for issue in resolved_issues:
            try:
                db.issues.replace_one(
                    {
                        "owner": fetcher.owner,
                        "name": fetcher.name,
                        "number": issue["number"],
                    },
                    issue,
                    upsert=True,
                )
            except WriteError as ex:
                logger.error("Failed to insert document due to %s: %s", ex, issue)
    return resolved_issues


def update_user(user: str, since: datetime) -> None:
    raise NotImplementedError()


def update_repo(token: str, owner: str, name: str) -> None:
    fetcher = RepoFetcher(token, owner, name)
    repo = update_repo_stat(fetcher)

    if repo["updated_at"] is None:
        since = repo["repo_created_at"]
    else:
        since = repo["updated_at"]
    repo["updated_at"] = datetime.now(timezone.utc)

    logger.info("Update stars, commits, and issues since %s", since)
    stars = update_stars(fetcher, since)
    commits = update_commits(fetcher, since)
    issues = update_issues(fetcher, since)

    repo["monthly_stars"] = count_by_month([s["starred_at"] for s in stars])
    repo["monthly_commits"] = count_by_month([c["committed_at"] for c in commits])
    repo["monthly_issues"] = count_by_month(
        [i["created_at"] for i in issues if not i["is_pull"]]
    )
    repo["monthly_pulls"] = count_by_month(
        [i["created_at"] for i in issues if i["is_pull"]]
    )

    resolved_issues = update_resolved_issues(fetcher, since)

    with Database() as db:
        db.repos.replace_one(
            {"owner": fetcher.owner, "name": fetcher.name}, repo, upsert=True
        )

    all_users = set([owner])
    for commit in commits:
        all_users.add(commit["author"])
    for issue in issues:
        all_users.add(issue["user"])
    for resolved in resolved_issues:
        all_users.add(resolved["resolver"])
        for event in resolved["events"]:
            all_users.add(event["actor"])
            if "assignee" in event:
                all_users.add(event["assignee"])
            if "commenter" in event:
                all_users.add(event["commenter"])
    logger.info("%d users associated with %s/%s", len(all_users), owner, name)

    # for user in all_users:
    # update_user(user, since)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    if args.debug:
        logger.setLevel(logging.DEBUG)
        rest_logger.setLevel(logging.DEBUG)

    for i, project in enumerate(CONFIG["gfibot"]["projects"]):
        owner, name = project.split("/")
        update_repo(TOKENS[i % len(TOKENS)], owner, name)

    logger.info("Done!")
