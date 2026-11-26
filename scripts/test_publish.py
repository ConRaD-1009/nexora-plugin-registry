import copy
import json
import subprocess
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from unittest.mock import Mock, patch

import publish

X86 = "x86_64-unknown-linux-gnu"
ARM = "aarch64-unknown-linux-gnu"
REPO = "example/registry"
PLUGIN = "example.notes"
ICON = {"name": "notes.svg", "sha256": "b" * 64}


def submission(version, *targets):
    tag = publish.canonical_tag(PLUGIN, version)
    return {
        "tag": tag,
        "min_core_version": "0.1.0",
        "plugin": {"name": "Notes", "description": "Notes", "publisher": "Example"},
        "packages": [
            {
                "id": PLUGIN, "version": version, "target": target, "key_id": "a" * 64,
                "name": f"notes-{version}-{target}.nxp", "archive_sha256": "c" * 64, "icon": ICON,
            }
            for target in targets
        ],
    }


def append(index, version, *targets, history=()):
    return publish.candidate(index, submission(version, *targets), history, REPO)[0]


class Releases:
    repository = REPO

    def __init__(self, index):
        self.releases = {}
        self.attachments = {}
        self.tags = {}
        self.deleted = []
        for release_id, (tag, (_, assets)) in enumerate(publish.catalog_refs(index, REPO).items(), 1):
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


class PublicationTest(unittest.TestCase):
    def setUp(self):
        self.empty = {"schema_version": 1, "plugins": []}

    def test_independent_retention_and_numeric_order(self):
        index = self.empty
        for minor in (1, 2, 3, 4, 5, 8, 9, 10):
            targets = (X86, ARM) if minor <= 5 else (X86,)
            index = append(index, f"1.0.{minor}", *targets)
        releases = index["plugins"][0]["releases"]
        self.assertEqual([r["version"] for r in releases if X86 in r["assets"]],
                         ["1.0.10", "1.0.9", "1.0.8", "1.0.5", "1.0.4"])
        self.assertEqual([r["version"] for r in releases if ARM in r["assets"]],
                         ["1.0.5", "1.0.4", "1.0.3", "1.0.2", "1.0.1"])
        with self.assertRaisesRegex(ValueError, "outside the five"):
            append(index, "1.0.0", ARM)

    def test_late_platform_and_no_replacement(self):
        old = append(self.empty, "1.0.0", X86)
        new = append(old, "1.0.0", ARM, history=[old])
        self.assertEqual(set(new["plugins"][0]["releases"][0]["assets"]), {X86, ARM})
        with self.assertRaisesRegex(ValueError, "already been published"):
            append(new, "1.0.0", ARM)
        with self.assertRaisesRegex(ValueError, "already been published"):
            append(self.empty, "1.0.0", X86, history=[old])
        changed = submission("1.0.0", ARM)
        changed["min_core_version"] = "0.2.0"
        with self.assertRaisesRegex(ValueError, "metadata cannot change"):
            publish.candidate(old, changed, [], REPO)

    def test_prune_only_stale_platform_and_retry_after_failure(self):
        before = append(self.empty, "1.0.1", X86, ARM)
        for minor in range(2, 6):
            before = append(before, f"1.0.{minor}", X86)
        after, tag, artifacts = publish.candidate(before, submission("1.0.6", X86), [], REPO)
        api = Releases({"plugins": [
            {"id": PLUGIN, "releases": before["plugins"][0]["releases"]
             + [after["plugins"][0]["releases"][0]]}
        ]})
        _, pending = publish.prepare_state(api, before, after, tag, artifacts)
        old_tag = publish.canonical_tag(PLUGIN, "1.0.1")
        [entry] = pending
        self.assertEqual(entry["tag"], old_tag)
        self.assertIsNone(entry["tag_sha"])
        self.assertListEqual(list(entry["assets"]), [submission("1.0.1", X86)["packages"][0]["name"]])
        with self.assertRaisesRegex(ValueError, "now referenced"):
            publish.prune(api, pending, before, lambda: None)
        self.assertFalse(api.deleted)
        publish.prune(api, pending, after, lambda: None)
        publish.prune(api, pending, after, lambda: None)
        self.assertEqual(len(api.deleted), 1)
        self.assertIn(old_tag, api.releases)
        self.assertIn(ICON["name"], api.assets(entry["id"]))
        self.assertIn(submission("1.0.1", ARM)["packages"][0]["name"], api.assets(entry["id"]))

    def test_full_release_delete_can_resume_between_release_and_tag(self):
        before = {"plugins": [{"id": PLUGIN, "releases": []}]}
        for minor in range(1, 6):
            before = append(before, f"1.0.{minor}", X86)
        after, tag, artifacts = publish.candidate(before, submission("1.0.6", X86), [], REPO)
        api = Releases({"plugins": [
            {"id": PLUGIN, "releases": before["plugins"][0]["releases"]
             + [after["plugins"][0]["releases"][0]]}
        ]})
        _, pending = publish.prepare_state(api, before, after, tag, artifacts)
        old_tag = publish.canonical_tag(PLUGIN, "1.0.1")
        delete = api.request

        def lost_connection(path, method):
            if path.startswith("/git/refs/tags/"):
                raise OSError("connection dropped")
            delete(path, method)

        with patch.object(api, "request", lost_connection), self.assertRaises(OSError):
            publish.prune(api, pending, after, lambda: None)
        self.assertNotIn(old_tag, api.releases)
        self.assertIn(old_tag, api.tags)
        publish.prune(api, pending, after, lambda: None)
        self.assertNotIn(old_tag, api.tags)
        self.assertEqual(len(api.deleted), 2)

    def test_external_release_has_no_delete_plan(self):
        before = self.empty
        for minor in range(1, 6):
            before = append(before, f"1.0.{minor}", X86)
        before["plugins"][0]["releases"][-1]["github_repository"] = "https://github.com/author/notes"
        after, tag, artifacts = publish.candidate(before, submission("1.0.6", X86), [], REPO)
        verified, pending = publish.prepare_state(Releases(after), before, after, tag, artifacts)
        self.assertEqual(verified["tag"], tag)
        self.assertEqual(pending, [])

    def test_publish_never_deletes_after_commit_failure(self):
        index = append(self.empty, "1.0.1", X86)
        api = Releases(index)
        tag = publish.canonical_tag(PLUGIN, "1.0.1")
        release = api.release(tag)
        verified = {"tag": tag, **release, "assets": api.assets(release["id"])}
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            publish.write_json(root / "plan.json", {"base": "sha", "verified": verified, "pending": []})
            publish.write_json(root / publish.INDEX, index)
            cleanup = Mock()
            with patch.object(publish, "git", return_value="sha"), \
                    patch.object(publish, "commit", side_effect=ValueError("push failed")), \
                    patch.object(publish, "cleanup", cleanup), \
                    patch.object(publish, "INDEX", Path("unused-index")), \
                    patch.object(publish, "PENDING", root / "pending-prune.json"), \
                    patch.object(publish, "load", side_effect=[{"base": "sha", "verified": verified,
                                                               "pending": []}, index]), \
                    patch.object(publish, "write_json"), self.assertRaisesRegex(ValueError, "push failed"):
                # main_tip() takes the first word of ls-remote output.
                publish.publish(api, root)
            cleanup.assert_not_called()
            self.assertFalse(api.deleted)

    def test_publish_pushes_catalog_before_deleting_and_clears_pending(self):
        before = self.empty
        for minor in range(1, 6):
            before = append(before, f"1.0.{minor}", X86)
        after, tag, artifacts = publish.candidate(before, submission("1.0.6", X86), [], REPO)
        source = copy.deepcopy(before)
        source["plugins"][0]["releases"].append(after["plugins"][0]["releases"][0])
        api = Releases(source)
        verified, pending = publish.prepare_state(api, before, after, tag, artifacts)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            origin, checkout, plan = (root / p for p in ("origin", "checkout", "plan"))
            checkout.mkdir()
            plan.mkdir()
            subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
            with chdir(checkout):
                publish.git("init", "-q", "-b", "main")
                publish.git("remote", "add", "origin", str(origin))
                publish.write_json(publish.INDEX, before)
                base = publish.commit("Seed test catalog", publish.INDEX)
                publish.write_json(plan / publish.INDEX, after)
                publish.write_json(plan / "plan.json", {
                    "base": base, "verified": verified, "pending": pending,
                })
                delete = api.request

                def inspect_pushed_state(path, method):
                    git_in_origin = lambda spec: publish.git("--git-dir", str(origin), "show", spec)
                    self.assertEqual(json.loads(git_in_origin("main:index.v1.json")), after)
                    self.assertEqual(json.loads(git_in_origin("main:pending-prune.json")), pending)
                    delete(path, method)

                with patch.object(api, "request", inspect_pushed_state):
                    publish.publish(api, plan)
                self.assertFalse(publish.PENDING.exists())
                self.assertEqual(publish.git("--git-dir", str(origin), "ls-tree", "--name-only", "main"),
                                 str(publish.INDEX))
                self.assertEqual(len(api.deleted), 2)


if __name__ == "__main__":
    unittest.main()
