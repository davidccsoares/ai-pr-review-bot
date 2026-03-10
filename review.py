import os
import requests
from github import Github
import sys

MAX_DIFF_SIZE = 12000
OPENROUTER_MODEL = "stepfun/step-3.5-flash:free"

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
            headers={"Authorization": f"Bearer {os.environ['OPENROUTER_KEY']}"},
            json={
                "model": OPENROUTER_MODEL,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=60
        )
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"Error calling LLM: {e}"

def run_review(pr_number, repo_full_name):
    token = os.environ["GITHUB_TOKEN"]
    g = Github(token)
    repo = g.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)

    print(f"Running AI review on PR #{pr_number} ({pr.head.ref} → {pr.base.ref})")

    diffs = []
    total_size = 0

    for file in pr.get_files():
        patch = file.patch
        if not patch:
            continue

        if total_size + len(patch) > MAX_DIFF_SIZE:
            diffs.append(f"\n... (truncated — {MAX_DIFF_SIZE} char limit reached)")
            break

        diffs.append(patch)
        total_size += len(patch)

    diff_text = "\n".join(diffs)

    if not diff_text.strip():
        review_text = "No code differences found."
    else:
        review_text = ask_llm(diff_text)

    # Comentário de resumo
    pr.create_issue_comment(f"🤖 **AI Review**\n\n{review_text}")
    print("Review comment posted.")

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python review.py <pr_number> <repo_full_name>")
        sys.exit(1)

    pr_number = int(sys.argv[1])
    repo_full_name = sys.argv[2]
    run_review(pr_number, repo_full_name)