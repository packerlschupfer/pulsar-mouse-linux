{
  description = "Linux configuration tool for Pulsar gaming mice (X2A, X2H, Xlite, Feinmann 8K/FO1, Nordic)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          default = self.packages.${system}.pulsar-mouse-linux;

          pulsar-mouse-linux = pkgs.python3Packages.buildPythonApplication {
            pname = "pulsar-mouse-linux";
            version = "0.1.0";
            pyproject = true;

            src = self;

            build-system = [ pkgs.python3Packages.setuptools ];

            dependencies = [
              pkgs.python3Packages.pyusb
              pkgs.python3Packages.pygobject3
            ];

            nativeBuildInputs = [
              pkgs.wrapGAppsHook4
              pkgs.gobject-introspection
            ];

            buildInputs = [
              pkgs.gtk4
              pkgs.libadwaita
              pkgs.libdbusmenu
            ];

            # udev rules aren't picked up automatically from a Python build;
            # ship them under lib/udev/rules.d so services.udev.packages
            # (NixOS) or the package's own postinstall hook (other distros)
            # can find them. Desktop file/icon likewise aren't part of the
            # Python package itself.
            postInstall = ''
              install -Dm444 udev/50-pulsar-mouse.rules $out/lib/udev/rules.d/50-pulsar-mouse-linux.rules
              install -Dm444 data/pulsar-mouse.desktop $out/share/applications/pulsar-mouse.desktop
              install -Dm444 data/pulsar-mouse.svg $out/share/icons/hicolor/scalable/apps/pulsar-mouse.svg
            '';

            meta = {
              description = "Linux configuration tool for Pulsar gaming mice";
              homepage = "https://github.com/harveywuk/pulsar-mouse-linux";
              license = pkgs.lib.licenses.mit;
              mainProgram = "pulsar-mouse-gui";
              platforms = pkgs.lib.platforms.linux;
            };
          };
        }
      );

      # For consumers who'd rather bring this into their own nixpkgs
      # instance (e.g. `nixpkgs.overlays = [ pulsar-mouse-linux.overlays.default ]`)
      # than reference `packages.<system>.default` directly.
      overlays.default = final: prev: {
        pulsar-mouse-linux = self.packages.${prev.system}.pulsar-mouse-linux;
      };

      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/pulsar-mouse-gui";
        };
        cli = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/pulsar-mouse";
        };
      });
    };
}
