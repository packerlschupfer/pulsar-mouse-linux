# Arch packaging

`PKGBUILD` builds the released tarball into an Arch package. It lives here so it
is updated alongside the code it packages; the copy on the AUR is this file with
nothing added.

Everything it needs is in the official repositories — `python-pyusb`,
`python-gobject`, `gtk4`, `libadwaita`, `libdbusmenu-glib` — so the package pulls
in nothing from the AUR itself.

## Building it locally

```sh
cd packaging
makepkg -si
```

## On a release

Two lines change:

```sh
cd packaging
sed -i "s/^pkgver=.*/pkgver=X.Y.Z/" PKGBUILD
updpkgsums          # from pacman-contrib; rewrites sha256sums
makepkg -f          # confirm it still builds
```

Then push to the AUR, which needs an Arch account with an SSH key registered:

```sh
git clone ssh://aur@aur.archlinux.org/pulsar-mouse-linux.git aur
cp PKGBUILD aur/
cd aur
makepkg --printsrcinfo > .SRCINFO     # the AUR rejects a push without this
git commit -am "Update to X.Y.Z" && git push
```

`.SRCINFO` is generated rather than kept here, since it is a restatement of the
PKGBUILD and the two drifting apart is the usual way an AUR package breaks.

## Checked before it was published

Built and installed in an `archlinux:latest` container: every dependency resolves
in core/extra, the entry points land in `/usr/bin`, the udev rules in
`/usr/lib/udev/rules.d` (a packaged rule belongs there, leaving `/etc` to the
administrator), and `pulsar-mouse --version` runs against the installed copy with
all drivers discoverable. `namcap` reports nothing beyond notes about the
`#!/usr/bin/env python3` shebangs.

There is no `-git` package. One would be easy — drop `source` to the repository
and add a `pkgver()` — but two packages mean two things to keep working, and
nobody has asked for it.
