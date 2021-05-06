
# Playground for Flatpak and osbuild

## Dep-solving flatpaks
```
echo "app/org.gnome.Devhelp/x86_64/stable" | dbus-run-session python3 flatpak-depsolve.py fedora.flatpakrepo -
```

## Fetching flatpaks
```
skopeo copy docker://registry.fedoraproject.org/devhelp@sha256:<id> oci:/tmp/registry
skopeo copy docker://registry.fedoraproject.org/f34/flatpak-runtime@sha256:<id>  oci:/tmp/registry
```

## Importing flatpaks

```
echo -e 'sha256:<id>\nsha256:<id>' | python3 flatpak-import.py /tmp/registry/ /tmp/repo -
```
