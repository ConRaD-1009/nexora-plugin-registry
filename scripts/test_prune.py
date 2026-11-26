import copy
import json
import subprocess
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from unittest.mock import patch

import prune

X86 = "x86_64-unknown-linux-gnu"
ARM = "aarch64-unknown-linux-gnu"
REPO = "example/registry"
PLUGIN = "example.notes"
ICON = {"name": "notes.svg", "sha256": "b" * 64}


def package_name(version, target):
    return f"notes-{version}-{target}.nxp"


def release(version, *targets):
    return {
        "version": version,
        "tag": prune.canonical_tag(PLUGIN, version),
        "signer_key_id": "a" * 64,
        "min_core_version": "1.0.0",
        "icon": ICON,
        "assets": {
            target: {"name": package_name(version, target), "sha256": "c" * 64}
            for target in targets
        },
    }


def catalog(*releases):
    return {"schema_version": 1, "plugins": [{"id": PLUGIN, "releases": list(releases)}]}


def six_versions(*old_targets):
    return catalog(release("1.0.1", *old_targets),
                   *(release(f"1.0.{minor}", X86) for minor in range(2, 7)))


class Releases:
    repository = REPO

    def __init__(self, index):
        self.releases = {}
        self.attachments = {}
        self.tags = {}
        self.deleted = []
        for release_id, (tag, (_, assets)) in enumerate(prune.catalog_refs(index, REPO).items(), 1):
            self.releases[tag] = {"id": release_id, "draft": False, "prerelease": False}
            self.tags[tag] = f"sha-{tag}"
            self.attachments[release_id] = {
                name: {"id": 100 * release_id + i, "digest": "sha256:" + a["sha256"]}
                for i, (name, a) in enumerate(assets.items())
            }

    def release(self, tag):
        return self.releases.get(tag)

    def assets(self, release_id):
        return copy.deepcopy(self.attachments[release_id])

    def tag(self, tag):
        return self.tags.get(tag)

    def request(self, path, method):
        assert method == "DELETE"
        self.deleted.append(path)
        if path.startswith("/git/refs/tags/"):
            del self.tags[path.rsplit("/", 1)[-1]]
        elif path.startswith("/releases/assets/"):
            for assets in self.attachments.values():
                for name, asset in list(assets.items()):
                    if asset["id"] == int(path.rsplit("/", 1)[-1]):
                        del assets[name]
        else:
            for tag, release in list(self.releases.items()):
                if release["id"] == int(path.rsplit("/", 1)[-1]):
                    del self.releases[tag]


class RetentionTest(unittest.TestCase):
    def test_independent_counts_and_semver(self):
        before = catalog(
            *(release(f"1.0.{minor}", X86, ARM) for minor in range(1, 6)),
            *(release(f"1.0.{minor}", X86) for minor in (8, 10, 9)),
        )
        small = {"id": "example.other", "releases": [release("1.0.1", X86)]}
        before["plugins"].append(small)
        releases, other = prune.trim(before)["plugins"]
        self.assertEqual(other, small)
        self.assertEqual([r["version"] for r in releases["releases"] if X86 in r["assets"]],
                         ["1.0.4", "1.0.5", "1.0.8", "1.0.10", "1.0.9"])
        self.assertEqual([r["version"] for r in releases["releases"] if ARM in r["assets"]],
                         ["1.0.1", "1.0.2", "1.0.3", "1.0.4", "1.0.5"])
        self.assertIn(X86, before["plugins"][0]["releases"][0]["assets"])

    def test_partial_prune_respects_live_references_and_can_retry(self):
        before = six_versions(X86, ARM)
        after = prune.trim(before)
        api = Releases(before)
        [item] = pending = prune.prepare_prune(api, before, after)
        self.assertIsNone(item["tag_sha"])
        with self.assertRaisesRegex(ValueError, "now referenced"):
            prune.prune(api, pending, before, lambda: None)
        self.assertFalse(api.deleted)

        prune.prune(api, pending, after, lambda: None)
        prune.prune(api, pending, after, lambda: None)
        self.assertEqual(len(api.deleted), 1)
        self.assertIn(item["tag"], api.releases)
        self.assertEqual(set(api.assets(item["id"])), {ICON["name"], package_name("1.0.1", ARM)})

    def test_full_prune_can_resume_after_removing_release(self):
        before = six_versions(X86)
        after = prune.trim(before)
        api = Releases(before)
        [item] = pending = prune.prepare_prune(api, before, after)
        delete = api.request

        def disconnect(path, method):
            if path.startswith("/git/refs/tags/"):
                raise OSError("connection dropped")
            delete(path, method)

        with patch.object(api, "request", disconnect), self.assertRaises(OSError):
            prune.prune(api, pending, after, lambda: None)
        self.assertNotIn(item["tag"], api.releases)
        self.assertIn(item["tag"], api.tags)
        prune.prune(api, pending, after, lambda: None)
        self.assertNotIn(item["tag"], api.tags)
        self.assertEqual(len(api.deleted), 2)

    def test_external_attachments_and_unlisted_uploads_are_never_deleted(self):
        before = six_versions(X86)
        external = copy.deepcopy(before)
        external["plugins"][0]["releases"][0]["github_repository"] = "https://github.com/author/plugin"
        self.assertEqual(prune.prepare_prune(Releases(external), external, prune.trim(external)), [])
        api = Releases(before)
        tag = before["plugins"][0]["releases"][0]["tag"]
        release_id = api.release(tag)["id"]
        api.attachments[release_id]["not-yet-indexed.nxp"] = {"id": 9000, "digest": "sha256:" + "f" * 64}
        after = prune.trim(before)
        [item] = pending = prune.prepare_prune(api, before, after)
        self.assertIsNone(item["tag_sha"])
        prune.prune(api, pending, after, lambda: None)
        self.assertIn("not-yet-indexed.nxp", api.assets(release_id))
        self.assertIn(tag, api.tags)

    def test_digest_mismatch_never_creates_deletion_plan(self):
        before = six_versions(X86)
        api = Releases(before)
        api.attachments[1][package_name("1.0.1", X86)]["digest"] = "sha256:" + "f" * 64
        with self.assertRaisesRegex(ValueError, "mismatched digest"):
            prune.prepare_prune(api, before, prune.trim(before))
        self.assertFalse(api.deleted)

    def test_preview_does_not_write_or_delete(self):
        before = six_versions(X86)
        api = Releases(before)
        with tempfile.TemporaryDirectory() as directory, chdir(directory):
            prune.write_json(prune.INDEX, before)
            prune.run(api, preview=True)
            self.assertEqual(prune.load(prune.INDEX), before)
            self.assertFalse(prune.PENDING.exists())
        self.assertFalse(api.deleted)

    def test_index_push_failure_does_not_delete(self):
        before = six_versions(X86)
        api = Releases(before)
        with tempfile.TemporaryDirectory() as directory, chdir(directory):
            prune.write_json(prune.INDEX, before)
            with patch.object(prune, "git", return_value="sha"), \
                    patch.object(prune, "commit", side_effect=OSError("push failed")), \
                    self.assertRaisesRegex(OSError, "push failed"):
                prune.run(api)
        self.assertFalse(api.deleted)

    def test_index_and_pending_are_on_origin_before_any_delete(self):
        before = six_versions(X86)
        after = prune.trim(before)
        api = Releases(before)
        expected = prune.prepare_prune(api, before, after)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            origin, checkout = root / "origin", root / "checkout"
            checkout.mkdir()
            subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
            with chdir(checkout):
                prune.git("init", "-q", "-b", "main")
                prune.git("remote", "add", "origin", str(origin))
                prune.write_json(prune.INDEX, before)
                prune.commit("Seed test catalog", prune.INDEX)
                delete = api.request

                def inspect_pushed_state(path, method):
                    index = prune.git("--git-dir", str(origin), "show", "main:index.v1.json")
                    pending = prune.git("--git-dir", str(origin), "show", "main:pending-prune.json")
                    self.assertEqual(json.loads(index), after)
                    self.assertEqual(json.loads(pending), expected)
                    delete(path, method)

                with patch.object(api, "request", inspect_pushed_state):
                    prune.run(api)
                self.assertFalse(prune.PENDING.exists())
                self.assertEqual(prune.git("--git-dir", str(origin), "ls-tree", "--name-only", "main"),
                                 str(prune.INDEX))
                self.assertEqual(len(api.deleted), 2)
                tip = prune.main_tip()
                prune.run(api)
                self.assertEqual(prune.main_tip(), tip)
                self.assertEqual(len(api.deleted), 2)


if __name__ == "__main__":
    unittest.main()
