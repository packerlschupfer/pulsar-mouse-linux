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

          pulsar-mouse-linux = pkgs.callPackage ./package.nix { src = self; };
        }
      );

      # For consumers who'd rather bring this into their own nixpkgs
      # instance (e.g. `nixpkgs.overlays = [ pulsar-mouse-linux.overlays.default ]`)
      # than reference `packages.<system>.default` directly.
      #
      # Builds from `final`, so the package really is part of the consumer's
      # package set - their nixpkgs config and any other overlays apply, and
      # no second nixpkgs gets instantiated. See package.nix's header for
      # what the previous `self.packages.${prev.system}` version got wrong.
      overlays.default = final: _prev: {
        pulsar-mouse-linux = final.callPackage ./package.nix { src = self; };
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
