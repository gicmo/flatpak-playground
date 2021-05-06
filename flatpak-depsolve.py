#!/usr/bin/python3


import argparse
import gi
import os
import sys
import tempfile

from functools import partial

gi.require_version("Flatpak", "1.0")
gi.require_version("Gio", "2.0")
gi.require_version("OSTree", "1.0")

import gi.repository.Flatpak as Flatpak  # nopep8
from gi.repository.Flatpak import Transaction, Installation, Remote  # nopep8
from gi.repository.Gio import File  # nopep8
from gi.repository import GLib  # nopep8

OSTREE_SUMMARY_GVARIANT_STRING = "(a(s(taya{sv}))a{sv})"


def metadata_for_remote(installation, remote):
    remote_name = remote.get_name()
    instpath = installation.get_path()
    summary_path = instpath.get_child(f"oci/{remote_name}.summary")
    data, _ = summary_path.load_bytes()
    variant_type = GLib.VariantType(OSTREE_SUMMARY_GVARIANT_STRING)
    summary = GLib.Variant.new_from_bytes(variant_type, data, False)
    ref_map = summary.get_child_value(0)
    metadata = {
        key: value[2] for key, value in ref_map
    }
    return metadata


def operation_ready(result, transaction):
    ops = transaction.get_operations()
    installation = transaction.get_installation()

    metadata = {}

    for op in ops:
        ref, commit = op.get_ref(), op.get_commit()
        remote = op.get_remote()

        remote = installation.get_remote_by_name(remote)
        remote_url = remote.get_url()

        pkg = {
            "ref": ref,
            "remote": remote_url
        }

        if remote_url.startswith("oci"):
            if remote not in metadata:
                metadata[remote] = metadata_for_remote(installation, remote)
            md = metadata[remote][ref]

            repo = md["xa.oci-repository"]

            url_base = remote_url[4:]

            pkg["transport"] = "oci"
            pkg["repo"] = repo
            pkg["url"] = f"{url_base}/{repo}@sha256:{commit}"
        else:
            pkg["transport"] = "ostree"
            pkg["url"] = remote_url

        result[commit] = pkg

    return False


def main():
    parser = argparse.ArgumentParser(description="Dep-solve flatpaks")

    parser.add_argument("remote", metavar="REMOTE", type=os.path.abspath,
                        help="Path to a 'flatpakrepo' file containing the remote information")
    parser.add_argument("flatpaks", metavar="FLATPAKS",
                        help="File containing the flatpaks to import or '-' for stdin")

    args = parser.parse_args(sys.argv[1:])

    path = args.flatpaks
    if path == "-":
        pkgs = sys.stdin.read()
    else:
        with open(path) as f:
            pkgs = f.read()

    flatpaks = pkgs.split("\n")

    with tempfile.TemporaryDirectory() as tmp:
        install_path = File.new_for_path(tmp)
        installation = Installation.new_for_path(install_path, True, None)
        installation.set_no_interaction(True)

        remote_path = File.new_for_path(args.remote)
        remote_data, _ = remote_path.load_bytes()
        remote = Remote.new_from_file("flatpak", remote_data)

        remote_name = remote.get_name()

        installation.add_remote(remote, True, None)

        packages = {}

        transaction = Transaction.new_for_installation(installation)
        transaction.connect("ready", partial(operation_ready, packages))

        for pkg in filter(len, flatpaks):
            print(f"'{pkg}'", file=sys.stderr)
            transaction.add_install(remote_name, pkg, None)

        try:
            transaction.run()
        except gi.repository.GLib.Error as e:
            if not e.matches(Flatpak.error_quark(), Flatpak.Error.ABORTED):
                print(e, file=sys.stderr)
                sys.exit(1)

        print(packages)


if __name__ == "__main__":
    main()
