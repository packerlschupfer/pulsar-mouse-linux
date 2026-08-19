# The package definition, split out of flake.nix so it can be consumed with
# callPackage from an arbitrary package set.
#
# This matters for overlays.default: an overlay that returns
# `self.packages.${system}.pulsar-mouse-linux` would hand the consumer a
# package built from THIS flake's own nixpkgs instantiation, silently
# ignoring the `final`/`prev` package set it was given - so the consumer's
# own nixpkgs config (allowUnfree, overlays, replaced Python, ...) would not
# apply, and a second nixpkgs would be instantiated even when the consumer
# used `inputs.nixpkgs.follows`. Reading `prev.system` to do it also tripped
# nixpkgs' deprecation warning for that attribute
# ('system' -> 'stdenv.hostPlatform.system'), which surfaced on every
# evaluation of any config using the overlay.
{
  lib,
  python3Packages,
  wrapGAppsHook4,
  gobject-introspection,
  gtk4,
  libadwaita,
  libdbusmenu,
  src,
}:

python3Packages.buildPythonApplication {
  pname = "pulsar-mouse-linux";
  version = "0.1.3";
  pyproject = true;

  inherit src;

  build-system = [ python3Packages.setuptools ];

  dependencies = [
    python3Packages.pyusb
    python3Packages.pygobject3
  ];

  nativeBuildInputs = [
    wrapGAppsHook4
    gobject-introspection
  ];

  buildInputs = [
    gtk4
    libadwaita
    libdbusmenu
  ];

  # udev rules aren't picked up automatically from a Python build; ship them
  # under lib/udev/rules.d so services.udev.packages (NixOS) or the package's
  # own postinstall hook (other distros) can find them. Desktop file/icon
  # likewise aren't part of the Python package itself.
  postInstall = ''
    install -Dm444 udev/50-pulsar-mouse.rules $out/lib/udev/rules.d/50-pulsar-mouse-linux.rules
    install -Dm444 data/pulsar-mouse.desktop $out/share/applications/pulsar-mouse.desktop
    install -Dm444 data/pulsar-mouse.svg $out/share/icons/hicolor/scalable/apps/pulsar-mouse.svg
  '';

  meta = {
    description = "Linux configuration tool for Pulsar gaming mice";
    homepage = "https://github.com/packerlschupfer/pulsar-mouse-linux";
    license = lib.licenses.mit;
    mainProgram = "pulsar-mouse-gui";
    platforms = lib.platforms.linux;
  };
}
