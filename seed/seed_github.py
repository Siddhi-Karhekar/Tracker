"""Push the demo scenario into a real (empty) GitHub repo.

    python -m seed.seed_github --dry-run
    python -m seed.seed_github --repo you/teampulse-demo --as siddhi=Siddhi-Karhekar,aarav=<login>,...

What you get: labels, milestones (with the scenario's due dates), issues #1–#12 with
assignees and comments, PRs #13–#19 with commits (author names and dates from the
scenario), reviews, merges, the reopened login issue, and the direct pushes.

What GitHub won't let us fake: issue/PR timestamps are "now", and PRs/reviews are created
by whoever owns the token. Set GITHUB_TOKEN__<HANDLE> per teammate in .env to make each
person's PRs and reviews come from their own account; otherwise they all come from you and
the scenario's author is written into the PR body. Evaluation therefore uses the offline
files in data/demo; this script is for showing live ingestion working.
"""
from __future__ import annotations

import argparse
import time

from seed import scenario as S
from seed.build import ist
from seed.live_common import parse_map, save_run
from teampulse.config import env


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=env("GITHUB_REPO"))
    ap.add_argument("--as", dest="logins", help="demo handle → real GitHub login, e.g. siddhi=Siddhi-Karhekar,...")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="seed even if the repo already has issues")
    a = ap.parse_args()
    logins = parse_map(a.logins)

    plan = []
    plan += [f"label {lb}" for lb in sorted({lb for i in S.ISSUES for lb in i["labels"]})]
    plan += [f"milestone {t} due {d}" for t, d in S.MILESTONES.items()]
    plan += [f"issue #{i['number']} {i['title']} → {logins.get(i['assignee'], '(' + i['assignee'] + ')')}"
             for i in S.ISSUES]
    plan += [f"PR #{p['number']} {p['title']} by {p['author']} ({len(p['commits'])} commits, "
             f"{'merge' if p['merged'] else 'leave open'})" for p in S.PULLS]
    plan += ["reopen issue #1", f"{len(S.DIRECT_COMMITS)} direct commits to main"]
    if a.dry_run or not a.repo:
        print("\n".join(plan))
        if not a.repo:
            print("\n(no --repo / GITHUB_REPO set: dry run only)")
        return

    from github import Auth, Github, GithubException, InputGitAuthor

    main_gh = Github(auth=Auth.Token(env("GITHUB_TOKEN")))
    repo = main_gh.get_repo(a.repo)
    member_repo = {}
    for h in S.MEMBERS:
        tok = env(f"GITHUB_TOKEN__{h.upper()}")
        member_repo[h] = Github(auth=Auth.Token(tok)).get_repo(a.repo) if tok else None

    def as_member(h):
        return member_repo.get(h) or repo

    if repo.get_issues(state="all").totalCount and not a.force:
        raise SystemExit("Repo already has issues/PRs; numbers won't match the scenario. Use a fresh repo or --force.")

    def git_author(h, ts):
        m = S.MEMBERS[h]
        login = logins.get(h, m["github"])
        return InputGitAuthor(m["display_name"], f"{login}@users.noreply.github.com",
                              ist(ts).strftime("%Y-%m-%dT%H:%M:%S+05:30"))

    # main branch must exist
    try:
        repo.get_branch(repo.default_branch)
    except GithubException:
        repo.create_file("README.md", "init", "# CampusEats (TeamPulse demo data)\n")
    base = repo.default_branch

    for lb in sorted({lb for i in S.ISSUES for lb in i["labels"]}):
        try:
            repo.create_label(lb, "c5def5")
        except GithubException:
            pass
    milestones = {}
    existing = {m.title: m for m in repo.get_milestones(state="all")}
    from datetime import datetime
    for title, due in S.MILESTONES.items():
        milestones[title] = existing.get(title) or repo.create_milestone(
            title, due_on=datetime.fromisoformat(due + "T12:00:00+05:30"))

    num = {}
    for it in S.ISSUES:
        assignee = logins.get(it["assignee"])
        body = it["body"] + ("" if assignee else f"\n\n_Demo assignee: {S.MEMBERS[it['assignee']]['github']}_")
        kw = dict(title=it["title"], body=body, labels=it["labels"])
        if assignee:
            kw["assignees"] = [assignee]
        if it["milestone"]:
            kw["milestone"] = milestones[it["milestone"]]
        issue = repo.create_issue(**kw)
        num[it["number"]] = issue.number
        for ts, who, text in it["comments"]:
            as_member(who).get_issue(issue.number).create_comment(text)
        print("issue", it["number"], "→", issue.html_url)
        time.sleep(1)

    for pr in S.PULLS:
        sha = repo.get_branch(base).commit.sha
        repo.create_git_ref(f"refs/heads/{pr['branch']}", sha)
        for i, (ts, msg) in enumerate(pr["commits"]):
            path = f"src/{pr['branch'].replace('/', '_')}/step{i + 1}.md"
            repo.create_file(path, msg, f"# {msg}\n", branch=pr["branch"],
                             author=git_author(pr["author"], ts), committer=git_author(pr["author"], ts))
        closes = " ".join(f"Closes #{num[i]}." for i in pr["closes"])
        body = f"{closes}\n\n{pr['title']}." + (
            "" if member_repo.get(pr["author"]) else f"\n\n_Demo author: {S.MEMBERS[pr['author']]['github']}_")
        p = as_member(pr["author"]).create_pull(title=pr["title"], body=body, head=pr["branch"], base=base,
                                                draft=pr.get("draft", False))
        for ts, who, state, text in pr["reviews"]:
            r = member_repo.get(who)
            if r and who != pr["author"]:
                event = {"APPROVED": "APPROVE", "COMMENTED": "COMMENT"}.get(state, "COMMENT")
                r.get_pull(p.number).create_review(body=text or None, event=event)
            else:
                p.create_issue_comment(f"[demo review by {S.MEMBERS[who]['github']}: {state}] {text}".strip())
        if pr["merged"]:
            as_member(pr["merged_by"]).get_pull(p.number).merge(merge_method="merge")  # keep per-author commits
        print("PR", pr["number"], "→", p.html_url)
        time.sleep(1)

    for it in S.ISSUES:  # issue #1: reopened after the merge closed it
        if it["events"] and it["events"][-1][2] == "reopened":
            as_member(it["events"][-1][1]).get_issue(num[it["number"]]).edit(state="open")

    for ts, who, msg in S.DIRECT_COMMITS:
        msg_real = msg
        for old, new in num.items():
            msg_real = msg_real.replace(f"(#{old})", f"(#{new})")
        repo.create_file(f"db/{ts[:10]}-{who}.md", msg_real, f"# {msg_real}\n", branch=base,
                         author=git_author(who, ts), committer=git_author(who, ts))

    path = save_run("github", {"repo": a.repo, "issue_numbers": num, "logins": logins})
    print(f"Done. Mapping saved to {path}. Ingest with: python -m teampulse.ingest.run github")


if __name__ == "__main__":
    main()
