"""Render an operator-installed OpenSSH rendezvous configuration.

No daemon implementation or terminal relay. Run with the coordinator stopped
when changing authorization; restart sshd AND terminate revoked connections.
"""
import argparse
import json
from pathlib import Path
import re
import sqlite3
from drover import private_write


def public_key(value):
    parts = value.split()
    if len(parts) < 2 or parts[0] != 'ssh-ed25519' or not re.fullmatch('[A-Za-z0-9+/=]+', parts[1]):
        raise ValueError('expected an OpenSSH Ed25519 public key')
    return ' '.join(parts[:2])


def render(config, rows):
    host, port = config.get('ssh_listen', '127.0.0.1:9841').rsplit(':', 1)
    if not re.fullmatch('[0-9a-fA-F:.]+', host) or not 1024 <= int(port) <= 65535:
        raise ValueError('numeric listen address and unprivileged port required')
    for key in ('tunnel_user', 'jump_user'):
        if not re.fullmatch('[a-z_][a-z0-9_-]*', config[key]):
            raise ValueError('invalid user')
    root = Path(config['output']).resolve()
    if any(c.isspace() for c in str(root)):
        raise ValueError('output path must not contain whitespace')
    tunnel_keys, destinations = [], []
    for name, route in rows:
        key = public_key(config['tunnel_keys'][name])
        tunnel_keys.append(f'restrict,port-forwarding,permitlisten="127.0.0.1:{route}" {key}')
        destinations.append(f'permitopen="127.0.0.1:{route}"')
    if not destinations:
        raise ValueError('enroll a machine before enabling jump access')
    jump = 'restrict,port-forwarding,' + ','.join(destinations) + ' ' + public_key(config['client_key'])
    private_write(root / 'tunnel_keys', '\n'.join(tunnel_keys) + '\n')
    private_write(root / 'jump_keys', jump + '\n')
    text = f'''ListenAddress {host}
Port {port}
HostKey {root}/host_key
PidFile {root}/sshd.pid
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
UsePAM no
PermitRootLogin no
AllowUsers {config['tunnel_user']} {config['jump_user']}
AuthorizedKeysFile none
StrictModes yes
MaxSessions 0
PermitTTY no
AllowAgentForwarding no
AllowStreamLocalForwarding no
PermitUserRC no
X11Forwarding no
PermitTunnel no
GatewayPorts no
ClientAliveInterval 15
ClientAliveCountMax 3
Match User {config['tunnel_user']}
  AuthorizedKeysFile {root}/tunnel_keys
  AllowTcpForwarding remote
  PermitOpen none
Match User {config['jump_user']}
  AuthorizedKeysFile {root}/jump_keys
  AllowTcpForwarding local
  PermitListen none
'''
    private_write(root / 'sshd_config', text)
    return text


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('config')
    args = p.parse_args()
    config = json.loads(Path(args.config).read_text())
    db = sqlite3.connect(config['database'])
    print(render(config, db.execute('SELECT name,port FROM machines').fetchall()))
