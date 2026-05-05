import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone

import boto3

# ─── AWS clients ─────────────────────────────────────────────────────────────
s3 = boto3.client("s3", region_name="us-east-2")

# ─── Config ──────────────────────────────────────────────────────────────────
GITHUB_TOKEN    = os.environ["GITHUB_TOKEN"]
GITHUB_REPO     = os.environ.get("GITHUB_REPO",        "tamulib-dc-labs/letters-metadata")
BASE_BRANCH     = os.environ.get("GITHUB_BASE_BRANCH", "main")
PIPELINE_BUCKET = os.environ.get("PIPELINE_BUCKET",    "cursive-letters-pipeline")
GIT_USER_NAME   = os.environ.get("GIT_USER_NAME",      "Cursive Pipeline")
GIT_USER_EMAIL  = os.environ.get("GIT_USER_EMAIL",     "cursive-pipeline@tamu.edu")

# Layer-provided binaries land under /opt; standard layer layout is /opt/bin.
# Setting PATH up-front lets git invoke git-lfs as a subcommand seamlessly.
_LAYER_BIN = "/opt/bin"
os.environ["PATH"] = f"{_LAYER_BIN}:{os.environ.get('PATH', '')}"
os.environ["HOME"] = "/tmp"  # git needs a writable HOME for config + lfs cache
# git looks for subcommand helpers (git-remote-https, git-http-fetch, …) here.
os.environ["GIT_EXEC_PATH"] = "/opt/libexec/git-core"
# Suppress "templates not found" warning — we don't ship /usr/share/git-core/templates.
os.environ["GIT_TEMPLATE_DIR"] = ""
# Point git-lfs at a writable cache dir.
os.environ["GIT_LFS_SKIP_SMUDGE"] = "0"
# CA bundle shipped in the layer, for HTTPS to github.com.
os.environ["GIT_SSL_CAINFO"] = "/opt/etc/pki/tls/certs/ca-bundle.crt"
os.environ["SSL_CERT_FILE"] = "/opt/etc/pki/tls/certs/ca-bundle.crt"

# Image extensions LFS will track inside the repo.
LFS_EXTENSIONS = ("jpg", "jpeg", "png", "tif", "tiff", "gif", "webp", "bmp")


# ─── Helpers ─────────────────────────────────────────────────────────────────

def force_string(value):
    if value is None:
        return None
    if isinstance(value, list):
        value = value[0] if value else None
        if value is None:
            return None
    return str(value)


def safe_id(value):
    return re.sub(r"[^a-zA-Z0-9\-]", "_", str(value))


def parse_s3_uri(uri):
    if not uri or not uri.startswith("s3://"):
        return None, None
    parts = uri[5:].split("/", 1)
    if len(parts) != 2:
        return None, None
    return parts[0], parts[1]


def run(cmd, cwd=None, sensitive=False):
    """Shell out, capture output, raise on non-zero. Redacts token in logs."""
    display = cmd if not sensitive else [_redact(c) for c in cmd]
    print(f"[CMD] {' '.join(display)}")
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip())
    if result.returncode != 0:
        raise RuntimeError(f"Command failed [{result.returncode}]: {' '.join(display)}")
    return result


def _redact(value):
    if GITHUB_TOKEN and GITHUB_TOKEN in value:
        return value.replace(GITHUB_TOKEN, "***")
    return value


def gh_api(method, path, body=None):
    url  = f"https://api.github.com{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req  = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization",        f"token {GITHUB_TOKEN}")
    req.add_header("Accept",               "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent",           "CursiveGitHubSync")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub {method} {path} failed [{e.code}]: {err_body}")


def s3_download(bucket, key, dest_path):
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    s3.download_file(bucket, key, dest_path)


def s3_download_uri(uri, dest_path):
    bucket, key = parse_s3_uri(uri)
    if not bucket:
        raise ValueError(f"Invalid S3 URI: {uri}")
    s3_download(bucket, key, dest_path)


def list_output_images(letter_id):
    """List the canonical output image bundle written by CursiveFinalAssembler."""
    lid    = safe_id(letter_id)
    prefix = f"output/{lid}/images/"
    paginator = s3.get_paginator("list_objects_v2")
    images = []
    for page in paginator.paginate(Bucket=PIPELINE_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            images.append((key, key[len(prefix):]))
    images.sort(key=lambda x: x[1])
    return images


# ─── Handler ─────────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    letter_id = force_string(event.get("letterId"))
    if not letter_id:
        raise ValueError("letterId is required")

    final_json_uri = force_string(event.get("finalJsonS3Uri"))
    final_xml_uri  = force_string(event.get("finalXmlS3Uri"))
    if not final_json_uri:
        raise ValueError("finalJsonS3Uri is required")

    lid        = safe_id(letter_id)
    timestamp  = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    new_branch = f"letters/{lid}-{timestamp}"

    work_dir = tempfile.mkdtemp(prefix="ghsync_", dir="/tmp")
    repo_dir = os.path.join(work_dir, "repo")

    try:
        # 1. Shallow-clone base branch using token in URL (HTTPS auth)
        clone_url = f"https://x-access-token:{GITHUB_TOKEN}@github.com/{GITHUB_REPO}.git"
        run(["git", "clone", "--depth", "1", "--branch", BASE_BRANCH, clone_url, repo_dir],
            sensitive=True)

        # 2. Identity + LFS setup
        run(["git", "config", "user.name",  GIT_USER_NAME],  cwd=repo_dir)
        run(["git", "config", "user.email", GIT_USER_EMAIL], cwd=repo_dir)
        run(["git", "lfs", "install", "--local"], cwd=repo_dir)

        for ext in LFS_EXTENSIONS:
            run(["git", "lfs", "track", f"*.{ext}"],         cwd=repo_dir)
            run(["git", "lfs", "track", f"*.{ext.upper()}"], cwd=repo_dir)

        # 3. Branch off main
        run(["git", "checkout", "-b", new_branch], cwd=repo_dir)

        # 4. Stage files into letters/<letterId>/
        letter_dir = os.path.join(repo_dir, "letters", lid)
        images_dir = os.path.join(letter_dir, "images")
        os.makedirs(images_dir, exist_ok=True)

        s3_download_uri(final_json_uri, os.path.join(letter_dir, "final.json"))
        print(f"[OK] Staged final.json")

        if final_xml_uri:
            try:
                s3_download_uri(final_xml_uri, os.path.join(letter_dir, "mods.xml"))
                print(f"[OK] Staged mods.xml")
            except Exception as e:
                print(f"[WARN] Skipping mods.xml: {e}")

        images = list_output_images(letter_id)
        print(f"[INFO] {len(images)} image(s) in output bundle for {letter_id}")
        for key, filename in images:
            s3_download(PIPELINE_BUCKET, key, os.path.join(images_dir, filename))
        print(f"[OK] Staged {len(images)} image(s)")

        # 5. Commit
        run(["git", "add", ".gitattributes", f"letters/{lid}"], cwd=repo_dir)
        # Skip empty commit if nothing changed
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_dir, capture_output=True, text=True, check=True
        )
        if not status.stdout.strip():
            print(f"[INFO] No changes to commit for {letter_id}")
            return {
                "letterId": letter_id,
                "status":   "no_changes",
                "branch":   None,
                "prUrl":    None,
            }

        run(["git", "commit", "-m", f"Add metadata for {letter_id} ({timestamp} UTC)"],
            cwd=repo_dir)

        # 6. Push branch (LFS objects pushed automatically alongside)
        run(["git", "push", "-u", "origin", new_branch], cwd=repo_dir)

        # 7. Open PR against main
        pr = gh_api("POST", f"/repos/{GITHUB_REPO}/pulls", {
            "title": f"Letter metadata: {letter_id}",
            "head":  new_branch,
            "base":  BASE_BRANCH,
            "body": (
                f"Automated metadata sync for **{letter_id}**.\n\n"
                f"- Pipeline run: `{timestamp}` UTC\n"
                f"- Files: `final.json`, `mods.xml`, {len(images)} page image(s) (LFS)\n"
            ),
        })
        pr_url    = pr.get("html_url")
        pr_number = pr.get("number")
        print(f"[OK] PR #{pr_number} → {pr_url}")

        return {
            "letterId":   letter_id,
            "branch":     new_branch,
            "prNumber":   pr_number,
            "prUrl":      pr_url,
            "imageCount": len(images),
        }

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
