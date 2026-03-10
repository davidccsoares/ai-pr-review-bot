import os
import requests
import base64
import difflib
import urllib.parse
from datetime import datetime, timezone
import json

MAX_DIFF_SIZE = 12000
OPENROUTER_MODEL = "stepfun/step-3.5-flash:free"
LAST_RUN_FILE = "last_run.json"

AZURE_ORG = os.environ["AZURE_ORG"]
AZURE_PROJECT = os.environ["AZURE_PROJECT"]
AZURE_TOKEN = os.environ["AZURE_TOKEN"]
OPENROUTER_KEY = os.environ["OPENROUTER_KEY"]

auth = base64.b64encode(f":{AZURE_TOKEN}".encode()).decode()
HEADERS = {"Authorization": f"Basic {auth}", "Content-Type": "application/json"}

# -----------------------
# Auxiliares de timestamp
# -----------------------
def read_last_run():
    if os.path.exists(LAST_RUN_FILE):
        try:
            return json.load(open(LAST_RUN_FILE, "r"))
        except:
            return {}
    return {}

def write_last_run(data):
    with open(LAST_RUN_FILE, "w") as f:
        json.dump(data, f)

# -----------------------
# Azure DevOps helpers
# -----------------------
def list_repos(project):
    url = f"{AZURE_ORG}/{project}/_apis/git/repositories?api-version=7.0"
    r = requests.get(url, headers=HEADERS)
    r.raise_for_status()
    return r.json().get("value", [])

def list_recent_prs(project, repo_id, last_run_repo):
    url = f"{AZURE_ORG}/{project}/_apis/git/repositories/{repo_id}/pullrequests?searchCriteria.status=active&api-version=7.0"
    r = requests.get(url, headers=HEADERS)
    r.raise_for_status()
    prs = r.json().get("value", [])
    recent_prs = []

    for pr in prs:
        created = datetime.fromisoformat(pr["creationDate"].replace("Z", "+00:00"))
        updated_str = pr.get("lastMergeSourceCommit", {}).get("committer", {}).get("date", pr["creationDate"])
        updated = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
        if last_run_repo is None or created > last_run_repo or updated > last_run_repo:
            recent_prs.append(pr)
    return recent_prs

def get_pr_changes(pr_id, repo_id, project):
    iter_url = f"{AZURE_ORG}/{project}/_apis/git/repositories/{repo_id}/pullRequests/{pr_id}/iterations?api-version=7.0"
    r = requests.get(iter_url, headers=HEADERS)
    r.raise_for_status()
    iterations = r.json()["value"]
    latest = iterations[-1]
    latest_iteration_id = latest["id"]
    source_commit = latest.get("targetRefCommit", {}).get("commitId")
    target_commit = latest.get("sourceRefCommit", {}).get("commitId")
    changes_url = f"{AZURE_ORG}/{project}/_apis/git/repositories/{repo_id}/pullRequests/{pr_id}/iterations/{latest_iteration_id}/changes?api-version=7.0"
    r = requests.get(changes_url, headers=HEADERS)
    r.raise_for_status()
    return r.json(), source_commit, target_commit

def get_file_content(repo_id, project, path, commit_id):
    if not commit_id or commit_id == "0000000000000000000000000000000000000000":
        return []
    safe_path = urllib.parse.quote(path)
    url = (
        f"{AZURE_ORG}/{project}/_apis/git/repositories/{repo_id}/items"
        f"?path={safe_path}"
        f"&versionDescriptor.version={commit_id}"
        f"&versionDescriptor.versionType=commit"
        f"&includeContent=true"
        f"&api-version=7.0"
    )
    r = requests.get(url, headers=HEADERS)
    if r.status_code == 200:
        return r.text.splitlines()
    else:
        print(f"⚠ Não foi possível buscar {path} no commit {commit_id[:8]}: HTTP {r.status_code}")
        return []

# -----------------------
# LLM + comentários
# -----------------------
def ask_llm(diff_text):
    prompt = f"""
You are a senior software engineer. Review the following code changes.
Identify bugs, security issues, and performance bottlenecks.
Format: [file]:[line] - description

Diff:
{diff_text}
"""
    try:
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_KEY}"},
            json={
                "model": OPENROUTER_MODEL,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=60
        )
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"Erro ao chamar LLM: {e}"

def comment_pr_inline(project, repo_id, pr_id, comments):
    url = f"{AZURE_ORG}/{project}/_apis/git/repositories/{repo_id}/pullRequests/{pr_id}/threads?api-version=7.0"
    posted = set()
    for c in comments.splitlines():
        if ":" not in c or "-" not in c:
            continue
        try:
            parts = c.split("-", 1)
            meta = parts[0].strip().split(":")
            file_path = meta[0].strip()
            line_num = int(meta[1].strip())
            content = parts[1].strip()
            key = f"{file_path}:{line_num}:{content}"
            if key in posted:
                continue
            posted.add(key)
            payload = {
                "comments": [{"parentCommentId": 0, "content": content, "commentType": 1}],
                "status": 1,
                "threadContext": {
                    "filePath": file_path if file_path.startswith("/") else f"/{file_path}",
                    "rightFileStart": {"line": line_num, "offset": 1},
                    "rightFileEnd": {"line": line_num, "offset": 1}
                }
            }
            r = requests.post(url, headers=HEADERS, json=payload)
            if r.status_code >= 400:
                print(f"⚠ Falha comentário {file_path}:{line_num}: HTTP {r.status_code} - {r.text[:200]}")
        except Exception as e:
            print(f"⚠ Ignorando linha inválida: {e}")
            continue

def comment_pr_summary(project, repo_id, pr_id, summary_text):
    url = f"{AZURE_ORG}/{project}/_apis/git/repositories/{repo_id}/pullRequests/{pr_id}/threads?api-version=7.0"
    payload = {
        "comments": [{"parentCommentId": 0, "content": f"🤖 **AI Review Summary**\n\n{summary_text}", "commentType": 1}],
        "status": 1
    }
    r = requests.post(url, headers=HEADERS, json=payload)
    if r.status_code >= 400:
        print(f"⚠ Falha comentário resumo: HTTP {r.status_code} - {r.text[:200]}")

# -----------------------
# Função principal
# -----------------------
def run_review_for_project():
    last_run_data = read_last_run()
    repos = list_repos(AZURE_PROJECT)
    if not repos:
        print(f"Nenhum repositório encontrado em {AZURE_PROJECT}")
        return

    for repo in repos:
        repo_id = repo["id"]
        repo_name = repo["name"]
        last_run_repo = None
        if AZURE_PROJECT in last_run_data:
            last_run_repo = datetime.fromisoformat(last_run_data[AZURE_PROJECT].get(repo_id, "1970-01-01T00:00:00+00:00"))

        prs = list_recent_prs(AZURE_PROJECT, repo_id, last_run_repo)
        if not prs:
            print(f"Nenhum PR novo/atualizado em {repo_name}")
            continue

        for pr in prs:
            pr_id = pr["pullRequestId"]
            print(f"\n--- Rodando AI review no PR {pr_id} ({repo_name}) ---")
            changes_data, source_commit, target_commit = get_pr_changes(pr_id, repo_id, AZURE_PROJECT)
            change_list = changes_data.get("changeEntries", [])
            if not change_list:
                continue

            diff_output = []
            total_size = 0
            for c in change_list:
                if c.get("isFolder") or "item" not in c:
                    continue
                path = c["item"]["path"]
                change_type = c.get("changeType", "").lower()
                if "delete" in change_type:
                    continue
                base_content = [] if "add" in change_type else get_file_content(repo_id, AZURE_PROJECT, path, source_commit)
                target_content = get_file_content(repo_id, AZURE_PROJECT, path, target_commit)
                if not base_content and not target_content:
                    continue
                file_diff = list(difflib.unified_diff(
                    base_content, target_content,
                    fromfile=f"a{path}", tofile=f"b{path}", lineterm=""
                ))
                if file_diff:
                    file_diff_text = "\n".join(file_diff)
                    if total_size + len(file_diff_text) > MAX_DIFF_SIZE:
                        diff_output.append(f"\n... (truncated — {MAX_DIFF_SIZE} char limit reached)")
                        break
                    diff_output.append(file_diff_text)
                    total_size += len(file_diff_text)

            diff_text = "\n".join(diff_output)
            if diff_text.strip():
                review_text = ask_llm(diff_text)
                comment_pr_inline(AZURE_PROJECT, repo_id, pr_id, review_text)
                comment_pr_summary(AZURE_PROJECT, repo_id, pr_id, review_text)

        # Atualiza timestamp do repo
        if AZURE_PROJECT not in last_run_data:
            last_run_data[AZURE_PROJECT] = {}
        last_run_data[AZURE_PROJECT][repo_id] = datetime.now(timezone.utc).isoformat()

    write_last_run(last_run_data)

# -----------------------
# Entrypoint
# -----------------------
if __name__ == "__main__":
    run_review_for_project()