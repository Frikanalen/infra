# Ansible Role: casparcg

This role installs CasparCG Server with its CEF dependencies, sets up a dedicated user, copies the configuration, and uses nodm to auto-login into a CasparCG session.

## Role Variables

| Variable                | Default                              | Description                   |
|-------------------------|--------------------------------------|-------------------------------|
| `casparcg_cef_url`      | See defaults                         | URL to CasparCG CEF .deb      |
| `casparcg_server_url`   | See defaults                         | URL to CasparCG Server .deb   |
| `casparcg_user`         | `casparcg`                           | System user                   |
| `casparcg_home`         | `/home/{{ casparcg_user }}`          | Home directory                |
| `casparcg_exec`         | `/usr/bin/casparcg-server-2.4`       | Server executable             |
| `casparcg_config_src`   | `casparcg.config`                    | Role-file name of config      |
| `casparcg_config_dest`  | `{{ casparcg_home }}/casparcg.config`| Destination path in user home |
| `nodm_xsession`         | `/usr/local/bin/casparcg_xsession.sh`| nodm XSession script          |
| `nodm_enabled`          | `true`                               | Enable nodm                   |

## Example Playbook

```yaml
- hosts: casparcg_nodes
  roles:
    - role: casparcg
      vars:
        casparcg_user: casparcg
