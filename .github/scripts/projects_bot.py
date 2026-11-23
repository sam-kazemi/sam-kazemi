"""Profile README project bot.

propose: find public repos that aren't in README.md yet and open an issue asking
         whether to add each one (with a suggested one-line description).
respond: when the owner replies "yes" / "yes: my own text" / "no" on that issue,
         update README.md or skip, then close the issue.

Uses only the Python standard library. Runs in GitHub Actions
(.github/workflows/project-proposals.yml).
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "")          # e.g. sam-kazemi/sam-kazemi
OWNER = REPO.split("/")[0] if REPO else ""
LABEL = "project-proposal"
START, END = "<!-- PROJECTS:START -->", "<!-- PROJECTS:END -->"
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL") or "gemini-3.1-flash-lite"


# ------------------------------------------------------------------ GitHub API
def api(method: str, path: str, data: dict | None = None, raw: bool = False):
    req = urllib.request.Request(API + path, method=method,
                                 data=json.dumps(data).encode() if data is not None else None)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Accept", "application/vnd.github.raw" if raw else "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode()
    return body if raw else (json.loads(body) if body else None)


def get_readme() -> tuple[str, str]:
    f = api("GET", f"/repos/{REPO}/contents/README.md")
    return base64.b64decode(f["content"]).decode(), f["sha"]


def put_readme(text: str, sha: str, message: str) -> None:
    api("PUT", f"/repos/{REPO}/contents/README.md",
        {"message": message, "content": base64.b64encode(text.encode()).decode(), "sha": sha})


def comment_and_close(number: int, text: str) -> None:
    api("POST", f"/repos/{REPO}/issues/{number}/comments", {"body": text})
    api("PATCH", f"/repos/{REPO}/issues/{number}", {"state": "closed"})


# ------------------------------------------------------------- README editing
def entry_line(name: str, url: str, description: str, homepage: str = "") -> str:
    line = f"- **[{name}]({url})** — {description.strip()}"
    if homepage:
        line += f" [Live demo →]({homepage})"
    return line


def insert_entry(readme: str, line: str) -> str:
    """Put the new project at the top of the list between the markers (newest first)."""
    if START not in readme or END not in readme:
        raise SystemExit(f"README.md needs the markers {START} and {END} around the project list.")
    return readme.replace(START, START + "\n" + line, 1)


# ------------------------------------------------------ suggested description
def suggest_description(repo: dict, readme_style: str) -> str:
    fallback = (repo.get("description") or "").strip() or "(add a one-line description)"
    if not GEMINI_KEY:
        return fallback
    try:
        repo_readme = api("GET", f"/repos/{OWNER}/{repo['name']}/readme", raw=True)[:4000]
    except urllib.error.HTTPError:
        repo_readme = ""
    examples = "\n".join(l for l in readme_style.splitlines() if l.startswith("- **["))[:1500]
    prompt = (
        "Write ONE line (max 35 words) describing this GitHub project for the owner's profile README. "
        "Match the tone of the example lines. Use only facts stated in the repo description or README; "
        "do not invent numbers or claims. Output only the description text, no name, no link, no bullet.\n\n"
        f"Example lines:\n{examples}\n\nRepo name: {repo['name']}\n"
        f"Repo description: {repo.get('description') or ''}\n\nRepo README:\n{repo_readme}"
    )
    try:
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
            data=json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("x-goog-api-key", GEMINI_KEY)
        with urllib.request.urlopen(req, timeout=60) as r:
            text = json.loads(r.read())["candidates"][0]["content"]["parts"][0]["text"]
        text = " ".join(text.split()).strip("-• ")
        return text if 0 < len(text) < 400 else fallback
    except Exception as e:  # free tier can be busy; never fail the run over a description
        print(f"Gemini suggestion failed ({e}); using the repo description.")
        return fallback


# ---------------------------------------------------------------------- modes
def propose() -> None:
    readme, _ = get_readme()
    repos = api("GET", f"/users/{OWNER}/repos?type=owner&per_page=100&sort=created")
    asked = set()
    for issue in api("GET", f"/repos/{REPO}/issues?labels={LABEL}&state=all&per_page=100"):
        m = re.search(r"<!-- repo: (.+?) -->", issue.get("body") or "")
        if m:
            asked.add(m.group(1).lower())
    try:
        api("POST", f"/repos/{REPO}/labels", {"name": LABEL, "color": "2b5cc4"})
    except urllib.error.HTTPError:
        pass  # label already exists

    profile_name = REPO.split("/")[1].lower()
    for repo in repos:
        name = repo["name"]
        if (repo.get("fork") or repo.get("archived") or repo.get("private")
                or name.lower() == profile_name or name.lower() in asked
                or f"github.com/{OWNER}/{name}".lower() in readme.lower()):
            continue
        line = entry_line(name, repo["html_url"], suggest_description(repo, readme), repo.get("homepage") or "")
        body = (
            f"I found a public repo that isn't on your profile yet: **[{name}]({repo['html_url']})**\n\n"
            f"Suggested line for your README:\n\n{line}\n\n"
            "**Reply to this issue with:**\n"
            "- `yes` to add it as written above\n"
            "- `yes: your own description` to add it with your wording\n"
            "- `no` to skip it (I won't ask about this repo again)\n\n"
            f"<!-- repo: {name} -->\n<!-- url: {repo['html_url']} -->\n"
            f"<!-- homepage: {repo.get('homepage') or ''} -->\n<!-- line: {line} -->"
        )
        api("POST", f"/repos/{REPO}/issues",
            {"title": f"Add {name} to your profile README?", "body": body, "labels": [LABEL]})
        print(f"Asked about {name}")


def respond() -> None:
    event = json.load(open(os.environ["GITHUB_EVENT_PATH"]))
    issue, comment = event["issue"], event["comment"]
    labels = {l["name"] for l in issue.get("labels", [])}
    if LABEL not in labels or issue["state"] != "open" or comment["user"]["login"].lower() != OWNER.lower():
        return
    body = issue.get("body") or ""
    get = lambda key: (re.search(rf"<!-- {key}: (.*?) -->", body) or [None, ""])[1]
    name, url, homepage, line = get("repo"), get("url"), get("homepage"), get("line")
    reply = comment["body"].strip()
    low = reply.lower()

    if low.startswith("no"):
        comment_and_close(issue["number"], f"Skipped. I won't ask about **{name}** again.")
    elif low.startswith("yes"):
        custom = reply.split(":", 1)[1].strip() if ":" in reply else ""
        if custom:
            line = entry_line(name, url, custom, homepage)
        readme, sha = get_readme()
        if url.lower() in readme.lower():
            comment_and_close(issue["number"], f"**{name}** is already in your README, so nothing changed.")
            return
        put_readme(insert_entry(readme, line), sha, f"Add {name} to projects")
        comment_and_close(issue["number"], f"Added to your profile README ✅\n\n{line}")
    else:
        api("POST", f"/repos/{REPO}/issues/{issue['number']}/comments",
            {"body": "I didn't catch that. Reply `yes`, `yes: your own description`, or `no`."})


if __name__ == "__main__":
    {"propose": propose, "respond": respond}[sys.argv[1]]()
