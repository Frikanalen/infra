# Ansible Role: casparcg

This role installs CasparCG Server with its CEF dependencies, sets up a dedicated user, copies the configuration, and uses nodm to auto-login into a CasparCG session.

## Role Variables

| Variable                 | Default                               | Description                                                                   |
| ------------------------ | -------------------------------------- | ------------------------------------------------------------------------------ |
| `casparcg_cef_url`       | See defaults                           | URL to CasparCG CEF .deb                                                      |
| `casparcg_server_url`    | See defaults                           | URL to CasparCG Server .deb                                                   |
| `casparcg_user`          | `casparcg`                             | System user                                                                   |
| `casparcg_media_path`    | `/mnt/media/`                          | Media path, mounted from file01 by `roles/nfs_client`                        |
| `casparcg_home`          | `/home/{{ casparcg_user }}`            | Home directory                                                                |
| `casparcg_exec`          | `/usr/bin/casparcg-server-2.4`         | Server executable                                                             |
| `casparcg_config_src`    | `casparcg.config`                      | Role-file name of config                                                      |
| `casparcg_config_dest`   | `{{ casparcg_home }}/casparcg.config`  | Destination path in user home                                                 |
| `nodm_xsession`          | `/usr/local/bin/casparcg_xsession.sh`  | nodm XSession script                                                          |
| `nodm_enabled`           | `true`                                 | Enable nodm                                                                   |
| `casparcg_video_mode`    | `720p5000`                             | Channel video mode                                                            |
| `casparcg_consumer_type` | `decklink`                             | `decklink` or `ffmpeg`                                                        |
| `casparcg_decklink_*`    | See defaults                           | Decklink consumer settings, used when `casparcg_consumer_type` is `decklink`  |
| `casparcg_ffmpeg_path`   | `udp://192.168.3.1:5568`               | ffmpeg consumer output URL, used when `casparcg_consumer_type` is `ffmpeg`    |
| `casparcg_ffmpeg_args`   | See defaults                           | Extra ffmpeg consumer args, used when `casparcg_consumer_type` is `ffmpeg`    |

Hosts in the `caspar_hw` inventory group (e.g. `caspar1`) use the Decklink
defaults as-is. Hosts in `caspar_sw` (e.g. `caspar-sw1`) get
`casparcg_consumer_type: ffmpeg` from `group_vars/caspar_sw.yml`, pairing
this role with `mesa_driver` instead of `nvidia_driver` +
`blackmagic_desktopvideo` — see `playbooks/caspar.yml`.

## Example Playbook

```yaml
- hosts: casparcg_nodes
  roles:
    - role: casparcg
      vars:
        casparcg_user: casparcg
