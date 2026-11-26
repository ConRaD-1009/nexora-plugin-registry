"""Prepare, publish, and prune the official Nexora plugin catalog."""
import argparse
import copy
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

INDEX = Path("index.v1.json")
PENDING = Path("pending-prune.json")
KEEP_VERSIONS = 5
TARGETS = {
    f"{arch}-unknown-linux-{abi}"
    for arch in ("x86_64", "aarch64")
    for abi in ("gnu", "musl")
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def version_key(version):
    require(isinstance(version, str) and len(version) <= 64
            and re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version),
            "Expected a stable SemVer version")
    return tuple(map(int, version.split(".")))


def validated(value, pattern):
    require(isinstance(value, str) and re.fullmatch(pattern, value), "Invalid submission field")
    return value


def descriptor(name, digest):
    return {
        "name": validated(name, r"[a-zA-Z0-9_.-]{1,128}"),
        "sha256": validated(digest, r"[a-f0-9]{64}"),
    }


def plugin_in(index, plugin_id):
    return next((p for p in index["plugins"] if p["id"] == plugin_id), None)


def release_in(plugin, version):
    return next((r for r in plugin["releases"] if r["version"] == version), None) if plugin else None


def is_local(release, repository):
    source = release.get("github_repository")
    return source is None or source.rstrip("/").removesuffix(".git").casefold() == (
        f"https://github.com/{repository}".casefold()
    )


def catalog_refs(index, repository):
    refs = {}
    for plugin in index["plugins"]:
        for release in plugin["releases"]:
            if not is_local(release, repository):
                continue
            owners, assets = refs.setdefault(release["tag"], (set(), {}))
            owners.add((plugin["id"], release["version"]))
            for asset in [*release["assets"].values(), release.get("icon")]:
                if asset is not None:
                    require(assets.setdefault(asset["name"], asset) == asset,
                            "Conflicting catalog references to the same attachment")
    return refs


def canonical_tag(plugin_id, version):
    return f"{plugin_id}-v{version}"


def dedicated_tag(tag, owner):
    plugin_id, version = owner
    return tag == canonical_tag(plugin_id, version) or (
        plugin_id == "nexora.filedock" and tag == f"filedock-v{version}"
    )


def candidate(index, submission, history, repository):
    packages = submission["packages"]
    require(isinstance(packages, list) and 1 <= len(packages) <= len(TARGETS),
            "Provide one or more package receipts")
    first = packages[0]
    plugin_id = validated(first["id"], r"[a-z0-9][a-z0-9_.-]{0,127}")
    version = first["version"]
    version_key(version)
    minimum = submission["min_core_version"]
    version_key(minimum)
    tag = validated(submission["tag"], r"[a-zA-Z0-9_.-]{1,128}")
    signer = validated(first["key_id"], r"[a-f0-9]{64}")
    icon = first.get("icon")
    if icon is not None:
        icon = descriptor(icon["name"], icon["sha256"])
        require(icon["name"].lower().endswith((".svg", ".png")), "Invalid icon extension")
    shared = {
        "version": version, "min_core_version": minimum, "signer_key_id": signer, "tag": tag,
    }
    if icon:
        shared["icon"] = icon

    result = copy.deepcopy(index)
    plugin = plugin_in(result, plugin_id)
    if plugin is None:
        fields = submission["plugin"]
        plugin = {"id": plugin_id, **{k: fields[k] for k in ("name", "description", "publisher")},
                  "releases": []}
        result["plugins"].append(plugin)
    old = release_in(plugin, version)
    previous = [r for snapshot in history if (r := release_in(plugin_in(snapshot, plugin_id), version))]
    for release in ([old] if old else []) + previous:
        require(is_local(release, repository) and
                {k: v for k, v in release.items() if k != "assets" and v is not None} == shared,
                "An existing version's release metadata cannot change")
    if not old and not previous:
        require(tag == canonical_tag(plugin_id, version), "Use the canonical tag for a new version")
    owners, _ = catalog_refs(index, repository).get(tag, (set(), {}))
    require(not (owners - {(plugin_id, version)}), "Release tag is already in use")
    for snapshot in history:
        old_owners, _ = catalog_refs(snapshot, repository).get(tag, (set(), {}))
        require(not (old_owners - {(plugin_id, version)}), "Release tag was used for another version")

    new_assets = {}
    for receipt in packages:
        require((receipt["id"], receipt["version"], receipt["key_id"], receipt.get("icon"))
                == (plugin_id, version, signer, icon), "Package receipts disagree")
        target = receipt["target"]
        require(target in TARGETS and target not in new_assets, "Invalid or repeated platform")
        require(not (old and target in old["assets"]) and
                not any(target in r["assets"] for r in previous),
                "This platform version has already been published")
        asset = descriptor(receipt["name"], receipt["archive_sha256"])
        require(asset["name"].endswith(".nxp"), "Expected an .nxp attachment")
        new_assets[target] = asset

    combined = {**old["assets"], **new_assets} if old else new_assets
    names = [asset["name"] for asset in combined.values()]
    require(len(set(names)) == len(names) and (not icon or icon["name"] not in names),
            "Each platform must have its own attachment")
    if old:
        old["assets"] = combined
    else:
        plugin["releases"].append({**shared, "assets": combined})
    counts = {}
    retained = []
    for release in sorted(plugin["releases"], key=lambda r: version_key(r["version"]), reverse=True):
        assets = {}
        for target, asset in release["assets"].items():
            count = counts.get(target, 0)
            if count < KEEP_VERSIONS:
                assets[target] = asset
                counts[target] = count + 1
        if assets:
            retained.append({**release, "assets": assets})
    plugin["releases"] = retained
    remaining = release_in(plugin, version)
    require(remaining and new_assets.keys() <= remaining["assets"].keys(),
            "Submitted version is outside the five retained versions for its platform")
    return result, tag, [*new_assets.values(), *([icon] if icon else [])]


class GitHub:
    def __init__(self):
        self.repository = os.environ["GITHUB_REPOSITORY"]
        self.base = f"https://api.github.com/repos/{self.repository}"
        self.headers = {
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def request(self, path, method="GET"):
        try:
            with urlopen(Request(self.base + path, headers=self.headers, method=method),
                         timeout=30) as response:
                return json.load(response) if response.status != 204 else None
        except HTTPError as error:
            if error.code != 404 or method != "GET":
                raise RuntimeError(f"GitHub {method} returned HTTP {error.code} for {path}") from error
            return None

    def release(self, tag):
        return self.request(f"/releases/tags/{quote(tag, safe='')}")

    def assets(self, release_id):
        results = {}
        for page in range(1, 101):
            batch = self.request(f"/releases/{release_id}/assets?per_page=100&page={page}")
            require(batch is not None, "Release disappeared")
            for asset in batch:
                name = asset["name"]
                require(name not in results and asset["state"] == "uploaded",
                        "Release has duplicate or unfinished attachments")
                results[name] = {key: asset.get(key) for key in ("id", "digest")}
            if len(batch) < 100:
                return results
        raise ValueError("Too many release attachments")

    def tag(self, tag):
        ref = self.request(f"/git/ref/tags/{quote(tag, safe='')}")
        return ref["object"]["sha"] if ref else None


def snapshot(api, tag, declarations, require_digest=False):
    release = api.release(tag)
    require(release and not release["draft"] and not release["prerelease"]
            and not release.get("immutable"), f"Expected a public, editable release for {tag}")
    actual = api.assets(release["id"])
    selected = {}
    for declaration in declarations:
        name = declaration["name"]
        state = actual.get(name)
        digest = "sha256:" + declaration["sha256"]
        require(state and (state["digest"] == digest or
                          (state["digest"] is None and not require_digest)),
                f"Missing attachment or mismatched digest for {tag}/{name}")
        selected[name] = state
    return release["id"], actual, selected


def prepare_state(api, base, result, tag, declarations):
    release_id, _, assets = snapshot(api, tag, declarations, require_digest=True)
    verified = {"tag": tag, "id": release_id, "assets": assets}
    current = catalog_refs(result, api.repository)
    pending = []
    for old_tag, (owners, old_assets) in catalog_refs(base, api.repository).items():
        _, retained = current.get(old_tag, (set(), {}))
        removed = [d for name, d in old_assets.items()
                   if name.endswith(".nxp") and name not in retained]
        if not removed:
            continue
        old_id, actual, selected = snapshot(api, old_tag, removed)
        full = (old_tag not in current and len(owners) == 1
                and dedicated_tag(old_tag, next(iter(owners))) and actual.keys() <= old_assets.keys())
        tag_sha = None
        if full:
            tag_sha = api.tag(old_tag)
            require(tag_sha, f"Release tag disappeared for {old_tag}")
            for name, state in actual.items():
                expected = "sha256:" + old_assets[name]["sha256"]
                require(state["digest"] in (expected, None), f"Attachment changed for {old_tag}/{name}")
            selected = actual
        pending.append({"tag": old_tag, "id": old_id, "assets": selected,
                        "tag_sha": tag_sha})
    return verified, pending


def prune(api, pending, index, check_head):
    refs = catalog_refs(index, api.repository)
    for item in pending:
        check_head()
        tag = item["tag"]
        _, referenced = refs.get(tag, (set(), {}))
        release = api.release(tag)
        full = item["tag_sha"] is not None
        require(not full or tag not in refs, f"Release {tag} is now referenced")
        if release:
            require(release["id"] == item["id"] and not release.get("immutable"),
                    f"Release changed for {tag}")
            actual = api.assets(item["id"])
            if full:
                require(actual == item["assets"] and api.tag(tag) == item["tag_sha"],
                        f"Release attachments or tag changed for {tag}")
                check_head()
                api.request(f"/releases/{item['id']}", "DELETE")
            else:
                for name, expected in item["assets"].items():
                    require(name not in referenced, f"Attachment {tag}/{name} is now referenced")
                    if name in actual:
                        require(actual[name] == expected, f"Attachment changed for {tag}/{name}")
                        check_head()
                        api.request(f"/releases/assets/{expected['id']}", "DELETE")
        if full:
            sha = api.tag(tag)
            if sha is not None:
                require(sha == item["tag_sha"] and api.release(tag) is None, f"Tag changed for {tag}")
                check_head()
                api.request(f"/git/refs/tags/{quote(tag, safe='')}", "DELETE")


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def main_tip():
    return git("ls-remote", "--exit-code", "origin", "refs/heads/main").split()[0]


def require_tip(expected):
    require(main_tip() == expected, "Registry main changed. Re-run from its new tip.")


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def commit(message, *paths):
    git("add", "--", *map(str, paths))
    git("-c", "user.name=github-actions[bot]", "-c",
        "user.email=41898282+github-actions[bot]@users.noreply.github.com", "commit", "-m", message)
    git("push", "origin", "HEAD:refs/heads/main")
    return git("rev-parse", "HEAD")


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def prepare(api, directory):
    require(not PENDING.exists(), "Retry the pending cleanup before publishing")
    history = [
        json.loads(git("show", f"{rev}:{INDEX}"))
        for rev in git("log", "--first-parent", "--diff-filter=AM", "--format=%H", "--", str(INDEX)).splitlines()
    ]
    base = load(INDEX)
    result, tag, declarations = candidate(base, json.loads(os.environ["SUBMISSION"]), history,
                                          api.repository)
    verified, pending = prepare_state(api, base, result, tag, declarations)
    directory.mkdir(parents=True, exist_ok=True)
    write_json(directory / INDEX, result)
    write_json(directory / "plan.json", {
        "base": git("rev-parse", "HEAD"), "verified": verified, "pending": pending,
    })
    print(f"Publish {tag}: {', '.join(verified['assets'])}")
    for item in pending:
        print(f"Prune {item['tag']}: {', '.join(item['assets'])}")


def publish(api, directory):
    plan = load(directory / "plan.json")
    require(git("rev-parse", "HEAD") == plan["base"] and not PENDING.exists(),
            "Registry base or pending cleanup changed")
    require_tip(plan["base"])
    verified = plan["verified"]
    release = api.release(verified["tag"])
    require(release and release["id"] == verified["id"] and not release["draft"]
            and not release["prerelease"] and not release.get("immutable"),
            "Verified release changed")
    assets = api.assets(verified["id"])
    require(all(assets.get(name) == expected for name, expected in verified["assets"].items()),
            "Verified attachments changed")
    write_json(INDEX, load(directory / INDEX))
    paths = [INDEX]
    if plan["pending"]:
        write_json(PENDING, plan["pending"])
        paths.append(PENDING)
    commit(f"Publish plugin {verified['tag']}", *paths)
    cleanup(api)


def cleanup(api):
    tip = git("rev-parse", "HEAD")
    require_tip(tip)
    if PENDING.exists():
        pending = load(PENDING)
        prune(api, pending, load(INDEX), lambda: require_tip(tip))
        PENDING.unlink()
        commit("Finish plugin release cleanup", PENDING)


def audit(index):
    require(not PENDING.exists(), "Retry the pending cleanup before publishing")
    for plugin in index["plugins"]:
        counts = {}
        for release in plugin["releases"]:
            for target in release["assets"]:
                counts[target] = counts.get(target, 0) + 1
        for target, count in sorted(counts.items()):
            print(f"{plugin['id']} {target}: {count}/{KEEP_VERSIONS}")
            require(count <= KEEP_VERSIONS, "Platform exceeds the retention limit")
    print("No pending cleanup")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("audit", "prepare", "publish", "cleanup"))
    parser.add_argument("plan", nargs="?", type=Path)
    args = parser.parse_args()
    try:
        if args.mode == "audit":
            audit(load(INDEX))
        elif args.mode == "cleanup":
            cleanup(GitHub())
        else:
            require(args.plan is not None, "Specify the plan directory")
            (prepare if args.mode == "prepare" else publish)(GitHub(), args.plan)
    except (ValueError, RuntimeError, KeyError, TypeError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from error
