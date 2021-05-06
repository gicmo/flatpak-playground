#!/usr/bin/python3


import argparse
import base64
import gi
import json
import os
import pathlib
import subprocess
import sys
import tempfile


gi.require_version("Flatpak", "1.0")
gi.require_version("Gio", "2.0")
gi.require_version("OSTree", "1.0")

from gi.repository import Gio  # nopep8
from gi.repository import GLib  # nopep8
from gi.repository import OSTree  # nopep8

DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"

MEDIA_TYPES = {
    "layer": "application/vnd.oci.image.layer.v1.tar",
    "manifest": "application/vnd.oci.image.manifest.v1+json",
    "config": "application/vnd.oci.image.config.v1+json"
}


def sha256sum(path: str) -> str:
    ret = subprocess.run(["sha256sum", path],
                         stdout=subprocess.PIPE,
                         encoding="utf-8",
                         check=True)

    return ret.stdout.strip().split(" ")[0]


class Index:
    def __init__(self, registry):
        self.registry = registry

        path = os.path.join(self.registry.root, "index.json")
        with open(path, "r") as f:
            data = json.load(f)
        self.data = data


class Registry:
    def __init__(self, root):
        self.root = pathlib.Path(root)

        if not self.root.exists():
            self.init()

        blobs = self.root.joinpath("blobs")
        self.blobs = {}

        for algorithm in ("sha256", ):
            path = blobs.joinpath(algorithm)
            path.mkdir(parents=True, exist_ok=True)
            self.blobs[algorithm] = path

    def init(self):
        layout = self.root.joinpath("oci-layout")
        data = {"imageLayoutVersion": "1.0.0"}

        with open(layout, "w") as f:
            json.dump(data, f)

    def path_for_blob(self, digest):
        algorithm, want = digest.split(":", 1)
        if not algorithm in self.blobs:
            return None

        return self.blobs[algorithm].joinpath(want)

    def blobs_add_file(self, path: str, mtype: str, algorithm="sha256"):
        blobs = self.blobs[algorithm]

        digest = sha256sum(path)
        size = os.stat(path).st_size

        os.rename(path, os.path.join(blobs, digest))
        info = {
            "digest": f"{algorithm}:{digest}",
            "size": size,
            "mediaType": MEDIA_TYPES[mtype]
        }

        print(f"blobs: +{mtype} ({size}, {digest})")
        return info

    def blobs_add_json(self, js: str, mtype: str, algorithm="sha256"):
        js_file = tempfile.mkstemp(dir=self.root, prefix=".temp-")

        with open(js_file, "w") as f:
            json.dump(js, f, indent=4)

        return self.blobs_add_file(js_file, mtype, algorithm=algorithm)

    def blobs_get_json(self, digest):
        path = self.path_for_blob(digest)
        with open(path, "r") as f:
            return json.load(f)

    def tempdir(self):
        return tempfile.TemporaryDirectory(dir=self.root, prefix=".tmp-")

    def __contains__(self, digest):
        path = self.path_for_blob(digest)
        return path.exists()


def import_layer(repo, mtree, path, modifier):
    with tempfile.TemporaryDirectory(dir="/var/tmp") as tmpdir:
        tree = os.path.join(tmpdir, "root")
        os.makedirs(tree)
        command = [
            "tar",
            "-x",
            "--auto-compress",
            "-f", path,
            "-C", tree
        ]
        subprocess.run(command, check=True)

        repo.write_directory_to_mtree(Gio.File.new_for_path(tree),
                                      mtree,
                                      modifier)


def import_image(registry, repo, digest):

    manifest = registry.blobs_get_json(digest)

    cfgdesc = manifest["config"]

    config = registry.blobs_get_json(cfgdesc["digest"])
    labels = config["config"]["Labels"]

    ref = labels["org.flatpak.ref"]
    parent = labels.get("org.flatpak.parent-commit")
    timestamp = int(labels["org.flatpak.timestamp"])
    subject = labels["org.flatpak.subject"]
    body = labels["org.flatpak.body"]

    builder = GLib.VariantBuilder(GLib.VariantType("a{sv}"))
    for k, v in labels.items():
        if not k.startswith("org.flatpak.commit-metadata."):
            continue
        binary = GLib.Bytes.new(base64.b64decode(v))
        key = k[len("org.flatpak.commit-metadata."):]

        data = GLib.Variant.new_from_bytes(GLib.VariantType("v"),
                                           binary,
                                           False)

        builder.add_value(GLib.Variant("{sv}", (key, data.get_variant())))

    _, checksum = digest.split(":", 1)
    builder.add_value(GLib.Variant("{sv}", ("xa.alt-id", GLib.Variant("s", checksum))))

    last_diff_id = config["rootfs"]["diff_ids"][-1]
    _, checksum = last_diff_id.split(":", 1)
    builder.add_value(GLib.Variant("{sv}", ("xa.diff-id", GLib.Variant("s", checksum))))

    metadata = builder.end()

    repo.prepare_transaction()
    try:
        mtree = OSTree.MutableTree.new()

        for layer in manifest["layers"]:
            digest = layer["digest"]
            path = registry.path_for_blob(digest)
            import_layer(repo, mtree, path, None)

        _, root = repo.write_mtree(mtree)

        _, rev = repo.write_commit_with_time(parent,
                                             subject,
                                             body,
                                             metadata,
                                             root,
                                             timestamp)

        print(f"committing {rev} for {ref}")

        repo.transaction_set_ref(None, ref, rev)

        # complete repo transaction
        repo.commit_transaction(None)
    except:
        repo.abort_transaction()
        raise


def iter_commits(repo):
    _, refs = repo.list_refs()

    for ref in refs:
        _, commit_id = repo.resolve_rev(ref, False)
        _, raw_md, _ = repo.load_commit(commit_id)
        metadata = GLib.VariantDict.new(raw_md.get_child_value(0))
        alt_id = metadata.lookup_value("xa.alt-id", GLib.VariantType("s"))
        if alt_id:
            yield "sha256:" + alt_id.get_string(), commit_id


def main():
    parser = argparse.ArgumentParser(description="Import flatpaks from OCI")

    parser.add_argument("registry", metavar="REGISTRY", type=os.path.abspath,
                        help="The local OCI container directory to use ")
    parser.add_argument("repo", metavar="REPOSITORY", type=os.path.abspath,
                        help="The OSTree repository to use")
    parser.add_argument("images", metavar="IMAGES",
                        help="File containing the images to import or '-' for stdin")

    args = parser.parse_args(sys.argv[1:])

    path = args.images
    if path == "-":
        pkgs = sys.stdin.read()
    else:
        with open(path) as f:
            pkgs = f.read()

    images = pkgs.split("\n")

    repo = OSTree.Repo.new(Gio.File.new_for_path(args.repo))
    repo.create(OSTree.RepoMode.ARCHIVE_Z2)

    registry = Registry(args.registry)

    existing = dict(iter_commits(repo))

    for img in filter(len, images):
        commit = existing.get(img)

        if commit:
            print(f"found commit '{commit}'' for image '{img}'")
            continue

        print(f"importing {img}")
        import_image(registry, repo, img)

    subprocess.run(["ostree", "summary", "--repo", args.repo, "-u"])


if __name__ == "__main__":
    main()
