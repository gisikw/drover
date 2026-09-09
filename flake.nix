{
  description = "Drover coordination plane";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  inputs.herdr.url = "github:herdrdev/herdr/b99002ac99b09e00b4ca692436cb15a6b0d676f1";
  outputs = { self, nixpkgs, herdr }: let
    systems = [ "x86_64-linux" "aarch64-linux" "aarch64-darwin" "x86_64-darwin" ];
    each = nixpkgs.lib.genAttrs systems;
  in {
    packages = each (system: let
      pkgs = import nixpkgs { inherit system; };
      python = pkgs.python3.withPackages (p: [ p.aiohttp ]);
      source = pkgs.runCommand "drover-source" {} ''
        mkdir -p $out
        cp ${./drover.py} $out/drover.py
        cp ${./ssh_config.py} $out/ssh_config.py
      '';
      cli = pkgs.writeShellApplication {
        name = "drover";
        runtimeInputs = [ python pkgs.openssh ];
        text = ''
          export DROVER_HERDR=${herdr.packages.${system}.default}/bin/herdr
          exec ${python}/bin/python ${source}/drover.py "$@"
        '';
      };
      sshConfig = pkgs.writeShellApplication {
        name = "drover-ssh-config";
        runtimeInputs = [ python pkgs.openssh ];
        text = ''exec ${python}/bin/python ${source}/ssh_config.py "$@"'';
      };
    in { default = pkgs.symlinkJoin { name = "drover-0.1.0"; paths = [ cli sshConfig ]; }; });
    checks = each (system: let
      pkgs = import nixpkgs { inherit system; };
      python = pkgs.python3.withPackages (p: [ p.aiohttp ]);
    in { tests = pkgs.runCommand "drover-tests" {} ''
      cp ${./drover.py} drover.py
      cp ${./ssh_config.py} ssh_config.py
      cp ${./test_drover.py} test_drover.py
      ${python}/bin/python -m unittest -v
      touch $out
    ''; });
    devShells = each (system: let pkgs = import nixpkgs { inherit system; }; in {
      default = pkgs.mkShell { packages = [ (pkgs.python3.withPackages (p: [ p.aiohttp ])) pkgs.openssh pkgs.sshpass pkgs.gitleaks ]; };
    });
  };
}
