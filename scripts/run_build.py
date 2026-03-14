#!/usr/bin/env python3

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STEP_SUMMARY = Path(os.environ["GITHUB_STEP_SUMMARY"]) if os.environ.get("GITHUB_STEP_SUMMARY") else None


def sh(
    cmd: list[str] | str,
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    capture: bool = True,
) -> str:
    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        shell=isinstance(cmd, str),
        capture_output=capture,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {cmd}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result.stdout.strip() if capture else ""


def append_summary(lines: list[str]) -> None:
    if not STEP_SUMMARY:
        return
    with STEP_SUMMARY.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def parse_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "on"}


def normalize_build_version(value: str) -> str:
    version = value.strip()
    version = version[1:] if version.startswith("v") else version
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", version)
    if not match:
        return version
    major, minor, patch = match.groups()
    return f"{major}.{minor}.{patch or '0'}"


def is_semver(value: str) -> bool:
    return bool(re.fullmatch(r"\d+\.\d+\.\d+", value))


def bump_patch(value: str) -> str:
    major, minor, patch = value.split(".")
    return f"{major}.{minor}.{int(patch) + 1}"


def gh_api_json(path: str) -> Any:
    try:
        return json.loads(sh(["gh", "api", path]))
    except RuntimeError:
        return None


def gh_api_text(path: str) -> str:
    data = gh_api_json(path)
    if not data or "content" not in data:
        return ""
    return base64.b64decode(data["content"]).decode("utf-8")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clone_repo(repo: str, token: str, destination: Path) -> tuple[str, str]:
    owner, name = repo.split("/", 1)
    url = f"https://x-access-token:{token}@github.com/{owner}/{name}.git"
    sh(["git", "clone", url, str(destination)])
    branch = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=destination)
    head_sha = sh(["git", "rev-parse", "HEAD"], cwd=destination)
    return branch, head_sha


def docker_login_ghcr(env: dict[str, str]) -> None:
    ghcr_token = env.get("GHCR_TOKEN") or env.get("GH_TOKEN") or env.get("GITHUB_TOKEN")
    ghcr_username = env.get("GHCR_USERNAME") or env.get("GITHUB_REPOSITORY_OWNER") or "github-actions"
    if not ghcr_token:
        raise RuntimeError("GHCR_TOKEN or GH_TOKEN is required")
    sh(
        f"printf '%s' '{ghcr_token}' | docker login ghcr.io -u '{ghcr_username}' --password-stdin",
        env=env,
    )


def resolve_version(config: dict[str, Any], current_build_version: str, current_source_version: str, manual_target_version: str) -> tuple[str, str]:
    if manual_target_version:
        normalized = normalize_build_version(manual_target_version)
        if not is_semver(normalized):
            normalized = current_build_version if is_semver(current_build_version) else "0.1.0"
        return manual_target_version, normalized

    upstream_repo = config.get("upstream_repo", "").strip()
    strategy = config.get("check_strategy", "github_release")
    build_version = ""
    source_version = ""

    if upstream_repo:
        if strategy == "github_release":
            release = gh_api_json(f"repos/{upstream_repo}/releases/latest")
            if isinstance(release, dict) and release.get("tag_name"):
                source_version = str(release["tag_name"])
                build_version = normalize_build_version(source_version)

        if not build_version and strategy in {"github_release", "github_tag"}:
            tags = gh_api_json(f"repos/{upstream_repo}/tags?per_page=20")
            if isinstance(tags, list):
                for tag in tags:
                    name = str(tag.get("name", "")).strip()
                    normalized = normalize_build_version(name)
                    if is_semver(normalized):
                        source_version = name
                        build_version = normalized
                        break

        if not build_version and strategy in {"github_release", "github_tag"}:
            for candidate in ["CHANGELOG.md", "CHANGELOG", "changelog.md", "HISTORY.md", "RELEASE.md"]:
                content = gh_api_text(f"repos/{upstream_repo}/contents/{candidate}")
                if not content:
                    continue
                match = re.search(r"^##\s+\[?v?(\d+\.\d+(?:\.\d+)?)", content, re.MULTILINE)
                if match:
                    source_version = match.group(1)
                    build_version = normalize_build_version(match.group(1))
                    break

        if strategy == "commit_sha" or not build_version:
            repo_meta = gh_api_json(f"repos/{upstream_repo}")
            default_branch = "main"
            if isinstance(repo_meta, dict) and repo_meta.get("default_branch"):
                default_branch = str(repo_meta["default_branch"])
            commit = gh_api_json(f"repos/{upstream_repo}/commits/{default_branch}")
            sha = ""
            if isinstance(commit, dict):
                sha = str(commit.get("sha", ""))[:7]
            source_version = sha or "latest"
            if is_semver(current_build_version):
                if source_version == current_source_version:
                    build_version = current_build_version
                else:
                    build_version = bump_patch(current_build_version)
            else:
                build_version = "0.1.0"

    if not source_version:
        source_version = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    if not build_version:
        build_version = normalize_build_version(source_version)
        if not is_semver(build_version):
            build_version = current_build_version if is_semver(current_build_version) else "0.1.0"
    return source_version, build_version


def extract_primary_service(manifest_text: str) -> str:
    backend = re.search(r"backend:\s*https?://([A-Za-z0-9_.-]+):\d+/?", manifest_text)
    if backend:
        return backend.group(1)
    service_names = re.findall(r"^\s{2}([A-Za-z0-9_.-]+):\s*$", manifest_text, re.MULTILINE)
    return service_names[0] if service_names else ""


def update_service_image(manifest_text: str, service_name: str, image: str) -> tuple[str, int]:
    pattern = re.compile(
        rf"(^\s{{2}}{re.escape(service_name)}:\n(?:\s{{4}}.*\n)*?\s{{4}}image:\s*)([^\n]+)",
        re.MULTILINE,
    )
    return pattern.subn(r"\1" + image, manifest_text, count=1)


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", text).replace("\r", "\n")


def copy_image_to_lazycat(source_image: str, env: dict[str, str]) -> str:
    result = subprocess.run(
        ["lzc-cli", "appstore", "copy-image", source_image],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    output = strip_ansi(f"{result.stdout}\n{result.stderr}").strip()
    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}): lzc-cli appstore copy-image {source_image}\n"
            f"output:\n{output}"
        )
    match = re.search(r"(registry\.lazycat\.cloud/[A-Za-z0-9_/.:-]+)", output)
    if not match:
        raise RuntimeError(f"Failed to extract LazyCat image from output:\n{output}")
    return match.group(1)


def publish_release_asset(
    repo: str,
    tag: str,
    title: str,
    notes: str,
    assets: list[Path],
    env: dict[str, str],
) -> str:
    sh(["gh", "release", "delete", tag, "--repo", repo, "--yes"], env=env, check=False)
    cmd = [
        "gh",
        "release",
        "create",
        tag,
        "--repo",
        repo,
        "--title",
        title,
        "--notes",
        notes,
    ] + [str(asset) for asset in assets]
    sh(cmd, env=env)
    return f"https://github.com/{repo}/releases/tag/{tag}"


def upload_release_asset(repo: str, tag: str, asset: Path, env: dict[str, str]) -> None:
    sh(
        ["gh", "release", "upload", tag, str(asset), "--repo", repo, "--clobber"],
        env=env,
    )


def build_report_base(
    *,
    config: dict[str, Any],
    artifact_repo: str,
    branch: str,
    head_sha: str,
    force_build: bool,
    publish_to_store: bool,
    check_only: bool,
    target_version: str,
) -> dict[str, Any]:
    return {
        "repo": config["repo"],
        "artifact_repo": artifact_repo,
        "branch": branch,
        "head_sha": head_sha,
        "force_build": force_build,
        "publish_to_store": publish_to_store,
        "check_only": check_only,
        "target_version": target_version,
        "status": "running",
        "phase": "start",
        "error": "",
        "source_version": "",
        "build_version": "",
        "update_needed": False,
        "image_targets": config.get("image_targets", []),
        "dependencies": config.get("dependencies", []),
        "target_image": "",
        "accelerated_image": "",
        "artifact_release_tag": "",
        "artifact_release_url": "",
        "lpk_name": "",
        "lpk_sha256": "",
        "report_generated_at": datetime.now(timezone.utc).isoformat(),
    }


def write_report(report: dict[str, Any], path: Path) -> None:
    report["report_generated_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n")


def publish_report_summary(report: dict[str, Any]) -> None:
    lines = [
        f"### {report.get('repo', '')}",
        f"- status: {report.get('status', '')}",
        f"- phase: {report.get('phase', '')}",
        f"- source_version: {report.get('source_version', '')}",
        f"- build_version: {report.get('build_version', '')}",
        f"- target_image: {report.get('target_image', '')}",
        f"- accelerated_image: {report.get('accelerated_image', '')}",
        f"- artifact_release_url: {report.get('artifact_release_url', '')}",
    ]
    if report.get("error"):
        lines.append(f"- error: {report['error']}")
    append_summary(lines + [""])


def resolve_image_targets(config: dict[str, Any], manifest_text: str) -> list[str]:
    configured = [str(item).strip() for item in config.get("image_targets", []) if str(item).strip()]
    if configured:
        return configured

    primary = extract_primary_service(manifest_text)
    return [primary] if primary else []


def checkout_source_ref(source_root: Path, source_ref: str, env: dict[str, str]) -> None:
    if not source_ref:
        return
    candidates = [source_ref]
    if not source_ref.startswith("v"):
        candidates.append(f"v{source_ref}")

    for candidate in candidates:
        attempt = subprocess.run(
            ["git", "checkout", candidate],
            cwd=source_root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        if attempt.returncode == 0:
            return
        subprocess.run(
            ["git", "fetch", "--depth", "1", "origin", candidate],
            cwd=source_root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        attempt = subprocess.run(
            ["git", "checkout", candidate],
            cwd=source_root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        if attempt.returncode == 0:
            return

    raise RuntimeError(f"Failed to checkout upstream ref: {source_ref}")


def build_with_dockerfile(
    build_root: Path,
    target_image: str,
    env: dict[str, str],
    dockerfile_rel: str,
    build_context_rel: str,
    build_args: dict[str, Any],
) -> str:
    dockerfile = build_root / dockerfile_rel
    context = build_root / build_context_rel
    if not dockerfile.exists():
        raise RuntimeError(f"Dockerfile not found: {dockerfile}")
    args = ["docker", "build", "-f", str(dockerfile), "-t", target_image]
    for key, value in build_args.items():
        args.extend(["--build-arg", f"{key}={value}"])
    args.append(str(context))
    sh(args, cwd=build_root, env=env)
    sh(["docker", "push", target_image], env=env)
    return target_image


def copy_overlay_paths(repo_dir: Path, build_root: Path, overlay_paths: list[str]) -> None:
    for relative in overlay_paths:
        source = repo_dir / relative
        target = build_root / relative
        if not source.exists():
            raise RuntimeError(f"Overlay path not found: {source}")
        if source.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def build_target_image(
    repo_dir: Path,
    config: dict[str, Any],
    env: dict[str, str],
    source_version: str,
    build_version: str,
    head_sha: str,
) -> str:
    owner, name = config["repo"].split("/", 1)
    owner_lower = owner.lower()
    name_lower = name.lower()
    image_tag = head_sha[:12]
    target_image = f"ghcr.io/{owner_lower}/{name_lower}:{image_tag}"
    docker_login_ghcr(env)
    build_strategy = str(config.get("build_strategy", "")).strip()
    build_args = dict(config.get("build_args", {}))
    overlay_paths = [str(item).strip() for item in config.get("overlay_paths", []) if str(item).strip()]
    build_args.setdefault("SOURCE_VERSION", source_version)
    build_args.setdefault("BUILD_VERSION", build_version)

    official_registry = str(config.get("official_image_registry", "")).strip()
    if build_strategy == "official_image" and official_registry:
        tag_candidates = [source_version, build_version]
        for tag in tag_candidates:
            if not tag:
                continue
            source_image = f"{official_registry}:{tag}"
            inspect = subprocess.run(
                ["docker", "manifest", "inspect", source_image],
                text=True,
                capture_output=True,
                check=False,
            )
            if inspect.returncode == 0:
                sh(["docker", "pull", source_image], env=env)
                sh(["docker", "tag", source_image, target_image], env=env)
                sh(["docker", "push", target_image], env=env)
                return target_image

    binary_url = str(config.get("precompiled_binary_url", "")).strip()
    if build_strategy == "precompiled_binary" and binary_url:
        build_root = Path(tempfile.mkdtemp(prefix="lzcat-binary-"))
        try:
            binary_url = binary_url.replace("$LATEST_VERSION", build_version)
            archive_path = build_root / "artifact"
            sh(["curl", "-fsSL", binary_url, "-o", str(archive_path)], env=env)
            if binary_url.endswith((".tar.gz", ".tgz")):
                sh(["tar", "xzf", str(archive_path), "-C", str(build_root)], env=env)
            elif binary_url.endswith(".zip"):
                sh(["unzip", "-o", str(archive_path), "-d", str(build_root)], env=env)

            binary_name = name_lower
            binary_path = build_root / binary_name
            if not binary_path.exists():
                candidates = [item for item in build_root.rglob("*") if item.is_file() and os.access(item, os.X_OK)]
                if candidates:
                    shutil.copy2(candidates[0], binary_path)

            dockerfile_type = str(config.get("dockerfile_type", "simple")).strip()
            dockerfile_path = build_root / "Dockerfile"
            if dockerfile_type == "custom":
                template_path = repo_dir / "Dockerfile.template"
                if not template_path.exists():
                    raise RuntimeError("dockerfile_type=custom but Dockerfile.template is missing")
                content = template_path.read_text()
                content = content.replace("{{PROJECT_NAME_LOWER}}", name_lower)
                content = content.replace("{{SERVICE_PORT}}", str(config.get("service_port", "")))
                dockerfile_path.write_text(content)
            else:
                service_cmd = json.dumps(config.get("service_cmd", []), ensure_ascii=True)
                dockerfile_path.write_text(
                    "\n".join(
                        [
                            "FROM debian:bookworm-slim",
                            "RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*",
                            f"COPY {binary_name} /usr/local/bin/{binary_name}",
                            f"RUN chmod +x /usr/local/bin/{binary_name}",
                            f"EXPOSE {config.get('service_port', 0)}",
                            f"CMD {service_cmd}",
                            "",
                        ]
                    )
                )
            sh(["docker", "build", "-t", target_image, "."], cwd=build_root, env=env)
            sh(["docker", "push", target_image], env=env)
            return target_image
        finally:
            shutil.rmtree(build_root, ignore_errors=True)

    if build_strategy == "target_repo_dockerfile":
        dockerfile_path = str(config.get("dockerfile_path", "Dockerfile")).strip()
        build_context = str(config.get("build_context", ".")).strip()
        return build_with_dockerfile(repo_dir, target_image, env, dockerfile_path, build_context, build_args)

    upstream_repo = str(config.get("upstream_repo", "")).strip()
    if not upstream_repo:
        raise RuntimeError("No upstream_repo configured for source build fallback")

    source_root = Path(tempfile.mkdtemp(prefix="lzcat-upstream-"))
    try:
        sh(["git", "clone", f"https://github.com/{upstream_repo}.git", str(source_root)], env=env)
        checkout_source_ref(source_root, source_version, env)
        if build_strategy == "upstream_with_target_template":
            template_path = repo_dir / str(config.get("dockerfile_path", "Dockerfile.template"))
            if not template_path.exists():
                raise RuntimeError(f"Template Dockerfile missing: {template_path}")
            content = template_path.read_text()
            content = content.replace("{{PROJECT_NAME_LOWER}}", name_lower)
            content = content.replace("{{SERVICE_PORT}}", str(config.get("service_port", "")))
            (source_root / "Dockerfile").write_text(content)
            copy_overlay_paths(repo_dir, source_root, overlay_paths)
            # Run prepare_build_context.py if exists (for frontend API URL replacement)
            prepare_script = source_root / "lazycat" / "prepare_build_context.py"
            if prepare_script.exists():
                sh(["python3", str(prepare_script), str(source_root)], env=env)
            return build_with_dockerfile(source_root, target_image, env, "Dockerfile", ".", build_args)
        dockerfile_path = source_root / "Dockerfile"
        if not dockerfile_path.exists():
            candidates = list(source_root.rglob("Dockerfile"))
            if not candidates:
                raise RuntimeError("No Dockerfile found in upstream repository")
            dockerfile_path = candidates[0]
        return build_with_dockerfile(
            source_root,
            target_image,
            env,
            str(dockerfile_path.relative_to(source_root)),
            ".",
            build_args,
        )
    finally:
        shutil.rmtree(source_root, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-root", required=True)
    parser.add_argument("--config-file", required=True)
    parser.add_argument("--artifact-repo", required=True)
    parser.add_argument("--target-version", default="")
    parser.add_argument("--force-build", action="store_true")
    parser.add_argument("--publish-to-store", action="store_true")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    config_path = Path(args.config_root) / "repos" / args.config_file
    config = json.loads(config_path.read_text())
    env = os.environ.copy()
    gh_token = env.get("GH_TOKEN") or env.get("GITHUB_TOKEN")
    if not gh_token:
        raise RuntimeError("GH_TOKEN or GITHUB_TOKEN is required")

    work_root = Path(tempfile.mkdtemp(prefix="lzcat-target-"))
    repo_dir = work_root / "target"
    report_path = work_root / "build-report.json"
    report: dict[str, Any] | None = None
    try:
        branch, head_sha = clone_repo(config["repo"], gh_token, repo_dir)
        publish_to_store = args.publish_to_store or parse_bool(config.get("publish_to_store"), False)
        report = build_report_base(
            config=config,
            artifact_repo=args.artifact_repo,
            branch=branch,
            head_sha=head_sha,
            force_build=args.force_build,
            publish_to_store=publish_to_store,
            check_only=args.check_only,
            target_version=args.target_version,
        )
        write_report(report, report_path)
        manifest_path = repo_dir / "lzc-manifest.yml"
        manifest_text = manifest_path.read_text()
        meta_path = repo_dir / ".lazycat-build.json"
        build_meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        current_version_match = re.search(r"^version:\s*(.+)$", manifest_text, re.MULTILINE)
        current_version = current_version_match.group(1).strip() if current_version_match else ""
        current_build_version = str(build_meta.get("build_version", current_version)).strip()
        current_source_version = str(build_meta.get("source_version", "")).strip()

        source_version, build_version = resolve_version(
            config,
            current_build_version,
            current_source_version,
            args.target_version,
        )
        report["source_version"] = source_version
        report["build_version"] = build_version
        update_needed = bool(args.target_version or args.force_build)
        if not update_needed:
            if current_source_version:
                update_needed = current_source_version != source_version
            else:
                update_needed = current_version != build_version
        report["update_needed"] = update_needed
        report["phase"] = "check_update"
        write_report(report, report_path)

        print(
            json.dumps(
                {
                    "repo": config["repo"],
                    "source_version": source_version,
                    "build_version": build_version,
                    "update_needed": update_needed,
                    "check_only": args.check_only,
                },
                ensure_ascii=True,
            )
        )

        if not update_needed or args.check_only:
            report["status"] = "skipped"
            report["phase"] = "completed"
            write_report(report, report_path)
            publish_report_summary(report)
            return 0

        report["phase"] = "build_image"
        write_report(report, report_path)
        target_image = build_target_image(repo_dir, config, env, source_version, build_version, head_sha)
        report["target_image"] = target_image
        write_report(report, report_path)

        report["phase"] = "copy_image"
        write_report(report, report_path)
        accelerated_url = copy_image_to_lazycat(target_image, env)
        report["accelerated_image"] = accelerated_url
        write_report(report, report_path)

        image_targets = resolve_image_targets(config, manifest_text)
        if not image_targets:
            raise RuntimeError("No target services configured for main image update")
        updated_manifest = manifest_text
        for target_service in image_targets:
            updated_manifest, count = update_service_image(updated_manifest, target_service, accelerated_url)
            if count != 1:
                raise RuntimeError(f"Failed to update service image in lzc-manifest.yml: {target_service}")

        for dependency in config.get("dependencies", []):
            target_service = str(dependency.get("target_service", "")).strip()
            source_image = str(dependency.get("source_image", "")).strip()
            if not target_service or not source_image:
                continue
            dependency_image = copy_image_to_lazycat(source_image, env)
            updated_manifest, dep_count = update_service_image(updated_manifest, target_service, dependency_image)
            if dep_count != 1:
                raise RuntimeError(f"Failed to update dependency image for service: {target_service}")

        updated_manifest = re.sub(
            r"^version:\s*.+$",
            f"version: {build_version}",
            updated_manifest,
            count=1,
            flags=re.MULTILINE,
        )
        manifest_path.write_text(updated_manifest)

        build_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        build_label = f"{source_version}@{build_stamp}"
        meta_path.write_text(
            json.dumps(
                {
                    "source_version": source_version,
                    "build_version": build_version,
                    "build_label": build_label,
                    "last_checked_at": datetime.now(timezone.utc).isoformat(),
                },
                ensure_ascii=True,
                indent=2,
            )
            + "\n"
        )

        project_name_lower = config["repo"].split("/", 1)[1].lower()
        lpk_path = repo_dir / f"{project_name_lower}.lpk"
        report["phase"] = "package"
        write_report(report, report_path)
        sh(["lzc-cli", "project", "build", "-o", str(lpk_path)], cwd=repo_dir, env=env)
        report["lpk_name"] = lpk_path.name
        report["lpk_sha256"] = file_sha256(lpk_path)
        write_report(report, report_path)

        if publish_to_store:
            report["phase"] = "publish_store"
            write_report(report, report_path)
            sh(
                [
                    "lzc-cli",
                    "appstore",
                    "publish",
                    str(lpk_path),
                    "--clang",
                    "en",
                    "-c",
                    f"Auto-built version {build_version}",
                ],
                cwd=repo_dir,
                env=env,
            )

        report["phase"] = "publish_artifact"
        report["artifact_release_tag"] = f"{config['repo'].replace('/', '--')}-v{build_version}-{build_stamp}"
        write_report(report, report_path)
        report["artifact_release_url"] = publish_release_asset(
            args.artifact_repo,
            report["artifact_release_tag"],
            f"{config['repo']} v{build_version} ({build_stamp})",
            f"Auto-built version {build_version} (source: {source_version}, label: {build_label})",
            [lpk_path, report_path],
            env,
        )
        write_report(report, report_path)
        upload_release_asset(args.artifact_repo, report["artifact_release_tag"], report_path, env)

        report["phase"] = "commit_target_repo"
        write_report(report, report_path)
        sh(["git", "config", "user.name", "github-actions[bot]"], cwd=repo_dir)
        sh(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], cwd=repo_dir)
        sh(["git", "add", "lzc-manifest.yml", ".lazycat-build.json"], cwd=repo_dir)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo_dir, check=False)
        if diff.returncode != 0:
            sh(["git", "commit", "-m", f"Update to version {build_version}"], cwd=repo_dir)
            sh(["git", "push", "origin", f"HEAD:{branch}"], cwd=repo_dir, env=env)
        report["status"] = "success"
        report["phase"] = "completed"
        write_report(report, report_path)
        publish_report_summary(report)
        return 0
    except Exception as exc:
        if report is None:
            report = {
                "repo": config["repo"] if "config" in locals() else "",
                "artifact_repo": args.artifact_repo if "args" in locals() else "",
                "status": "failed",
                "phase": "startup",
                "error": str(exc),
                "report_generated_at": datetime.now(timezone.utc).isoformat(),
            }
        else:
            report["status"] = "failed"
            report["error"] = str(exc)
        write_report(report, report_path)
        publish_report_summary(report)
        raise
    finally:
        shutil.rmtree(work_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
