"""Keep the latest plugin versions for each target in the official catalog."""
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


def trim(index):
    require(index["schema_version"] == 1, "Unsupported catalog schema")
    result = copy.deepcopy(index)
    seen_plugins = set()
    for plugin in result["plugins"]:
        require(plugin["id"] not in seen_plugins, "Duplicate catalog plugin ID")
        seen_plugins.add(plugin["id"])
        releases = sorted(plugin["releases"], key=lambda r: version_key(r["version"]), reverse=True)
        versions = {release["version"] for release in releases}
        require(releases and len(versions) == len(releases), "Missing or duplicate plugin versions")
        counts = {}
        for release in releases:
            assets = release["assets"]
            require(assets and assets.keys() <= TARGETS, "Missing or unsupported release platforms")
            for target in list(assets):
                counts[target] = counts.get(target, 0) + 1
                if counts[target] > KEEP_VERSIONS:
                    del assets[target]
        # Preserve the index's existing order and all metadata on retained records.
        plugin["releases"] = [release for release in plugin["releases"] if release["assets"]]
    return result


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


def snapshot(api, tag, declarations):
    release = api.release(tag)
    require(release and not release["draft"] and not release["prerelease"]
            and not release.get("immutable"), f"Expected a public, editable release for {tag}")
    actual = api.assets(release["id"])
    selected = {}
    for declaration in declarations:
        name = declaration["name"]
        state = actual.get(name)
        digest = "sha256:" + declaration["sha256"]
        require(state and state["digest"] == digest,
                f"Missing attachment or mismatched digest for {tag}/{name}")
        selected[name] = state
    return release["id"], actual, selected


def prepare_prune(api, base, result):
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
                require(state["digest"] == expected, f"Attachment changed for {old_tag}/{name}")
            selected = actual
        pending.append({"tag": old_tag, "id": old_id, "assets": selected,
                        "tag_sha": tag_sha})
    return pending


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
            require(release["id"] == item["id"] and not release["draft"]
                    and not release["prerelease"] and not release.get("immutable"),
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


def finish_pending(api, pending, index, tip):
    prune(api, pending, index, lambda: require_tip(tip))
    require_tip(tip)
    PENDING.unlink()
    return commit("Finish plugin release cleanup", PENDING)


def show_plan(pending):
    for item in pending:
        tag = item["tag"]
        if item["tag_sha"]:
            print(f"Prune release and tag {tag}")
        else:
            for name in item["assets"]:
                print(f"Prune attachment {tag}/{name}")


def run(api, preview=False):
    base = load(INDEX)
    if not preview:
        tip = git("rev-parse", "HEAD")
        require_tip(tip)
    if PENDING.exists():
        pending = load(PENDING)
        print("Pending from an earlier cleanup:")
        show_plan(pending)
        if preview:
            return
        tip = finish_pending(api, pending, base, tip)

    result = trim(base)
    if result == base:
        print("No plugin platform exceeds the retention limit")
        return

    for plugin, remaining in zip(base["plugins"], result["plugins"], strict=True):
        retained = {(r["version"], t) for r in remaining["releases"] for t in r["assets"]}
        for release in plugin["releases"]:
            for target in release["assets"]:
                if (release["version"], target) not in retained:
                    print(f"Unlist {plugin['id']} {release['version']} {target}")

    pending = prepare_prune(api, base, result)
    show_plan(pending)
    if preview:
        return

    require_tip(tip)
    write_json(INDEX, result)
    paths = [INDEX]
    if pending:
        write_json(PENDING, pending)
        paths.append(PENDING)
    tip = commit("Keep five plugin versions per target", *paths)
    if pending:
        finish_pending(api, pending, result, tip)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", action="store_true", help="Report changes without writing")
    args = parser.parse_args()
    try:
        run(GitHub(), args.preview)
    except (ValueError, RuntimeError, KeyError, TypeError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from error
